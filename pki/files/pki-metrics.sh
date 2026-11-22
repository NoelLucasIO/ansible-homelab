#!/bin/bash
# pki-metrics: expiry gauges for the lab CA, as a Prometheus text file served by pki-web
# at http://127.0.0.1:<web port>/metrics. Run by pki-metrics.timer (user unit).
#   pki-metrics.sh <certs dir> <out file> <step-ca port> <web port>
set -uo pipefail
certs=$1 out=$2 ca_port=$3 web_port=$4
tmp=$(mktemp "${out}.XXXXXX")
trap 'rm -f "$tmp"' EXIT

epoch() { date -d "$1" +%s 2>/dev/null || echo NaN; }
enddate() { openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2; }

{
  echo '# HELP pki_cert_not_after_seconds Expiry of the CA certificates and step-ca serving certificate (unix time).'
  echo '# TYPE pki_cert_not_after_seconds gauge'
  for c in root intermediate; do
    echo "pki_cert_not_after_seconds{cert=\"$c\"} $(epoch "$(enddate < "$certs/${c}_ca.crt")")"
  done
  serving=$(openssl s_client -connect "127.0.0.1:$ca_port" </dev/null 2>/dev/null | enddate)
  echo "pki_cert_not_after_seconds{cert=\"serving\"} $(epoch "$serving")"

  crl=$(mktemp)
  if curl -fsS -m 10 -o "$crl" "http://127.0.0.1:$web_port/crl" \
     && openssl crl -inform DER -in "$crl" -noout >/dev/null 2>&1; then
    next=$(openssl crl -inform DER -in "$crl" -noout -nextupdate | cut -d= -f2)
    last=$(openssl crl -inform DER -in "$crl" -noout -lastupdate | cut -d= -f2)
    revoked=$(openssl crl -inform DER -in "$crl" -noout -text | grep -c 'Serial Number:')
    echo '# HELP pki_crl_fetch_success 1 if the CRL could be fetched and parsed.'
    echo '# TYPE pki_crl_fetch_success gauge'
    echo 'pki_crl_fetch_success 1'
    echo '# TYPE pki_crl_next_update_seconds gauge'
    echo "pki_crl_next_update_seconds $(epoch "$next")"
    echo '# TYPE pki_crl_last_update_seconds gauge'
    echo "pki_crl_last_update_seconds $(epoch "$last")"
    echo '# TYPE pki_crl_revoked_certificates gauge'
    echo "pki_crl_revoked_certificates $revoked"
  else
    echo '# TYPE pki_crl_fetch_success gauge'
    echo 'pki_crl_fetch_success 0'
  fi
  rm -f "$crl"

  echo '# HELP pki_metrics_last_run_seconds When this file was written (unix time).'
  echo '# TYPE pki_metrics_last_run_seconds gauge'
  echo "pki_metrics_last_run_seconds $(date +%s)"
} > "$tmp"

chmod 0644 "$tmp"
mv -f "$tmp" "$out"
trap - EXIT
