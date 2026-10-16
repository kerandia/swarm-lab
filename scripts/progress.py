"""Summarise an OpenCode --format json event log: time, tool use, tokens, cost, last text."""

import json
import sys
from collections import Counter
from datetime import datetime, timezone

path = sys.argv[1] if len(sys.argv) > 1 else "run.jsonl"
tools, errors, files = Counter(), [], set()
tokens = cost = 0
first = last = None
last_text = ""
for line in open(path):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    ts = event.get("timestamp")
    first = first or ts
    last = ts or last
    part = event.get("part", {})
    if event.get("type") == "tool_use":
        state = part.get("state", {})
        tools[part.get("tool")] += 1
        if state.get("status") == "error":
            errors.append(f"{part.get('tool')}: {str(state.get('error'))[:120]}")
        target = state.get("input", {}).get("filePath")
        if part.get("tool") in ("write", "edit") and target:
            files.add(target)
    elif event.get("type") == "step_finish":
        tokens += part.get("tokens", {}).get("total", 0)
        cost += part.get("cost", 0) or 0
    elif event.get("type") == "text":
        last_text = part.get("text", "")

minutes = (last - first) / 60000 if first and last else 0
print(f"elapsed {minutes:.1f} min | tool calls {sum(tools.values())} | tokens {tokens:,} | cost ${cost:.2f}")
print("tools:", dict(tools.most_common()))
print("files written:", sorted(files))
print(f"tool errors ({len(errors)}):", errors[-5:])
print("last text:", last_text[-600:].replace("\n", " "))
if last:
    print("last event:", datetime.fromtimestamp(last / 1000, timezone.utc).strftime("%H:%M:%SZ"))
