# syntax=docker/dockerfile:1
# Sandbox image for builder.py: OpenCode + the Anatomy skill, run inside NVIDIA OpenShell.
# Build from this folder after ./fetch-anatomy-skill.sh:  docker build -t swarm-builder:1 .
FROM node:22-bookworm-slim

# iproute2 and nftables are required by OpenShell's network namespace setup.
# Chromium and a font let the Anatomy skill's checks take screenshots offline.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates iproute2 nftables chromium fonts-dejavu-core ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g opencode-ai@1.19.2 && npm cache clean --force

RUN groupadd --gid 1500 sandbox \
    && useradd --uid 1500 --gid sandbox --create-home sandbox \
    && install -d -o sandbox -g sandbox /sandbox /sandbox/work /sandbox/work/out

COPY --chown=sandbox:sandbox skills/anatomy /home/sandbox/.config/opencode/skills/anatomy
COPY --chown=sandbox:sandbox opencode.json /sandbox/work/opencode.json

ENV CHROME_PATH=/usr/bin/chromium \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1

USER sandbox:sandbox
WORKDIR /sandbox/work

# OpenShell replaces the command; this keeps the image usable on its own.
CMD ["sleep", "infinity"]
