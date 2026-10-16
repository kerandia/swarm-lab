# Field notes: how a Nebius devlab works (verified by inspection, 16 Oct 2026)

Observed on devlab `alp-swarm-lab` in a Nebius workshop project (region eu-west2).
Everything below was seen directly via the Nebius CLI, the API, or SSH on the VM.

## What it is
- A Devlab is Nebius AI Cloud's managed interactive development environment
  (`nebius ai devlab create|stop|restart|delete|logs|ssh`). It is part of Nebius
  Serverless: its tunnel is described as "Managed automatically by Serverless" and
  belongs to an internal "appbox" resource.
- Created from a template (14 global templates: JupyterLab, vLLM, SGLang, Ollama,
  Open WebUI, ComfyUI, n8n, Flowise, Coder Server, LangChain, LlamaIndex,
  NVIDIA AI Workbench, NVIDIA Transaction FM, Deep Research with OpenShell) or from any
  container image. A Devlab can also be created from a failed AI Job to debug it.
- Lifecycle states seen: PROVISIONING -> IMAGE_PULLING -> STARTING -> RUNNING;
  also STOPPED and DELETING. Creating this one took about 4 minutes.

## One devlab = one dedicated VM
- Each devlab gets its own Compute VM. This one: platform `cpu-d3`, preset
  `4vcpu-16gb` (4 vCPU, 16 GB RAM), 100 GiB network SSD boot disk, a public IPv4
  address and a private address in the project's default subnet.
- GPU devlabs use GPU platforms (in eu-west2: `gpu-b300-sxm`, presets 1 GPU/24 vCPU/346 GB
  or 8 GPU/192 vCPU/2768 GB).

## What runs on the VM (a Docker Compose project written by the service)
1. **Workload container** (`devlab`): the template image, here
   `openshell-devlab:0.1.1-nebius.5` (Debian 12, Python 3.12, uv, JupyterLab on port 8888).
   Runs **privileged** (so Docker-in-Docker works for the OpenShell sandbox). Mounts the
   persistent workspace volume at `/workspace`.
2. **Auth proxy** (`devlab__auth_proxy_v1`, Envoy): publishes port 8888 and puts a login in
   front of Jupyter. Without a session: `/lab` -> 302 to `/login`, `/api/kernels` -> 403.
3. **Tunnel agent** (`_tunna_agent`, `nebius-tunnel-agent:0.3.0`, host network): dials
   out to the Nebius edge `tunnel.applications.eu-west2.nebius.cloud:8443` and exposes
   service `port8888` -> `127.0.0.1:8888` as
   `https://port8888-<masked-tunnel-id>.tunnel.applications.eu-west2.nebius.cloud`.
   It authenticates with the VM's own service account through the instance metadata
   service (no key files). It opens 3 edge connections for redundancy.
4. **Observability agent** (`nebius-vmapp-observability-agent`): ships logs and metrics
   (`nebius ai devlab logs`).
5. **Health-check containers** (small alpine containers) that report readiness.

## Persistence
- `/workspace` is a Docker volume on the VM's boot disk. Code, packages, runs and the
  Jupyter kernel spec live there and survive stop/restart. Everything else in the
  container's filesystem can be lost if the container is recreated.

## Access paths
- HTTPS through the tunnel (works without opening any inbound port).
- The public IP on port 8888 (through the Envoy login).
- SSH with a key as user `nebius` on the VM; `nebius ai devlab ssh` = SSH to the VM, then
  `docker exec` into the workload container (needs an interactive terminal).
- Network: the project's default security group allows all inbound and outbound traffic,
  so the login proxy and key-only SSH are what protect the VM. Devlabs in the same project
  share one private subnet.

## The sandbox inside: NVIDIA OpenShell (Deep Research template)
- The template's research agent runs inside an OpenShell sandbox nested in the workload
  container. Policy file `openshell/policy.yaml`:
  - runs as non-root user `sandbox`;
  - Landlock filesystem rules (hard requirement): read-only `/usr /lib /etc /proc /sandbox`,
    read-write only `/sandbox/runs`, `/tmp`;
  - network allowlist, default deny: only `POST api.studio.nebius.ai/v1/chat/completions`
    (Nebius Token Factory) and `POST api.tavily.com/search`, and only from the
    `python3.12` binary;
  - credentials are bound to those endpoints by the sandbox proxy ("credential binding"),
    so the agent code does not hold the keys.
- The notebook workspace itself is "trusted" and not restricted by that policy.

## Models: Nebius Token Factory
- OpenAI-compatible API at `https://api.studio.nebius.ai/v1/`; the workshop key could use
  27 models (MiniMax-M3, Kimi-K3, DeepSeek-V4-Pro, GLM-5.3, Qwen3.5-397B, gpt-oss-120b,
  Nemotron-3-Ultra and others). Per-model token quotas return HTTP 429 when exceeded.

## Tunnels (Nebius Tunnels, preview, free during preview)
- A tunnel is a cloud resource; an agent near the service connects out to the edge and
  declares services (name -> local host:port, type http or tcp). Each service gets
  `https://<service>-<masked-id>.tunnel.applications.<region>.nebius.cloud`.
- The agent's service account needs the `applicationtunnel.agent` role.
- We created a second tunnel and agent on the same VM to publish results from
  `/workspace/swarm/public` via nginx bound to 127.0.0.1:8080.

## Prices (Nebius calculator, list prices per hour)
- cpu-d3 2 vCPU/8 GB: $0.066; 4 vCPU/16 GB: $0.132; 16 vCPU/64 GB: $0.528
- 100 GiB network SSD: about $0.0097
- gpu-b300-sxm 1 GPU: $9.50; 8 GPUs: $76
- Billing is per running VM; a stopped devlab keeps its disk.

## Scale seen in the workshop project
- About 90 devlabs, one or more per attendee: 77 RUNNING at one moment, the rest
  provisioning, starting or pulling images. 89 used the Deep Research template on CPU
  (mostly 2 or 4 vCPU), one ran on a single B300 GPU.
