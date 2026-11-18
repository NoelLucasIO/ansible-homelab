#!/bin/sh
# Runs inside the openvas-scanner image (vigil-gb-openvas-config): writes openvas.conf
# and a log config into the shared volume. Same as Greenbone's compose "configure-openvas"
# step, except openvasd is on 127.0.0.1 (the pod shares the host network).
set -e
printf "table_driven_lsc = yes\nopenvasd_server = %s\n" "$OPENVASD_URL" > /mnt/openvas.conf
sed "s/127/128/" /etc/openvas/openvas_log.conf | sed 's/gvm/openvas/' > /mnt/openvas_log.conf
chmod 644 /mnt/openvas.conf /mnt/openvas_log.conf
touch /var/log/openvas/openvas.log
chmod 666 /var/log/openvas/openvas.log
