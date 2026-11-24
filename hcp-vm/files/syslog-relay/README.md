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
