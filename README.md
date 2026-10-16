# swarm-lab

A research agent swarm with memory, fact-checking, a live web interface and a sandboxed page
builder, built on a Nebius AI Cloud devlab with open models from Nebius Token Factory.

```
   you ── web interface (FastAPI, behind an access code, published via a Nebius tunnel)
                │ question
   ┌────────────▼──────────────────────────────┐
   │ swarm/: LangGraph Swarm research team     │
   │   planner → researcher ⇄ critic           │
   │   web search · page reader · headless     │
   │   browser · SQLite memory · SubQ checker  │
   └────────────┬──────────────────────────────┘
                │ cited answer
   ┌────────────▼──────────────────────────────┐
   │ builder: OpenCode + Anatomy skill inside  │
   │ an NVIDIA OpenShell sandbox               │
   │ (network: model API + npm reads only)     │
   └────────────┬──────────────────────────────┘
                │ page, checked offline before publishing
         swarm/public/ → nginx → Nebius tunnel

   MASS-lite (swarm/mass_lite.py): an occasional tuner that rewrites the team design
   and keeps whichever version a judge model prefers.
```

| Folder | What's in it |
|---|---|
| `swarm/` | The research team, tools, memory, MASS-lite, web interface (`app.py`, `ui/`) and page builder (`builder.py`). See [swarm/README.md](swarm/README.md). |
| `builder-image/` | Docker image, OpenShell policy, provider profile and OpenCode config for the page builder. The Anatomy skill is fetched at a pinned commit, not vendored. |
| `scripts/` | `dl.sh` (run commands in a devlab container over SSH), `sweep.sh` (list billable Nebius resources per project), tunnel agent example, build-progress helpers. |
| `docs/` | [Learnings](docs/learnings.md) from building this, and [field notes](docs/devlab-field-notes.md) on how a devlab works inside. |

## What you need

- A Nebius AI Cloud project and the [Nebius CLI](https://docs.nebius.com/cli/) (`nebius profile create`).
- A Nebius Token Factory API key. Optional: a SubQ API key for `verify_claims`.
- A devlab. The **Deep Research with OpenShell** template already provides the OpenShell CLI,
  gateway and Docker-in-Docker that the builder needs.

## Set up on a devlab

1. **Create the devlab** (CPU is enough; the template asks for keys, which can be placeholders
   because the swarm reads its own `.env`):

   ```sh
   nebius ai devlab create --parent-id <project-id> --name swarm-lab \
     --template vmapptemplate-g00devlab-openshell-research \
     --platform cpu-d3 --preset 4vcpu-16gb --disk-size 100Gi --public \
     --ssh-key "$(cat ~/.ssh/id_ed25519.pub)" \
     --input jupyter_token=<16-alphanumeric> --input nebius_api_key=not-set --input tavily_api_key=not-set
   ```

   Find the VM's public IP and the workload container name with
   `nebius ai devlab get --id <devlab-id>`; the SSH user is `nebius` and the container is
   `devlab-<devlab-id>-devlab-1`. Set `DEVLAB_HOST`, `DEVLAB_CONTAINER` and `DEVLAB_SSH_KEY`
   for `scripts/dl.sh`.

2. **Copy the code** into `/workspace` (persistent across restarts) and install it:

   ```sh
   cd /workspace/swarm-lab/swarm
   uv sync
   cp .env.example .env        # fill in NEBIUS_API_KEY, SWARM_UI_CODE, ...
   uv run playwright install --with-deps chromium
   ```

3. **Ask a question:** `uv run python swarm.py "..."`.

4. **Web interface:** `sh restart-ui.sh`, then publish it and `swarm/public/` with a Nebius
   tunnel and nginx as shown in [scripts/tunnel-agent.example.yaml](scripts/tunnel-agent.example.yaml).
   Set `SWARM_PUBLIC_BASE` to the `results` service address.

5. **Page builder** (inside the devlab container, where `DOCKER_HOST` points at OpenShell's daemon):

   ```sh
   cd /workspace/swarm-lab/builder-image
   ./fetch-anatomy-skill.sh && docker build -t swarm-builder:1 .
   openshell profile import --file token-factory-profile.yaml
   NEBIUS_API_KEY=... openshell provider create --name swarm-builder-nebius \
     --type swarm-token-factory --credential NEBIUS_API_KEY
   ```

## Costs and safety

- **Model tokens dominate.** The devlab VM costs about $0.14/h; one autonomous page build on
  Kimi-K3 used about 4.5M tokens (~$15) in 25 minutes. Use budgets (`SWARM_UI_TOKEN_BUDGET`,
  `SWARM_UI_DAILY_BUILDS`, `SWARM_BUILD_MINUTES`) and consider a cheaper model for builds.
- **A devlab is not a sandbox.** The workload container is privileged and the default security
  group allows all traffic. Keep the access code on the web interface, keep nginx on localhost,
  and run anything agent-written inside OpenShell.
- **Outputs need checking.** Answers are AI-generated from web pages; the builder refuses to
  publish pages that fail its offline checks, but nobody reviews them.

## Credits

[LangGraph Swarm](https://github.com/langchain-ai/langgraph-swarm-py),
[SakanaAI MASS](https://github.com/SakanaAI/mass) (inspiration for MASS-lite),
[Anatomy](https://github.com/wheresryan22/anatomy) (MIT, fetched at build time),
[OpenCode](https://opencode.ai), [NVIDIA OpenShell](https://docs.nvidia.com/openshell/),
[SubQ](https://docs.subq.ai), [Nebius AI Cloud](https://docs.nebius.com).

## License

MIT, see [LICENSE](LICENSE). The Anatomy skill fetched by `builder-image/fetch-anatomy-skill.sh` is MIT-licensed by its authors.
