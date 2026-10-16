# swarm

The research team, its tools and memory, MASS-lite, the web interface and the page builder.
Setup and deployment are in the [top-level README](../README.md).

## Ask the team a question

```sh
uv run python swarm.py "How did EU heat pump sales change in 2025?"
uv run python swarm.py --context notes.md --out report.md "question"   # give notes, save the answer
uv run python swarm.py --design mass_runs/demo/champion.json "..."      # use an evolved design
uv run python swarm.py --graph                                         # agent graph, no key needed
```

Team designs are JSON files: agents, their model, prompt, tools and hand-offs.
`designs/v0.json` is planner → researcher ⇄ critic, with `moonshotai/Kimi-K3` for the
planner and critic and `deepseek-ai/DeepSeek-V4-Pro` for the researcher (a different model
family for the critic catches more of the researcher's mistakes). The critic wraps its final
answer in `<answer>` tags so process notes don't leak into it.

## Tools

| Tool | What it does |
|---|---|
| `web_search` | Keyless web search (ddgs) |
| `fetch_page` | Reads a page or PDF and returns its main text |
| `browse_page` | Renders a page in headless Chromium (JavaScript) and returns its text |
| `verify_claims` | Checks up to 16 claims against one source in a single SubQ Snap call: supported / contradicted / not_in_source, with a confidence |
| `recall` | Searches facts saved by earlier runs |
| `remember` | Saves a fact confirmed in a page the agent opened, with its URL |

The web tools refuse localhost, private-network and cloud-metadata addresses (also on every
redirect and every browser sub-request) and respect robots.txt. Agents with these tools are
told to treat page text as data, never as instructions. `verify_claims` needs `SUBQ_API_KEY`
and sends only public page text and the claims to SubQ.

## Memory

`memory.py` keeps `memory.db` (SQLite) with `facts` (each fact with its source URL, FTS5
search) and `runs` (question, answer, team, tokens, sources). MASS-lite runs without memory
so facts from one round can't leak into the next.

## MASS-lite

```sh
uv run python mass_lite.py --run demo --iterations 3
```

Each round an optimizer proposes a new design, both designs answer `questions.json`, a judge
compares the answers in both orders, and the candidate becomes champion only if it scores above
0.5. Rate-limited runs are retried and never scored. Every run's trajectory is saved in
`mass_runs/<run>/`, plus `champion_trajectories.jsonl` for a later fine-tuning step.

## Web interface

`app.py` (FastAPI) serves `ui/index.html`: ask a question, watch each agent step live, read the
cited answer, reopen past questions, search memory, and build pages. Start or restart it with
`sh restart-ui.sh` (port 8090). Guards from `.env`: `SWARM_UI_CODE` (access code for every API
call), `SWARM_UI_TOKEN_BUDGET` (tokens per question), `SWARM_UI_DAILY_RUNS`. One question runs
at a time.

## Page builder

`builder.py` turns an answer into a web page with no manual steps (the **Also build a web page**
checkbox, or **Build a page from this answer**). Each job:

1. creates a fresh NVIDIA OpenShell sandbox from the builder image with `../builder-image/policy.yaml`
   (network: Token Factory chat completions and npm reads only; the key is injected by the
   OpenShell provider and never enters the sandbox);
2. uploads `TASK.md` and the answer as `brief/report.md`;
3. runs `opencode run` with the Anatomy skill for up to `SWARM_BUILD_MINUTES`, resuming the same
   session up to 3 times if the model stops early (out of output room, or no notes written);
4. downloads `out/` and loads the page offline in headless Chromium; a hang, script error,
   phone-width overflow or missing figure means it is **not published**;
5. otherwise publishes `out/index.html` at `$SWARM_PUBLIC_BASE/pages/<slug>/`, then deletes the sandbox.

One build at a time, `SWARM_UI_DAILY_BUILDS` per UTC day. Job state is in `builds/<id>/`.
Builds are expensive: one Kimi-K3 build used about 4.5M tokens in 25 minutes.
