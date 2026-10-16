#!/bin/sh
# Restart the web interface (FastAPI on port 8090) in the background.
# The container has no pkill/ps, so find the old server through /proc.
cd "$(dirname "$0")" || exit 1
for p in /proc/[0-9]*; do
  pid=${p#/proc/}
  [ "$pid" = "$$" ] && continue
  if tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -q "uvicorn app:app"; then
    kill "$pid" && echo "stopped old server ($pid)"
  fi
done
sleep 2
setsid nohup uv run uvicorn app:app --host 0.0.0.0 --port 8090 > ui.log 2>&1 &
sleep 7
tail -1 ui.log
