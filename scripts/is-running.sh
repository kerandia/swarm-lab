#!/bin/sh
# Print "running" if an OpenCode exec client for sandbox $1 is alive, else "stopped".
for p in /proc/[0-9]*; do
  [ "${p#/proc/}" = "$$" ] && continue
  cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)
  case "$cmd" in
    *"sandbox exec --name $1 "*"opencode run"*) echo running; exit 0 ;;
  esac
done
echo stopped
