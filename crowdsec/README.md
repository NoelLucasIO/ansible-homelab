# crowdsec

CrowdSec with the decisions and the blocking on the firewall, and the detection where the
traffic is:

```
internet ─► OPNsense ─► Nginx Proxy Manager (Podman host)
              ▲   │            │ access logs (read-only)
              │   │            ▼
              │   │      crowdsec-agent  (this playbook: log processor only, no local API)
              │   │            │ alerts
              │   ▼            ▼
              │  os-crowdsec LAPI  ◄── community blocklist (CAPI)
              │        │ decisions
              └── firewall bouncer ─► pf table ─► block rules (every interface)
```

- **OPNsense** (`os-crowdsec` plugin, set up by hand below) runs the LAPI, its own log
  processor (OPNsense's logs) and the firewall bouncer. The bouncer puts the LAPI's
  decisions and the community blocklist into one pf table that the plugin's rules block.
- **The Podman host** runs `crowdsec-agent` (`playbooks/agent.yml`): a rootful Quadlet
  container that reads NPM's access logs and reports to that LAPI. An IP that probes or
  brute-forces a site behind NPM gets blocked at the firewall for every address you serve,
  not only at the proxy.
- Private addresses are whitelisted by CrowdSec's default parsers, so LAN clients are never
  banned.

## OPNsense (once, by hand)

1. **System → Firmware → Plugins**, tick **Show community plugins**, install `os-crowdsec`.
2. **Services → CrowdSec → Settings**: keep the log processor, LAPI and remediation
   component (bouncer) enabled. Set the **LAPI listen address** to the firewall's LAN
   address (default `127.0.0.1`: the agent couldn't reach it) and keep port 8080.
   Enable the blocklist rules, and their logging if you want the blocks in your firewall
   log. **Apply**.

   Check that the new address took: `sockstat -4l | grep 8080` should show the LAN
   address. If it still shows `127.0.0.1:8080`, Apply didn't restart the LAPI; run
   `service oscrowdsec restart` and check again.
3. If your LAN rules don't already allow it, pass TCP from the Podman host to *This
   Firewall* port 8080.
4. Create the agent's machine on the LAPI (console or SSH), named like the inventory host:

   ```sh
   cscli machines add fedora --auto -f -
   ```

   It prints `url`, `login` and `password` (on stderr). The password is
   `crowdsec_agent_password`.

The LAPI listens over plain HTTP. Every agent needs its own machine login, and the port is
reachable from the LAN only.

## The agent (this playbook)

Variables (inventory; defaults in `playbooks/roles/crowdsec_agent/defaults/main.yml`):

| Variable | What |
|---|---|
| `crowdsec_agent_lapi_url` | `http://<firewall LAN address>:8080` |
| `crowdsec_agent_npm_logs` | NPM's `data/logs` directory on the host |
| `crowdsec_agent_machine` | machine name from step 4 (default: the inventory host name) |
| `crowdsec_agent_password` | **secret**, from step 4 |
| `crowdsec_agent_collections` | hub collections (default: `nginx-proxy-manager`, `http-cve`) |
| `crowdsec_agent_acquisitions` | log files and their parser type (default: NPM's proxy-host, redirection-host, default-host and fallback access logs) |

**CLI:** copy `playbooks/group_vars/all/vault.yml.example` to `vault.yml`, fill it in,
`ansible-vault encrypt` it, then `ansible-playbook playbooks/agent.yml --ask-vault-pass -K`.

**AAP:** a custom credential type for the password, a credential of that type, and a job
template (playbook `crowdsec/playbooks/agent.yml`, machine credential + this one,
privilege escalation on):

```yaml
# Input configuration
fields:
  - {id: crowdsec_agent_password, label: CrowdSec agent machine password, type: string, secret: true}
required: [crowdsec_agent_password]
```

```yaml
# Injector configuration
extra_vars:
  crowdsec_agent_password: '{{ crowdsec_agent_password }}'
```

If the inventory names the host differently (an FQDN, say), set `crowdsec_agent_host` to
that name and keep `crowdsec_agent_machine` as the machine name from step 4, in the job
template's extra variables. A run that matches no host succeeds in seconds and changes
nothing.

The run checks that the LAPI answers, installs the Quadlet, waits until the agent has logged
in (`cscli lapi status`), and prints the acquisition metrics. Remove with
`-e crowdsec_agent_state=absent` (add `-e crowdsec_agent_purge=true` to delete its volumes).

## Checking it

On the Podman host:

```sh
podman exec crowdsec-agent cscli metrics show acquisition   # lines read / parsed / whitelisted per log
journalctl CONTAINER_NAME=crowdsec-agent -n 30
```

On OPNsense:

```sh
cscli machines list          # the agent, validated, with a recent heartbeat
cscli alerts list            # what the agents detected
cscli decisions list         # local bans (community blocklist entries: --origin CAPI)
pfctl -s Tables | grep -i crowdsec
```

## Tested

Against CrowdSec v1.8.2 on Fedora with Podman 5.8: `cscli explain` on real NPM logs
(proxy-host and default-host access lines parse; NPM's error logs and its JSON access log
don't, so they aren't acquired; scanner lines on the default host trigger
`http-crawl-non_statics` and `http-probing`), and an agent container configured exactly like
the Quadlet logging in to a separate LAPI container, tailing the logs, whitelisting LAN
clients, and logging in again after a restart. Then deployed for real: OPNsense 26.x with
the os-crowdsec plugin as the LAPI, the agent run from AAP, logged in to the LAPI and
tailing 38 NPM access logs. The plugin's rules show up in `pfctl -sr` as `block drop in/out
log quick` on `<crowdsec_blocklists>` / `<crowdsec6_blocklists>`.
