#!/bin/sh
# Runs inside the semgrep container: one report per repo, /reports/code-<name>.json
# Repos: /src/<name> (fetched by vigil-code-fetch). Rulesets: SEMGREP_CONFIGS in code.env.
# Exits non-zero if any repo fails, so the import (OnSuccess=) doesn't run on a partial set.
set -u
. /vigil-conf/code.env

rc=0
for dir in /src/*/; do
  [ -d "$dir" ] || continue
  name=$(basename "$dir")
  out="/reports/code-$name.json"
  rm -f "$out"
  set --
  for c in $SEMGREP_CONFIGS; do set -- "$@" --config "$c"; done
  echo "semgrep: scanning $name"
  if semgrep scan "$@" --json --output "$out" --metrics=off --disable-version-check --quiet "$dir"; then
    echo "semgrep: $name done"
  else
    echo "semgrep: $name failed (exit $?)"
    rm -f "$out"
    rc=1
  fi
done
exit $rc
