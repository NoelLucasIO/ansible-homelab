# HCP syslog relay

HCP's syslog output (System Management Console → Monitoring → Syslog) is UDP-only,
RFC 3164 with **no HOSTNAME** field and a **trailing NUL byte**, for example:

```
<133>Nov 24 12:14:21 HCP_System_Event: 0000004616 node 101, event 1201, severity: Notice, ...\x00
```

Grafana Alloy's `loki.source.syslog` discards these as empty messages because of the NUL.
`hcp-syslog-relay.py` sits in front of Alloy: it strips trailing NULs, inserts the sender's
reverse-DNS name as HOSTNAME (e.g. `hcp-node-101.hcp01.example.com`), and forwards to an Alloy listener bound to localhost.

```
HCP --udp/1517--> hcp-syslog-relay.py --udp 127.0.0.1:1599--> Alloy (job="hcp") --> Loki
HCP --udp/1517--> Splunk UDP input (index=hcp)   # Splunk takes the message as-is
```

| File | Installs to (log host, as the user running Alloy) |
|---|---|
| `hcp-syslog-relay.py` | `~/.local/bin/hcp-syslog-relay.py` |
| `hcp-syslog-relay.service` | `~/.config/systemd/user/hcp-syslog-relay.service` (needs `loginctl enable-linger`) |
| `hcp.alloy` | Alloy's config directory, loaded alongside the main config (it uses the main config's `loki.relabel.syslog` and `loki.write.local`) |

Ports can be changed with `RELAY_LISTEN_PORT` / `RELAY_FORWARD_PORT` in the unit's environment;
keep `hcp.alloy`'s listener address in step. In Grafana: `{job="hcp"}`; labels `host`, `app`
(`HCP_System_Event`) and `level` come from the syslog header.

## Splunk side

A plain UDP input takes HCP's messages as they are:

```
[udp://1517]
acceptFrom = 192.0.2.0/24
connection_host = dns
index = hcp
sourcetype = syslog
no_appending_timestamp = true
```

- `connection_host = dns` makes each event's `host` the node's DNS name; don't set a fixed `host`.
- `no_appending_timestamp = true`: HCP's message has no hostname, so Splunk would otherwise put its
  own timestamp and host in front of every event.
- Don't write the stanza as `[udp://<HCP address>:1517]`: an address there limits the input to that
  one sender. Here it silently dropped every HCP message; use `acceptFrom` to limit senders.

