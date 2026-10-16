## Context

A one-day session (16 Oct 2026) in a Nebius workshop project (GTC devlab workshop, region eu-west2), driven from Claude Code. Goal: understand Nebius devlabs and build an agent system on one: research agents, memory, a web interface, a sandboxed builder and hosted results.

## What we built

- **Research swarm** (LangGraph Swarm): planner → researcher ⇄ critic. Tools: keyless web search, page/PDF reader, headless browser (Playwright), SQLite memory (sourced facts + run log, FTS5), and a SubQ Snap claim checker. Models from Nebius Token Factory: Kimi-K3 for planner and critic, DeepSeek-V4-Pro for the researcher (different families, so the critic catches more).
- **MASS-lite**: a small take on SakanaAI's MASS. An optimizer rewrites the team design, a judge compares answers in both orders, the winner is kept, and every run is saved as a training-ready trace.
- **Web interface** (FastAPI): ask a question, watch each agent step live, read cited answers, search memory. Published through a Nebius Tunnel behind an access code, with limits (one run at a time, daily cap, token budget per question).
- **Builder**: OpenCode with the Anatomy skill inside an NVIDIA OpenShell sandbox (network allowlist: Token Factory chat endpoint and npm reads only; the API key is injected by the sandbox proxy and never enters the sandbox). A build job turns a swarm answer into a published page.
- **Hosting**: nginx bound to localhost plus our own Nebius Tunnel for public result pages.

## What a devlab is

- A managed dev environment = one dedicated VM running a Docker Compose stack: the template's workload container (privileged, for Docker-in-Docker), an Envoy login proxy, a tunnel agent (dials out, 3 edge connections, no inbound port), an observability agent and health checks.
- `/workspace` persists across stop/restart; billing is per running hour (4 vCPU / 16 GB: about $0.132/h plus disk).
- It is not a sandbox by itself: the project's default security group allows all traffic, and the container is privileged. The real sandbox is NVIDIA OpenShell inside it (Landlock, non-root, default-deny network, credential binding).

## Learnings

1. **Compute was cheap; model tokens were not.** The VM cost about $0.14/h. Two autonomous OpenCode builds on Kimi-K3 spent about $41 in 30 minutes, after which the workshop's Token Factory key was disabled (HTTP 401 on every model). Set token budgets and pick models by price: MiniMax-M3 is about 10× cheaper than Kimi-K3.
2. **Rate limits corrupt evaluations.** In MASS-lite, two baseline runs failed on HTTP 429 and were scored as losses, so the first "improvement" was an artefact. Retry infrastructure errors and never score them.
3. **Agents report their own process.** Critics wrote "Let me finalize…" into answers; wrapping the final answer in tags fixed it. Agents also cited pages they had only seen in search snippets, so log which pages were actually opened and check citations against that list.
4. **Review before publishing.** The swarm's report needed three factual corrections (a non-existent product name, a wrong claim about defaults, a misleading comparison). An automatic build published a page that hung the browser, so every page now goes through an offline headless check (hang, script errors, phone overflow) before it is published.
5. **Long reasoning can end a run.** Kimi-K3 spent its whole 32k output allowance on hidden reasoning in one step, which ended OpenCode's non-interactive run. Detect the "length" stop and resume the session.
6. **Sandbox details matter.** OpenShell sets HOME to the work directory (so OpenCode didn't find skills), OpenCode needs ripgrep locally (its download is blocked), and npm scoped packages need encoded slashes allowed. The audit log showed every allowed and denied connection, which made debugging fast.
7. **Be explicit about who made what.** A Claude helper built the first figure; the user wanted the system to build it, so that figure was discarded and the system's own build was run. Attribution blocks now say who made each part.
8. **Guard public endpoints that spend someone else's credits.** The web interface kept an access code and limits even though fully open was requested, because one deep question could use over a million tokens.

## Platform comparison (about 4 vCPU, per hour)

- Nebius devlab: about $0.14/h always while running, GPUs (B300 $9.50/h), EU regions, persistent disk. Best as an always-on agent home.
- Cloudflare Sandbox SDK: about $0.40/h at full CPU, about $0.14/h mostly idle; no GPUs; max 4 vCPU / 12 GiB.
- Vercel Sandbox: about $0.68/h at full CPU, about $0.22/h mostly idle; Firecracker microVMs, sessions up to 24 h.
- Modal Sandboxes: about $0.67/h; GPUs; Python-first.
- RunPod: cheapest raw GPUs (H100 about $1.99–2.89/h).

## Next steps

- Get a working Token Factory key (own account), switch builds to a cheaper model, then let the system finish its own Anatomy figure by resuming the same OpenCode session.
- Try Prime Agent (self-improving harness) as a challenger to the swarm + OpenCode pipeline on the same task, comparing quality and cost.
- Move the always-on setup to a permanent tenant; the workshop project is temporary.
