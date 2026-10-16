#!/usr/bin/env bash
# Run things inside a devlab's workload container from your own machine.
#   dl.sh sh '<command>'          run a shell command in $DEVLAB_DIR (stdin is not forwarded)
#   dl.sh put <local> <remote>    copy a local file to $DEVLAB_DIR/<remote>
#   dl.sh py < script.py          run a Python script from stdin with the project's uv venv
#
# Settings (environment):
#   DEVLAB_HOST       SSH target, e.g. nebius@203.0.113.10 (the devlab VM's public IP)
#   DEVLAB_CONTAINER  workload container, e.g. devlab-<devlab-id>-devlab-1
#   DEVLAB_SSH_KEY    private key authorised with --ssh-key when the devlab was created
#   DEVLAB_DIR        working directory in the container (default /workspace/swarm)
set -euo pipefail
: "${DEVLAB_HOST:?set DEVLAB_HOST}" "${DEVLAB_CONTAINER:?set DEVLAB_CONTAINER}"
KEY=${DEVLAB_SSH_KEY:-$HOME/.ssh/id_ed25519}
DIR=${DEVLAB_DIR:-/workspace/swarm}
SSH=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -i "$KEY" "$DEVLAB_HOST")

case "${1:-}" in
  sh)  "${SSH[@]}" "sudo docker exec $DEVLAB_CONTAINER sh -c $(printf '%q' "cd $DIR && $2")" ;;
  put) "${SSH[@]}" "sudo docker exec -i $DEVLAB_CONTAINER sh -c $(printf '%q' "mkdir -p \$(dirname $DIR/$3) && cat > $DIR/$3")" < "$2" ;;
  py)  "${SSH[@]}" "sudo docker exec -i $DEVLAB_CONTAINER sh -c $(printf '%q' "cd $DIR && uv run python -")" ;;
  *)   echo "usage: dl.sh sh|put|py" >&2; exit 2 ;;
esac
