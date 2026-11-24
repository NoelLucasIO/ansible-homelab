#!/usr/bin/env python3
"""UDP syslog relay: Hitachi Content Platform -> Grafana Alloy.

HCP sends RFC 3164 syslog with no HOSTNAME field and a trailing NUL byte.
Alloy's loki.source.syslog drops those as "empty messages" because of the NUL.
This relay strips trailing NULs, inserts the sender's reverse-DNS name as the
HOSTNAME (so Loki gets a host label), and forwards to a localhost-only Alloy
listener. Splunk can receive HCP directly; it keeps the message as-is.
"""
import os
import re
import socket
import sys

LISTEN = ("0.0.0.0", int(os.environ.get("RELAY_LISTEN_PORT", "1517")))
FORWARD = ("127.0.0.1", int(os.environ.get("RELAY_FORWARD_PORT", "1599")))
HDR = re.compile(rb"^(<\d{1,3}>)([A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d) (.*)$", re.S)
names = {}


def host_of(ip):
    if ip not in names:
        try:
            names[ip] = socket.gethostbyaddr(ip)[0]
        except OSError:
            names[ip] = ip
    return names[ip].encode()


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(LISTEN)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"relaying udp/{LISTEN[1]} -> {FORWARD[0]}:{FORWARD[1]}", file=sys.stderr, flush=True)
    while True:
        data, (ip, _) = rx.recvfrom(65535)
        data = data.rstrip(b"\x00\r\n")
        m = HDR.match(data)
        if m:
            data = m.group(1) + m.group(2) + b" " + host_of(ip) + b" " + m.group(3)
        tx.sendto(data, FORWARD)


if __name__ == "__main__":
    main()
