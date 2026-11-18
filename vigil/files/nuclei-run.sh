#!/bin/sh
# Runs inside the nuclei container: one report per scope, /reports/<scope>.jsonl
# Targets: /vigil-conf/nuclei/<scope>.txt (CIDRs, hosts or URLs), exclusions in exclude.txt
set -u
. /vigil-conf/scan.env
nuclei -update-templates -silent -duc || echo "nuclei: template update failed, using cached templates"
for list in /vigil-conf/nuclei/*.txt; do
  scope=$(basename "$list" .txt)
  [ -s "$list" ] || continue
  set -- -l "$list" -jsonl -o "/reports/$scope.jsonl" -silent -nc -duc \
         -severity "$NUCLEI_SEVERITY" -rl "$NUCLEI_RATE_LIMIT" -c "$NUCLEI_CONCURRENCY"
  [ -s /vigil-conf/exclude.txt ] && set -- "$@" -eh /vigil-conf/exclude.txt
  echo "nuclei: scanning $scope"
  nuclei "$@" || echo "nuclei: $scope exited $?"
  [ -f "/reports/$scope.jsonl" ] || : > "/reports/$scope.jsonl"
done
