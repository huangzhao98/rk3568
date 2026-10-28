#!/bin/sh
while :; do
  {
    echo "===== $(date '+%F %T') uptime:$(cut -d' ' -f1 /proc/uptime)s load:$(cat /proc/loadavg)"
    dmesg | tail -40
  } > /var/log/blackbox.log.tmp
  sync
  mv /var/log/blackbox.log.tmp /var/log/blackbox.log
  sleep 10
done
