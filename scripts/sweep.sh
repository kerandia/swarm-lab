#!/usr/bin/env bash
# Sweep billable Nebius resources in each project passed as an argument.
N=~/.nebius/bin/nebius
export NO_COLOR=1

CMDS=(
  "compute instance list"
  "compute disk list"
  "compute filesystem list"
  "compute gpu-cluster list"
  "compute disk-snapshot list"
  "compute image list"
  "mk8s cluster list"
  "storage bucket list"
  "vpc allocation list"
  "vpc network list"
  "msp postgresql v1alpha1 cluster list"
  "msp mlflow v1alpha1 cluster list"
  "registry list"
  "ai endpoint list"
  "ai job list"
)

for p in "$@"; do
  echo "################ $p"
  for c in "${CMDS[@]}"; do
    out=$($N $c --parent-id "$p" --format json 2>&1)
    if echo "$out" | grep -q '^{'; then
      summary=$(echo "$out" | python3 -I -c '
import json,sys
d=json.load(sys.stdin)
items=d.get("items",[]) or []
names=[(i.get("metadata",{}).get("name") or i.get("metadata",{}).get("id")) for i in items]
print(len(items), ", ".join(names[:10]))
')
      echo "  $c: $summary"
    else
      echo "  $c: ERR $(echo "$out" | grep -m1 -oE 'PermissionDenied|NotFound|Unimplemented|unknown|Error[^\n]{0,80}')"
    fi
  done
done
