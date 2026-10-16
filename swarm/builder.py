"""Build jobs: turn a swarm report into a published web page.

Each job runs OpenCode (Kimi-K3 on Token Factory, with the Anatomy skill) in a fresh
NVIDIA OpenShell sandbox whose policy only allows the model API and npm reads:

  create sandbox -> upload TASK.md + report -> opencode run -> download out/
  -> publish out/index.html at public/pages/<slug>/ -> delete sandbox

One job runs at a time. Job state lives in builds/<id>/status.json so it survives
restarts of the web server (a job interrupted by a restart is marked failed).
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
BUILDER_DIR = HERE.parent / "builder-image"
BUILDS = HERE / "builds"
PUBLIC_PAGES = HERE / "public" / "pages"
# Public address that serves public/ (e.g. a Nebius tunnel); empty gives relative links.
PUBLIC_BASE = os.environ.get("SWARM_PUBLIC_BASE", "").rstrip("/")
IMAGE = os.environ.get("SWARM_BUILDER_IMAGE", "swarm-builder:1")
POLICY = Path(os.environ.get("SWARM_BUILDER_POLICY", BUILDER_DIR / "policy.yaml"))
OPENCODE_CONFIG = Path(os.environ.get("SWARM_BUILDER_CONFIG", BUILDER_DIR / "opencode.json"))
# OpenShell provider holding the Token Factory key; must match credential_binding in the policy.
PROVIDER = os.environ.get("SWARM_BUILDER_PROVIDER", "swarm-builder-nebius")
MODEL = "moonshotai/Kimi-K3"
BUILD_MINUTES = int(os.environ.get("SWARM_BUILD_MINUTES", "60"))
DAILY_BUILDS = int(os.environ.get("SWARM_UI_DAILY_BUILDS", "3"))
MAX_CONTINUES = 3
PHONE_WIDTH = 390
PROMPT = "Read /sandbox/work/TASK.md and carry out the task it describes. Load the anatomy skill first."
CONTINUE_PROMPT = (
    "Continue the task in /sandbox/work/TASK.md from where you stopped. Keep each reasoning "
    "step short and act with tools; write out/NOTES.md before you finish."
)

_lock = threading.Lock()

TASK_TEMPLATE = """# Task: build an Anatomy page from a research report

You are running inside an NVIDIA OpenShell sandbox. Your working directory is
`/sandbox/work`. Only `/sandbox/work`, `/tmp` and your home directory are writable, and
the network is closed except for the model API, so every check runs locally.

## Goal

Build one standalone web page, `/sandbox/work/out/index.html`, about:

> {title}

1. **An Anatomy figure** that explains the report's subject. Load the **anatomy** skill
   with the skill tool before anything else, read its `SKILL.md` in full and follow its
   workflow and quality bar. Its files are in `~/.config/opencode/skills/anatomy/`.
   Size: **Figure** (one mechanism, 40–80 parts, SVG only, no WebGL). Invent a real,
   recognisable machine whose working maps one-to-one onto the subject ("the object is
   the explanation"), with a live readout and calm motion.
2. **The report below the figure**, converted from `brief/report.md` to HTML with its
   headings, tables, code blocks, citations and sources list intact. Write the converter
   yourself (no network, no external libraries).
3. **An attribution block** at the end of the page with exactly these facts: the figure
   and page were built autonomously by OpenCode with the model `{model}` (Nebius Token
   Factory) inside an NVIDIA OpenShell sandbox, using the Anatomy skill; the report was
   researched and written by the swarm-lab agents in answer to the question
   "{question}" on {date}; nobody reviewed the page before it was published.

## Truth

Every number in the readout, the motion and the captions must come from
`brief/report.md`. Do not invent facts.

## Page rules

- One HTML file with everything inline: no CDN scripts, no web fonts, no external images.
- Dark by default, light when `prefers-color-scheme: light`. Both must be readable.
- Works at phone width (390 px) with no sideways page scroll; wide tables scroll in their box.

## Checks (the skill's step 10)

Node is installed, and Chromium is at `$CHROME_PATH` for the skill's `scripts/capture.mjs`,
`scripts/drive.mjs` and `scripts/lines.mjs`. Capture desktop and phone widths in both
themes, run the line check to `line ends: 0`, and run the audit to `audit passed` if you
use pipes, cables or round parts. Read the PNGs you make and fix what you see. If Chromium
cannot start inside the sandbox, say so in your notes and continue without screenshots.

## Output

- `out/index.html` (the page), `out/build.mjs` (the figure build), `out/shots/` (PNGs).
- `out/NOTES.md`: the object you chose and what each part maps to, the part count, what
  moves, the readout, and the results of every check, stated plainly, including anything
  unfinished or failing.

Work for at most about {minutes} minutes. Have a working page early, then improve it.
Update `out/NOTES.md` as you go so partial progress is never lost.
"""


class BuildRefused(Exception):
    """A build could not start (busy, daily limit, nothing to build from)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "page"


def _status_path(job_id: str) -> Path:
    return BUILDS / job_id / "status.json"


def _save(job: dict) -> None:
    path = _status_path(job["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1))
    tmp.replace(path)


def _openshell(*args: str, timeout: int = 300, stdout=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["openshell", *args], check=True, text=True, timeout=timeout,
        stdout=stdout or subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def check_page(page: Path, shots: Path) -> dict:
    """Load a built page offline in headless Chromium; report hangs, script errors and overflow.

    Runs on the trusted side with every non-file request blocked, so the page cannot
    reach the network while it is checked.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    import tools  # noqa: F401  (sets PLAYWRIGHT_BROWSERS_PATH on a devlab)

    shots.mkdir(parents=True, exist_ok=True)
    problems, stats = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for label, size in (("desktop", {"width": 1280, "height": 900}),
                                ("phone", {"width": PHONE_WIDTH, "height": 844})):
                context = browser.new_context(viewport=size)
                context.route("**/*", lambda r: r.continue_() if r.request.url.startswith("file:") else r.abort())
                pg = context.new_page()
                errors = []
                pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
                try:
                    pg.goto(page.resolve().as_uri(), timeout=20000)
                    pg.wait_for_timeout(2000)
                    pg.screenshot(path=str(shots / f"{label}.png"), timeout=15000)
                    found = pg.evaluate(
                        "() => ({width: document.documentElement.scrollWidth, svgs: document.querySelectorAll('svg').length})"
                    )
                except PlaywrightError as e:
                    problems.append(f"{label}: the page did not finish loading or rendering ({str(e).splitlines()[0][:120]})")
                    context.close()
                    continue
                stats[label] = found
                if errors:
                    problems.append(f"{label}: script error: {errors[0]}")
                if label == "phone" and found["width"] > PHONE_WIDTH + 2:
                    problems.append(f"phone: page is {found['width']} px wide at a {PHONE_WIDTH} px screen")
                if not found["svgs"]:
                    problems.append(f"{label}: no SVG figure on the page")
                context.close()
        finally:
            browser.close()
    return {"passed": not problems, "problems": sorted(set(problems)), "stats": stats}


def _stop_reason(log: Path) -> str:
    """How the last OpenCode step ended: 'length' means the model ran out of output room."""
    reason = ""
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "step_finish":
                reason = event.get("part", {}).get("reason", "")
            elif event.get("type") == "error":
                reason = "error: " + str(event.get("error", {}).get("data", {}).get("message", ""))[:200]
    return reason


def summarize(log: Path) -> dict:
    """Progress from OpenCode's --format json event log."""
    tools, files = Counter(), set()
    tokens = cost = 0
    first = last = None
    last_text = ""
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = event.get("timestamp")
            first = first or ts
            last = ts or last
            part = event.get("part", {})
            if event.get("type") == "tool_use":
                tools[part.get("tool")] += 1
                target = part.get("state", {}).get("input", {}).get("filePath")
                if part.get("tool") in ("write", "edit") and target:
                    files.add(target.removeprefix("/sandbox/work/"))
            elif event.get("type") == "step_finish":
                tokens += part.get("tokens", {}).get("total", 0)
                cost += part.get("cost", 0) or 0
            elif event.get("type") == "text":
                last_text = part.get("text", "")
    return {
        "minutes": round((last - first) / 60000, 1) if first and last else 0,
        "tool_calls": sum(tools.values()),
        "tools": dict(tools.most_common(8)),
        "files": sorted(files)[:30],
        "tokens": tokens,
        "cost_usd": round(cost, 2),
        "last_text": last_text[-500:],
    }


def builds_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for job in list_builds(with_progress=False) if job["created"].startswith(today))


def busy() -> bool:
    return _lock.locked()


def list_builds(with_progress: bool = True) -> list[dict]:
    jobs = []
    for path in BUILDS.glob("*/status.json"):
        try:
            job = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if with_progress:
            job["progress"] = summarize(path.parent / "run.jsonl")
        jobs.append(job)
    return sorted(jobs, key=lambda j: j["created"], reverse=True)


def get_build(job_id: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{10}", job_id):
        return None
    path = _status_path(job_id)
    if not path.exists():
        return None
    job = json.loads(path.read_text())
    job["progress"] = summarize(path.parent / "run.jsonl")
    return job


def mark_interrupted() -> None:
    """Jobs left running by a previous server process can no longer finish."""
    for job in list_builds(with_progress=False):
        if job["status"] not in ("done", "failed"):
            job.update(status="failed", error="Interrupted by a server restart.", finished=_now())
            _save(job)
            subprocess.run(["openshell", "sandbox", "delete", job["sandbox"]],
                           capture_output=True, timeout=120)


def start_build(question: str, report: str, run_id: int | None = None) -> dict:
    if not report.strip():
        raise BuildRefused("There is no answer to build a page from.")
    if builds_today() >= DAILY_BUILDS:
        raise BuildRefused(f"Today's limit of {DAILY_BUILDS} page builds is used up. It resets at midnight UTC.")
    if not _lock.acquire(blocking=False):
        raise BuildRefused("Another page is being built. Try again when it finishes.")
    try:
        job_id = uuid.uuid4().hex[:10]
        title = question.split("\n")[0].strip()[:160]
        slug = f"{_slug(title)}-{job_id[:6]}"
        job = {
            "id": job_id, "title": title, "question": question, "run_id": run_id,
            "slug": slug, "url": f"{PUBLIC_BASE}/pages/{slug}/", "sandbox": f"b-{job_id}",
            "model": MODEL, "status": "starting", "created": _now(), "finished": None,
            "error": None, "notes": "", "stop_reason": "", "builder_finished": False,
            "continues": 0, "check": None,
        }
        (BUILDS / job_id).mkdir(parents=True)
        _save(job)
        threading.Thread(target=_run, args=(job, report), daemon=True).start()
        return job
    except Exception:
        _lock.release()
        raise


def _run(job: dict, report: str) -> None:
    folder = BUILDS / job["id"]
    brief = folder / "brief"
    brief.mkdir()
    task = brief / "TASK.md"
    task.write_text(TASK_TEMPLATE.format(
        title=job["title"], question=job["question"].replace('"', "'")[:500], model=MODEL,
        date=job["created"][:10], minutes=BUILD_MINUTES,
    ))
    (brief / "report.md").write_text(report)
    name = job["sandbox"]
    try:
        _openshell(
            "sandbox", "create", "--name", name, "--from", IMAGE, "--policy", str(POLICY),
            "--provider", PROVIDER, "--no-auto-providers", "--approval-mode", "manual",
            "--cpu", "2", "--memory", "6Gi", "--label", "app=swarm-build",
            "--env", "OPENCODE_DISABLE_AUTOUPDATE=1", "--env", "OPENCODE_DISABLE_MODELS_FETCH=1",
            "--env", "CHROME_PATH=/usr/bin/chromium",
            "--detach", "--no-tty", "--", "/usr/bin/sleep", "infinity",
        )
        # The image carries an older brief from its first use; replace it with this job's.
        _openshell("sandbox", "exec", "--name", name, "--no-tty", "--no-login-shell", "--",
                   "sh", "-c", "rm -rf /sandbox/work/brief /sandbox/work/out /sandbox/work/TASK.md "
                   "&& mkdir -p /sandbox/work/brief /sandbox/work/out")
        for local, remote in ((task, "/sandbox/work/TASK.md"),
                              (brief / "report.md", "/sandbox/work/brief/report.md"),
                              (OPENCODE_CONFIG, "/sandbox/work/opencode.json")):
            _openshell("sandbox", "upload", name, str(local), remote)

        job.update(status="building", continues=0)
        _save(job)
        log_path = folder / "run.jsonl"
        deadline = time.time() + BUILD_MINUTES * 60
        flags = ""
        prompt = PROMPT
        while True:
            remaining = int(deadline - time.time())
            with open(log_path, "a") as log:
                subprocess.run(
                    ["openshell", "sandbox", "exec", "--name", name, "--no-tty", "--no-login-shell", "--",
                     "sh", "-c", f"cd /sandbox/work && timeout {max(remaining, 60)} "
                     f"opencode run --auto --format json {flags}\"{prompt}\""],
                    stdout=log, stderr=subprocess.STDOUT, timeout=max(remaining, 60) + 300,
                )
            job["stop_reason"] = _stop_reason(log_path)
            has_notes = _openshell("sandbox", "exec", "--name", name, "--no-tty", "--no-login-shell", "--",
                                   "sh", "-c", "test -s /sandbox/work/out/NOTES.md && echo yes || echo no").stdout
            finished = "yes" in has_notes and job["stop_reason"] not in ("length",)
            # Resume the same session when the model stopped early and there is time left.
            if (finished or job["stop_reason"].startswith("error") or job["continues"] >= MAX_CONTINUES
                    or deadline - time.time() < 300):
                break
            job["continues"] += 1
            _save(job)
            flags, prompt = "--continue ", CONTINUE_PROMPT
        job["builder_finished"] = finished

        job.update(status="checking")
        _save(job)
        _openshell("sandbox", "download", name, "/sandbox/work/out", str(folder / "out"), timeout=600)
        pages = sorted((folder / "out").rglob("index.html"), key=lambda p: len(p.parts))
        notes = sorted((folder / "out").rglob("NOTES.md"), key=lambda p: len(p.parts))
        if notes:
            job["notes"] = notes[0].read_text(errors="replace")[:20000]
        if not pages:
            raise RuntimeError("The builder stopped without writing out/index.html "
                               f"(last step: {job['stop_reason'] or 'unknown'}).")
        job["check"] = check_page(pages[0], folder / "check")
        if not job["check"]["passed"]:
            raise RuntimeError("The page failed its checks, so it was not published: "
                               + "; ".join(job["check"]["problems"]))
        dest = PUBLIC_PAGES / job["slug"]
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(pages[0], dest / "index.html")
        job.update(status="done")
    except subprocess.CalledProcessError as e:
        job.update(status="failed", error=f"{' '.join(e.cmd[:3])} failed: {(e.stdout or '')[-400:]}")
    except Exception as e:  # report any other failure on the page instead of losing the job
        job.update(status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        job["finished"] = _now()
        _save(job)
        subprocess.run(["openshell", "sandbox", "delete", name], capture_output=True, timeout=120)
        _lock.release()
