"""Web interface for the swarm: ask a question, watch the agents work, search memory.

Run inside the devlab container (the Nebius tunnel publishes it as service "swarm"):
  uv run uvicorn app:app --host 0.0.0.0 --port 8090

Limits, from .env: SWARM_UI_CODE (access code; empty means open to anyone),
SWARM_UI_TOKEN_BUDGET (tokens per run), SWARM_UI_DAILY_RUNS (runs per UTC day).
"""

import hmac
import json
import os
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import builder
import memory
from swarm import DEFAULT_DESIGN, DEFAULT_MODEL, HERE, load_design, run_swarm

# Read after importing swarm, which loads .env.
ACCESS_CODE = os.environ.get("SWARM_UI_CODE", "")
TOKEN_BUDGET = int(os.environ.get("SWARM_UI_TOKEN_BUDGET", "300000"))
DAILY_RUNS = int(os.environ.get("SWARM_UI_DAILY_RUNS", "20"))
MAX_QUESTION_CHARS = 2000
EVENT_TEXT_CHARS = 6000

app = FastAPI(title="swarm-lab", docs_url=None, redoc_url=None)

_live: dict[str, dict] = {}  # runs started by this process, by id
_busy = threading.Lock()
builder.mark_interrupted()


class BudgetExceeded(Exception):
    pass


class Ask(BaseModel):
    question: str
    build: bool = False  # also build a web page from the answer


class BuildFromRun(BaseModel):
    run_id: int


def require_code(request: Request) -> None:
    if not ACCESS_CODE:
        return
    given = request.headers.get("x-access-code") or request.query_params.get("code") or ""
    if not hmac.compare_digest(given.encode(), ACCESS_CODE.encode()):
        raise HTTPException(401, "Enter the access code to use this page.")


def check_daily_limit() -> None:
    # Counted from the run log, so restarting the server does not reset it.
    if memory.runs_today() >= DAILY_RUNS:
        raise HTTPException(429, f"Today's limit of {DAILY_RUNS} questions is used up. It resets at midnight UTC.")


def worker(run: dict) -> None:
    def on_event(agent: str, msg) -> None:
        if msg.type != "ai":
            return
        run["tokens"] += (msg.usage_metadata or {}).get("total_tokens", 0)
        for call in msg.tool_calls:
            args = call["args"]
            detail = (args.get("query") or args.get("url") or args.get("source_url") or args.get("fact")
                      or args.get("agent_name") or "")
            run["events"].append({"agent": agent, "kind": "tool", "tool": call["name"],
                                  "detail": str(detail)[:300], "t": time.time()})
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.strip():
            run["events"].append({"agent": agent, "kind": "message", "text": content[:EVENT_TEXT_CHARS],
                                  "t": time.time()})
        if run["tokens"] > TOKEN_BUDGET:
            raise BudgetExceeded(f"stopped at the {TOKEN_BUDGET:,}-token budget")

    try:
        result = run_swarm(load_design(DEFAULT_DESIGN), run["question"], on_event=on_event)
        error = result["error"] or ""
        run.update(
            status="stopped" if error.startswith("BudgetExceeded") else "failed" if error else "done",
            answer=result["answer"], sources=result["fetched_urls"], error=result["error"],
            tokens=result["total_tokens"], run_id=result.get("run_id"),
        )
        if run["build"] and run["status"] == "done":
            try:
                run["build_id"] = builder.start_build(run["question"], run["answer"], run["run_id"])["id"]
            except builder.BuildRefused as e:
                run["build_error"] = str(e)
    except Exception as e:  # anything unexpected still ends the run cleanly for the page
        run.update(status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        run["finished"] = time.time()
        _busy.release()


@app.get("/")
def index():
    return FileResponse(HERE / "ui" / "index.html")


@app.get("/api/status")
def status(request: Request):
    require_code(request)
    current = next((r for r in _live.values() if r["status"] == "running"), None)
    agents = load_design(DEFAULT_DESIGN)["agents"]
    return {
        "models": {a["name"]: a.get("model") or DEFAULT_MODEL for a in agents},
        "busy": current["id"] if current else None,
        "runs_today": memory.runs_today(),
        "daily_limit": DAILY_RUNS,
        "token_budget": TOKEN_BUDGET,
        "team": [a["name"] for a in agents],
        "builds_today": builder.builds_today(),
        "daily_builds": builder.DAILY_BUILDS,
        "build_busy": builder.busy(),
        "build_model": builder.MODEL,
        **memory.stats(),
    }


@app.post("/api/ask")
def ask(body: Ask, request: Request):
    require_code(request)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")
    if len(question) > MAX_QUESTION_CHARS:
        raise HTTPException(400, f"Keep the question under {MAX_QUESTION_CHARS} characters.")
    if not _busy.acquire(blocking=False):
        raise HTTPException(409, "The team is answering another question. Try again when it finishes.")
    try:
        check_daily_limit()
    except HTTPException:
        _busy.release()
        raise
    run = {"id": uuid.uuid4().hex[:12], "question": question, "status": "running", "events": [],
           "tokens": 0, "started": time.time(), "answer": "", "sources": [], "error": None,
           "build": body.build, "build_id": None, "build_error": None}
    _live[run["id"]] = run
    threading.Thread(target=worker, args=(run,), daemon=True).start()
    return {"id": run["id"]}


@app.get("/api/live/{run_id}/events")
def events(run_id: str, request: Request):
    """Server-sent events: each agent step as it happens, then the final result."""
    require_code(request)
    run = _live.get(run_id)
    if not run:
        raise HTTPException(404, "That run is not running on this server any more.")

    def stream():
        sent = 0
        while True:
            while sent < len(run["events"]):
                yield f"event: step\ndata: {json.dumps(run['events'][sent])}\n\n"
                sent += 1
            yield f"event: tokens\ndata: {json.dumps({'tokens': run['tokens']})}\n\n"
            if run["status"] != "running":
                final = {k: run.get(k) for k in ("status", "answer", "sources", "error", "tokens", "run_id",
                                                 "build_id", "build_error")}
                yield f"event: final\ndata: {json.dumps(final)}\n\n"
                return
            time.sleep(0.7)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs(request: Request):
    require_code(request)
    return memory.recent_runs(20)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int, request: Request):
    require_code(request)
    run = memory.get_run(run_id)
    if not run:
        raise HTTPException(404, "No saved run with that number.")
    return run


@app.get("/api/memory")
def search_memory(request: Request, q: str = ""):
    require_code(request)
    return memory.search_facts(q[:200], 30)


@app.get("/api/builds")
def builds(request: Request):
    require_code(request)
    return builder.list_builds()[:20]


@app.get("/api/builds/{build_id}")
def build_detail(build_id: str, request: Request):
    require_code(request)
    job = builder.get_build(build_id)
    if not job:
        raise HTTPException(404, "No page build with that id.")
    return job


@app.post("/api/builds")
def build_from_run(body: BuildFromRun, request: Request):
    """Build a page from a saved answer."""
    require_code(request)
    run = memory.get_run(body.run_id)
    if not run:
        raise HTTPException(404, "No saved run with that number.")
    if run["error"]:
        raise HTTPException(400, "That run did not finish cleanly, so there is no answer to build from.")
    question = run["question"].split("\n\nContext provided")[0]
    try:
        return builder.start_build(question, run["answer"], run["id"])
    except builder.BuildRefused as e:
        raise HTTPException(409, str(e))
