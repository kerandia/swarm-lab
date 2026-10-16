"""MASS-lite: improve the swarm's team design by self-supervised workflow search.

A small take on the workflow-search half of SakanaAI's MASS
(https://github.com/SakanaAI/mass). Each iteration:
  1. run the champion design on every question (cached),
  2. an optimizer model proposes a new design from the champion's answers,
     the latest judge feedback and the history of earlier attempts,
  3. run the candidate on every question,
  4. a judge model compares champion vs candidate per question, in both orders,
  5. the candidate becomes champion if it scores above 0.5.
The same model is executor, optimizer and judge by default, as in MASS.
Every run's full trajectory is saved, so the champion's traces can later feed
MASS's post-training step (LoRA on the winning trajectories).

Usage:
  uv run python mass_lite.py --run demo --iterations 3
  uv run python swarm.py --design mass_runs/demo/champion.json "your question"
"""

import argparse
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from swarm import (
    DEFAULT_DESIGN,
    DEFAULT_MODEL,
    HERE,
    MAX_AGENTS,
    MAX_PROMPT_CHARS,
    MEMORY_TOOLS,
    TOOLS,
    chat_model,
    require_api_key,
    run_swarm,
    validate_design,
)

OPTIMIZER_MODEL = os.environ.get("OPTIMIZER_MODEL") or DEFAULT_MODEL
JUDGE_MODEL = os.environ.get("JUDGE_MODEL") or DEFAULT_MODEL
ANSWER_CHARS_FOR_JUDGE = 12000
ANSWER_CHARS_FOR_OPTIMIZER = 3000
PROPOSAL_ATTEMPTS = 3
# Token Factory enforces per-model token quotas; parallel runs hit them quickly.
PARALLEL_RUNS = 2
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_WAIT_SECONDS = 60

_usage_lock = threading.Lock()
usage = {"optimizer": 0, "judge": 0}


def is_rate_limited(error: str | None) -> bool:
    return bool(error) and ("429" in error or "RateLimit" in error)

JUDGE_SYSTEM = """You compare two research answers to the same question.

Judge, in this order of importance:
1. Support: are the key claims backed by sources the team actually opened? Each answer
   comes with the list of URLs its team fetched. Penalise citations to URLs that are not
   in that list, figures with no source, and claims that look fabricated.
2. Correctness and internal consistency of facts, figures and dates.
3. Completeness: does it answer every part of the question?
4. Clarity, concision and honest statement of uncertainty.
Do not prefer an answer just because it is longer. A failed run loses unless both failed.

Reply with only a JSON object:
{"winner": "A" | "B" | "tie", "rationale": "<3-6 sentences naming concrete strengths and flaws of each>"}"""

OPTIMIZER_SYSTEM = f"""You improve the design of a multi-agent research team (a LangGraph swarm).

A design is JSON:
{{"entry": "<agent that receives the question>",
  "agents": [{{"name": "<lowercase id>", "description": "<one line, shown to agents that can hand off to it>",
              "tools": [<subset of {sorted(TOOLS.keys() - MEMORY_TOOLS)}>], "handoffs": ["<other agent names>"],
              "prompt": "<system prompt>"}}]}}

How the swarm runs: one agent is active at a time and sees the whole conversation. It can
call its tools, hand off to an agent in its "handoffs", or finish by replying without a
handoff; that reply is the team's final answer. web_search returns titles, URLs and
snippets; fetch_page returns a page's or PDF's main text; browse_page renders a page in a
headless browser (runs JavaScript, slower) and returns its main text.

Rules: 1-{MAX_AGENTS} agents; unique names matching [a-z][a-z0-9_-]+; prompts under
{MAX_PROMPT_CHARS} characters; every agent reachable from entry; tools only from the list.
A safety note about untrusted web content is appended automatically; do not add one.

Make a focused change that targets the weaknesses in the feedback (prompts, roles, tools,
hand-off structure or number of agents). Do not repeat a change the history shows failed.

Reply with only a JSON object:
{{"changes": ["<short description of each change>"], "rationale": "<why this should win>", "design": {{...}}}}"""


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (tolerates fences and <think> blocks)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object in reply")


def ask(role: str, model_name: str, system: str, user: str) -> str:
    """One optimizer or judge call, waiting out Token Factory rate limits."""
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            reply = chat_model(model_name).invoke([("system", system), ("user", user)])
            break
        except Exception as e:  # provider error types vary; only rate limits are retried
            if not is_rate_limited(f"{type(e).__name__}: {e}") or attempt == RATE_LIMIT_RETRIES:
                raise
            time.sleep(RATE_LIMIT_WAIT_SECONDS * attempt)
    with _usage_lock:
        usage[role] += (reply.usage_metadata or {}).get("total_tokens", 0)
    return reply.content if isinstance(reply.content, str) else str(reply.content)


class Run:
    """Files for one search run under mass_runs/<name>/; reruns resume from them."""

    def __init__(self, name: str, questions: list[dict]):
        self.dir = HERE / "mass_runs" / name
        self.questions = questions
        (self.dir / "designs").mkdir(parents=True, exist_ok=True)
        state_path = self.dir / "state.json"
        if state_path.exists():
            self.state = json.loads(state_path.read_text())
        else:
            shutil.copy(DEFAULT_DESIGN, self.dir / "designs" / "v0.json")
            self.state = {"champion": "v0", "next_version": 1}
            self.save_state()

    def save_state(self) -> None:
        (self.dir / "state.json").write_text(json.dumps(self.state, indent=2))
        shutil.copy(self.design_path(self.state["champion"]), self.dir / "champion.json")

    def design_path(self, version: str) -> Path:
        return self.dir / "designs" / f"{version}.json"

    def design(self, version: str) -> dict:
        return json.loads(self.design_path(version).read_text())

    def history(self) -> list[dict]:
        path = self.dir / "history.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def append_history(self, row: dict) -> None:
        with open(self.dir / "history.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")

    def outputs(self, version: str) -> dict[str, dict]:
        """Run a design on every question, reusing saved results."""
        out_dir = self.dir / "outputs" / version
        out_dir.mkdir(parents=True, exist_ok=True)
        design = self.design(version)

        def one(q: dict) -> tuple[str, dict]:
            path = out_dir / f"{q['id']}.json"
            if path.exists():
                return q["id"], json.loads(path.read_text())
            for attempt in range(1, RATE_LIMIT_RETRIES + 1):
                start = time.time()
                # No memory here: facts saved in one run would leak into later comparisons.
                result = run_swarm(design, q["question"], use_memory=False)
                result["seconds"] = round(time.time() - start)
                if not is_rate_limited(result["error"]):
                    break
                wait = RATE_LIMIT_WAIT_SECONDS * attempt
                print(f"    {version}/{q['id']}: rate-limited by Token Factory, retrying in {wait}s")
                time.sleep(wait)
            else:
                # An infrastructure failure must not be scored as a design failure.
                raise RuntimeError(f"{version}/{q['id']}: still rate-limited; nothing was scored")
            path.write_text(json.dumps(result, indent=1))
            status = "FAILED" if result["error"] else "ok"
            print(f"    {version}/{q['id']}: {status}, {result['searches']} searches, "
                  f"{len(result['fetched_urls'])} pages, {result['total_tokens']} tokens, "
                  f"{result['seconds']}s")
            return q["id"], result

        with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
            return dict(pool.map(one, self.questions))


def judge(question: str, a: dict, b: dict) -> dict:
    def block(label: str, r: dict) -> str:
        urls = "\n".join(f"- {u}" for u in r["fetched_urls"]) or "- (none)"
        return (f"=== Answer {label} ===\n{r['answer'][:ANSWER_CHARS_FOR_JUDGE]}\n\n"
                f"URLs team {label} actually fetched:\n{urls}\n")

    user = f"Question: {question}\n\n{block('A', a)}\n{block('B', b)}"
    try:
        verdict = extract_json(ask("judge", JUDGE_MODEL, JUDGE_SYSTEM, user))
    except ValueError as e:  # an unparseable verdict counts as a tie
        return {"winner": "tie", "rationale": f"(judge reply unparseable: {e})"}
    if verdict.get("winner") not in ("A", "B", "tie"):
        verdict["winner"] = "tie"
    return verdict


def compare(run: Run, champion: str, candidate: str) -> tuple[float, list[dict]]:
    """Score the candidate against the champion: 1 win, 0.5 tie, 0 loss, averaged."""
    champ_out, cand_out = run.outputs(champion), run.outputs(candidate)
    jobs = []
    for q in run.questions:
        jobs.append((q, "candidate_is_A", cand_out[q["id"]], champ_out[q["id"]]))
        jobs.append((q, "candidate_is_B", champ_out[q["id"]], cand_out[q["id"]]))

    def one(job):
        q, order, a, b = job
        verdict = judge(q["question"], a, b)
        cand_label = "A" if order == "candidate_is_A" else "B"
        score = 0.5 if verdict["winner"] == "tie" else float(verdict["winner"] == cand_label)
        return {"question": q["id"], "order": order, "candidate_score": score, **verdict}

    with ThreadPoolExecutor(max_workers=PARALLEL_RUNS) as pool:
        judgments = list(pool.map(one, jobs))
    return sum(j["candidate_score"] for j in judgments) / len(judgments), judgments


def propose(run: Run, champion: str) -> tuple[dict, dict]:
    outputs = run.outputs(champion)
    answers = "\n\n".join(
        f"--- {q['id']}: {q['question']}\n"
        f"[{outputs[q['id']]['searches']} searches, {len(outputs[q['id']]['fetched_urls'])} pages "
        f"fetched, {outputs[q['id']]['total_tokens']} tokens"
        f"{', ERROR: ' + outputs[q['id']]['error'] if outputs[q['id']]['error'] else ''}]\n"
        f"{outputs[q['id']]['answer'][:ANSWER_CHARS_FOR_OPTIMIZER]}"
        for q in run.questions
    )
    history = run.history()
    latest = next((h for h in reversed(history) if champion in (h["champion"], h["candidate"])), None)
    feedback_lines = []
    for j in latest["judgments"] if latest else []:
        # Express each verdict from the current champion's side of that comparison.
        champ_was_candidate = latest["candidate"] == champion
        champ_score = j["candidate_score"] if champ_was_candidate else 1 - j["candidate_score"]
        champ_label = ("A" if j["order"] == "candidate_is_A" else "B") if champ_was_candidate \
            else ("B" if j["order"] == "candidate_is_A" else "A")
        outcome = {1.0: "won", 0.0: "lost", 0.5: "tied"}[champ_score]
        feedback_lines.append(
            f"- {j['question']}: champion (Answer {champ_label}) {outcome}. {j['rationale']}"
        )
    feedback_text = "\n".join(feedback_lines) or "(no comparisons yet)"
    history_text = "\n".join(
        f"- {h['candidate']} vs {h['champion']}: score {h['score']:.2f}, "
        f"{'ACCEPTED' if h['accepted'] else 'rejected'}; changes: {'; '.join(h['changes'])}"
        for h in history
    ) or "(none)"

    user = (
        f"Current champion design ({champion}):\n{json.dumps(run.design(champion), indent=1)}\n\n"
        f"Champion's answers (truncated):\n{answers}\n\n"
        f"Judge feedback from the latest comparison involving the champion:\n{feedback_text}\n\n"
        f"History of earlier attempts:\n{history_text}"
    )
    error = None
    for _ in range(PROPOSAL_ATTEMPTS):
        prompt = user if not error else f"{user}\n\nYour previous reply was invalid: {error}. Fix it."
        try:
            proposal = extract_json(ask("optimizer", OPTIMIZER_MODEL, OPTIMIZER_SYSTEM, prompt))
            validate_design(proposal["design"])
            return proposal["design"], proposal
        except (ValueError, KeyError, TypeError) as e:
            error = str(e)
            print(f"    proposal invalid ({error}); retrying")
    raise RuntimeError(f"optimizer gave no valid design after {PROPOSAL_ATTEMPTS} attempts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", default="demo", help="run name under mass_runs/")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--questions", type=Path, default=HERE / "questions.json")
    args = parser.parse_args()

    require_api_key()
    questions = json.loads(args.questions.read_text())["train"]
    run = Run(args.run, questions)
    print(f"MASS-lite run '{args.run}': {len(questions)} questions, models: "
          f"executor={DEFAULT_MODEL} optimizer={OPTIMIZER_MODEL} judge={JUDGE_MODEL}")

    for _ in range(args.iterations):
        champion = run.state["champion"]
        candidate = f"v{run.state['next_version']}"
        print(f"\n== {candidate} vs champion {champion}")
        print("  running champion (cached after first time)")
        run.outputs(champion)

        print("  proposing candidate")
        design, proposal = propose(run, champion)
        run.design_path(candidate).write_text(json.dumps(design, indent=1))
        for change in proposal.get("changes", []):
            print(f"    change: {change}")

        print("  running candidate")
        score, judgments = compare(run, champion, candidate)
        accepted = score > 0.5
        run.append_history({
            "champion": champion, "candidate": candidate, "score": score, "accepted": accepted,
            "changes": proposal.get("changes", []), "rationale": proposal.get("rationale", ""),
            "judgments": judgments, "overhead_tokens": dict(usage),
        })
        print(f"  optimizer+judge tokens so far: {usage}")
        run.state["next_version"] += 1
        if accepted:
            run.state["champion"] = candidate
        run.save_state()
        print(f"  score {score:.2f} -> {'ACCEPTED, new champion ' + candidate if accepted else 'rejected'}")

    champion = run.state["champion"]
    with open(run.dir / "champion_trajectories.jsonl", "w") as f:
        for q_id, result in run.outputs(champion).items():
            f.write(json.dumps({"question_id": q_id, **result}) + "\n")
    print(f"\nChampion: {champion} -> {run.dir / 'champion.json'}")
    print(f"Training-ready traces: {run.dir / 'champion_trajectories.jsonl'}")


if __name__ == "__main__":
    main()
