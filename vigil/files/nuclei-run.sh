#!/bin/sh
# Runs inside the nuclei container: one report per scope, /reports/<scope>.jsonl
# Targets: /vigil-conf/nuclei/<scope>.txt (CIDRs, hosts or URLs), exclusions in exclude.txt
# Exits non-zero if templates are missing or any scope fails, so the unit fails and the
# import (OnSuccess=) doesn't archive an empty report as "no findings".
set -u
. /vigil-conf/scan.env

# No -duc here: it turns -update-templates into a silent no-op (exit 0, zero templates).
nuclei -update-templates -silent || echo "nuclei: template update failed, using cached templates"
templates=$(find "$HOME/nuclei-templates" -name '*.yaml' 2>/dev/null | wc -l)
if [ "$templates" -eq 0 ]; then
  echo "nuclei: no templates in $HOME/nuclei-templates, not scanning"
  exit 1
fi
echo "nuclei: $templates templates"

rc=0
for list in /vigil-conf/nuclei/*.txt; do
  scope=$(basename "$list" .txt)
  [ -s "$list" ] || continue
  out="/reports/$scope.jsonl"
  rm -f "$out"
  set -- -l "$list" -jsonl -o "$out" -silent -nc -duc \
         -severity "$NUCLEI_SEVERITY" -rl "$NUCLEI_RATE_LIMIT" -c "$NUCLEI_CONCURRENCY"
  [ -s /vigil-conf/exclude.txt ] && set -- "$@" -eh /vigil-conf/exclude.txt
  echo "nuclei: scanning $scope"
  if nuclei "$@"; then
    [ -f "$out" ] || : > "$out"          # ran fine, found nothing
    echo "nuclei: $scope done"
  else
    echo "nuclei: $scope failed (exit $?)"
    rm -f "$out"
    rc=1
  fi
done
exit $rc
