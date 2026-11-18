#!/bin/bash
# Runs inside the ZAP container: passive baseline per site -> /zap/wrk/web--<site>.xml
# Passive only (spider + passive rules); nothing is attacked.
set -u
. /vigil-conf/scan.env
while read -r url; do
  [[ -z $url || $url == \#* ]] && continue
  site=${url#*://}; site=${site%%/*}
  echo "zap: baseline $url"
  zap-baseline.py -t "$url" -m "$ZAP_SPIDER_MINUTES" -x "web--$site.xml" -I \
    || echo "zap: $url exited $?"
done < /vigil-conf/zap.txt
