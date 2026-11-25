# porkbun-ddns: Porkbun DDNS for a secondary (DHCP) WAN on OPNsense

Keeps one Porkbun A record (e.g. `vpn2.<domain>`) equal to the IPv4 address of a chosen
OPNsense interface, such as a DHCP backup WAN. It runs from OPNsense's own cron through a
configd action, so it shows up in **System > Settings > Cron**.

**Why not os-ddclient?** On a dual-WAN firewall, ddclient 3.11.2 ignored
`usev4=ifv4, ifv4=<interface>` and published the address it saw through the default route,
the primary WAN, even though `ddclient -query` read the interface correctly.

| File | Goes to (on the firewall) | Mode |
|---|---|---|
| `porkbun-ddns.sh` | `/root/bin/porkbun-ddns.sh` | 0700 |
| `porkbun-ddns.env.example` | `/root/.porkbun-ddns.env` (fill in keys) | 0600 |
| `actions_porkbunddns.conf` | `/usr/local/opnsense/service/conf/actions.d/` | 0644 |

## Behaviour

- Reads the interface address with `ifconfig`; nothing goes out to a "what is my IP" service.
- Talks to Porkbun only when the address differs from the last one it set, and checks
  Porkbun again at least hourly (state file `/var/db/porkbun-ddns.<sub>.<domain>`).
- API keys reach curl on stdin, so they never appear in `ps` or logs.
- Logs a change or a failure to syslog with tag `porkbun-ddns`; quiet otherwise.

## Install

```sh
mkdir -p /root/bin
install -m 700 porkbun-ddns.sh /root/bin/
install -m 600 porkbun-ddns.env.example /root/.porkbun-ddns.env   # then edit it
install -m 644 actions_porkbunddns.conf /usr/local/opnsense/service/conf/actions.d/
service configd restart
/root/bin/porkbun-ddns.sh && grep porkbun-ddns /var/log/system/latest.log | tail -3
```

Then **System > Settings > Cron > +**: Command *Porkbun DDNS update (vpn2)*, Minutes `*/5`,
everything else `*`, and **Apply**.

These files live outside `config.xml`: an OPNsense config restore does not bring them back.
Reinstall from here after a reinstall of the firewall.
