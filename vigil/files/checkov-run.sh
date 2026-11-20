#!/bin/sh
# Runs inside the checkov container: one report per repo, /reports/code-<name>.json
# Repos: /src/<name> (fetched by vigil-code-fetch). Frameworks: CHECKOV_FRAMEWORKS in code.env.
# --soft-fail: failed checks are findings, not an error. Exits non-zero if checkov itself fails.
set -u
. /vigil-conf/code.env

rc=0
for dir in /src/*/; do
  [ -d "$dir" ] || continue
  name=$(basename "$dir")
  out="/reports/code-$name.json"
  rm -f "$out" "$out.tmp"
  echo "checkov: scanning $name"
  # shellcheck disable=SC2086  # CHECKOV_FRAMEWORKS is a space-separated list
  if checkov -d "$dir" --framework $CHECKOV_FRAMEWORKS -o json --soft-fail --quiet --compact \
       --skip-download > "$out.tmp"; then
    # One framework prints an object, several print a list. A framework with nothing to scan
    # prints a bare summary without "results"; drop those. Nothing left = empty report.
    python3 - "$out.tmp" "$out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
runs = [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict) and "results" in r]
with open(sys.argv[2], "w") as f:
    if runs:
        json.dump(runs if len(runs) > 1 else runs[0], f)
PY
    rm -f "$out.tmp"
    echo "checkov: $name done"
  else
    echo "checkov: $name failed (exit $?)"
    rm -f "$out" "$out.tmp"
    rc=1
  fi
done
exit $rc
