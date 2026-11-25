#!/bin/sh
# porkbun-ddns.sh - keep one Porkbun A record equal to an OPNsense interface's IPv4.
#
# Used instead of os-ddclient: ddclient 3.11.2 ignored "usev4=ifv4, ifv4=<if>" and
# published the default-route (primary WAN) address for a secondary-WAN record.
#
# Install on the firewall:
#   /root/bin/porkbun-ddns.sh                  (0700)
#   /root/.porkbun-ddns.env                    (0600, from porkbun-ddns.env.example)
#   /usr/local/opnsense/service/conf/actions.d/actions_porkbunddns.conf
# then: service configd restart, and add a cron job in System > Settings > Cron.
set -eu
. /root/.porkbun-ddns.env    # APIKEY SECRET DOMAIN SUB IFACE
API=https://api.porkbun.com/api/json/v3/dns
STATE=/var/db/porkbun-ddns.$SUB.$DOMAIN
TAG=porkbun-ddns

ip=$(ifconfig "$IFACE" | awk '/inet /{print $2; exit}')
[ -n "$ip" ] || { logger -t $TAG "no IPv4 on $IFACE"; exit 1; }

# Re-verify against Porkbun at least hourly, otherwise trust local state
find "$STATE" -mmin +60 -delete 2>/dev/null || true
[ -f "$STATE" ] && [ "$(cat "$STATE")" = "$ip" ] && exit 0

# Keys go to curl on stdin (-d @-), never on the command line
auth="\"apikey\":\"$APIKEY\",\"secretapikey\":\"$SECRET\""
post() { curl -fsS -m 30 -X POST -H 'Content-Type: application/json' -d @- "$API/$1"; }

cur=$(printf '{%s}' "$auth" | post "retrieveByNameType/$DOMAIN/A/$SUB" \
      | sed -n 's/.*"content":"\([^"]*\)".*/\1/p')

if [ "$cur" != "$ip" ]; then
  printf '{%s,"content":"%s","ttl":"600"}' "$auth" "$ip" \
    | post "editByNameType/$DOMAIN/A/$SUB" | grep -q '"status":"SUCCESS"' \
    || { logger -t $TAG "update FAILED $SUB.$DOMAIN -> $ip"; exit 1; }
  logger -t $TAG "$SUB.$DOMAIN: ${cur:-none} -> $ip"
fi
echo "$ip" > "$STATE"
