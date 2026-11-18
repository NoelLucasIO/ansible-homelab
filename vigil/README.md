# vigil

A security-scanning stack for one Podman host, all in one rootful Podman pod named `vigil`, so Cockpit shows it as one group:

| Part | What it does | Reached at |
|---|---|---|
| **Greenbone CE** (OpenVAS) | Weekly authenticated-capable network vulnerability scans per scope | https://gvm.&lt;domain&gt; |
| **DefectDojo** (open source) | Collects every scanner's findings, dedupes, tracks fixes, Jira if wanted | https://dojo.&lt;domain&gt; |
| **Nuclei** | Weekly template checks of every scope and every web app | (timer) |
| **ZAP** | Weekly passive baseline of every web app behind NPM | (timer) |

Everything is a Quadlet in `/etc/containers/systemd/vigil*.{pod,container}`, rendered from `playbooks/vars/containers.yml`.

## Why it's built this way

- **Rootful, host network.** OpenVAS needs raw sockets for ICMP/ARP alive detection and SYN scans, and every container in a pod shares one mode and one network.
- **No port collisions with what the host already runs.** The upstream Greenbone stack binds 80, 443 and 5432; here:
  - pg-gvm listens on its Unix socket only (`POSTGRES_LISTEN_ADDRESSES=" "`), and redis already does.
  - gsad serves the web UI itself on `127.0.0.1:9392`, so Greenbone's nginx (443) isn't used.
  - gvmd's socket and PID file live in a host directory (`vigil_gvmd_run_dir`, recreated at boot by tmpfiles.d), bind-mounted into gvmd, gsad and gvm-tools. As a named volume shared with gsad it kept reverting to root and gvmd couldn't write its PID file.
  - openvasd is on `127.0.0.1:3080`; DefectDojo's valkey is on `127.0.0.1:6380` (6379 is often taken).
  - DefectDojo's nginx is `:8080` and uwsgi's HTTP side is `:8081` (both fixed by the images, all interfaces); uwsgi's socket is `127.0.0.1:3031`.
- **DefectDojo's database is on the host's existing rootless Postgres** (`defectdojo` DB and role, plus a read-only `vigil_ro` role for Grafana). Greenbone keeps its own pg-gvm because it needs Greenbone's extension.
- **DefectDojo images come from docker.io** (`defectdojo/defectdojo-django`, `-nginx`), pinned to one release.

## Data flow

```
Greenbone (gvmd schedule, Sun) ─┐
Nuclei timer (Sat 01:00) ───────┼─► <vigil_data>/reports/<tool>/<scope>.* ─► vigil-import@<tool> ─► DefectDojo
ZAP timer (Sat 04:00) ──────────┘        (export timer daily 07:00 for Greenbone)      reimport-scan API
```

- Each **scope** in `vigil_scopes` becomes a Greenbone target + weekly task, a Nuclei target list, and a DefectDojo product. Web apps are a third product, `Web apps`.
- **Web targets** are every enabled NPM proxy host, plus `vigil_web_extra`, minus `vigil_web_exclude`. They're re-read from NPM on every deploy.
- The import uses `reimport-scan` with `auto_create_context` and `close_old_findings`. Each scanner+scope is one DefectDojo test that gets updated: fixed findings close, repeats dedupe. Imported files are archived under `reports/archive/<date>/` for `vigil_report_keep_days`.
- **Feeds:** `vigil-feed-sync.timer` pulls the Greenbone feed images daily. If one changed, it re-runs that copy unit, and `Requires=` restarts gvmd/ospd so they pick it up. Nuclei updates its templates at the start of each run.

## Observability

- **Logs:** nothing to add. Rootful containers log to the system journal, and Alloy's `loki.source.journal` (`playbooks/observability-agents.yml`) already labels them `container="vigil-…"`.
- **Metrics:** `scrape.d/vigil.yml` scrapes DefectDojo's `/django_metrics`. Container state comes from the existing rootful podman exporter (`:9882`).
- **Alerts** (`rules.d/vigil.yml`): `VigilDefectDojoDown`, `VigilContainerNotRunning`, `VigilContainersMissing` (Quadlet uses `--rm`, so a dead container disappears rather than showing "exited").
- **Grafana:** datasource `DefectDojo (vigil)` and dashboard **Security / vigil: vulnerability findings**: open findings by severity and product, new findings per day, the newest open findings (linked to DefectDojo), last import per scanner, and the pod's logs.
- The playbook adds `/etc/prometheus/scrape.d/*.yml` and `/etc/prometheus/rules.d/*.yml` to `prometheus.yml`, keeping a backup. Existing entries aren't touched.

## Setup

1. **Site values.** Defaults live in `playbooks/roles/vigil_defaults/defaults/main.yml` and use documentation addresses. Set the real ones as inventory variables (AAP) or in a git-ignored `site-vars.yml` (`-e @site-vars.yml` on the CLI). At minimum:
   - `vigil_pg_owner`: the user that owns the rootless Postgres/Prometheus/Grafana Quadlets
   - `vigil_data_mount`, `vigil_prom_config`, `vigil_grafana_provisioning`, `vigil_loki_uid`
   - `vigil_scopes` (real CIDRs), and `vigil_domain` (or ipam-vm's `vm_domain`)
   - The NIOS/NPM variables ipam-vm already uses (`nios_ip`, `npm_api`, `npm_ip`, ...) plus `vigil_dns_resolver`
2. **Secrets** (nine; `playbooks/group_vars/all/vault.yml.example` lists them):
   - **CLI:** copy it to `vault.yml` (git-ignored), fill it in, `ansible-vault encrypt vault.yml`.
   - **AAP:** the three NIOS/NPM secrets come from ipam-vm's secrets credential. Add a second custom credential type for vigil's own six and attach both to the templates:

     ```yaml
     # Input configuration
     fields:
       - {id: vigil_dojo_db_password, label: DefectDojo DB password, type: string, secret: true}
       - {id: vigil_dojo_admin_password, label: DefectDojo admin password, type: string, secret: true}
       - {id: vigil_dojo_secret_key, label: DefectDojo SECRET_KEY (50+ chars), type: string, secret: true}
       - {id: vigil_dojo_aes_key, label: DefectDojo AES-256 key (exactly 32 chars), type: string, secret: true}
       - {id: vigil_gvm_admin_password, label: Greenbone admin password, type: string, secret: true}
       - {id: vigil_grafana_db_password, label: Grafana read-only DB password, type: string, secret: true}
     required: [vigil_dojo_db_password, vigil_dojo_admin_password, vigil_dojo_secret_key,
                vigil_dojo_aes_key, vigil_gvm_admin_password, vigil_grafana_db_password]
     ```

     ```yaml
     # Injector configuration
     extra_vars:
       vigil_dojo_db_password: '{{ vigil_dojo_db_password }}'
       vigil_dojo_admin_password: '{{ vigil_dojo_admin_password }}'
       vigil_dojo_secret_key: '{{ vigil_dojo_secret_key }}'
       vigil_dojo_aes_key: '{{ vigil_dojo_aes_key }}'
       vigil_gvm_admin_password: '{{ vigil_gvm_admin_password }}'
       vigil_grafana_db_password: '{{ vigil_grafana_db_password }}'
     ```
3. **AAP job templates**, each with the machine credential and both secrets credentials, privilege escalation on, any EE (only `ansible.builtin` is used):
   - **vigil deploy**: playbook `vigil/playbooks/deploy.yml`. Optional survey `vigil_pull_latest` (boolean). Tick "Prompt on launch" for job tags to allow partial runs.
   - **vigil remove**: playbook `vigil/playbooks/remove.yml`. Survey: `confirm` (text, required), `purge` (boolean, default false).
   - Steps that touch the rootless Postgres/Prometheus/Grafana run as root via `runuser -u <vigil_pg_owner>`, so the SSH user only needs sudo to root.
   - The first run takes 20–30 min (image pulls + migrations): job timeout 0 or ≥ 45 min.

   The first run waits for DefectDojo's migrations. Greenbone's first feed load takes another 30–60 min; `vigil-gb-setup` keeps retrying in the background (every 5 min, up to 5 h) and creates the targets/tasks once the feed is in (`journalctl -fu vigil-gb-setup`).

Partial runs: `--tags config,quadlets` (targets/units only), `--tags observability`, `--tags dns`, `--tags verify`. Newer `:stable` Greenbone images: `-e vigil_pull_latest=true`.

## Day to day

```bash
systemctl list-timers 'vigil-*'                  # next scans
systemctl start vigil-scan-nuclei                # scan now (import follows on success)
systemctl start vigil-gb-export                  # push the newest Greenbone reports now
journalctl -u 'vigil-import@*' -n 50             # what got imported
podman pod ps; podman ps --pod --filter pod=vigil
```

Change scope, schedules or exclusions in your inventory variables and rerun with `--tags config,quadlets,greenbone`. Greenbone can't change the hosts of a target that a task uses. `vigil-gb-setup` logs a warning when they differ; delete that task + target in GSA and rerun to rebuild them.

## remove.yml

`-e confirm=vigil` stops everything and removes the units. Volumes, the database, reports, DNS and proxy hosts are kept. Add `-e purge=true` to delete those too. NIOS records are only deleted when they carry the `vigil: managed by Ansible` comment.

## Tested here vs. on first run

- **Tested:** both playbooks pass syntax check and ansible-lint. All templates render, and Podman 5.8.9's Quadlet generator (`quadlet -dryrun`) accepts all 29 units: the pod pulls in the 25 service and setup containers, and the 4 timer jobs stay out. `promtool check rules` passes the alert rules, and the dashboard JSON parses. Scripts pass `bash -n` / `py_compile`. I checked the upstream sources each piece depends on:
  - Greenbone's compose file and the pg-gvm, redis and gsad startup scripts inside the images.
  - DefectDojo's entrypoints, `complete_initialization`, the reimport-scan serializer, the parser names (`OpenVAS Parser`, `Nuclei Scan`, `ZAP Scan`) and the table names used by the dashboard.
- **First run will show:**
  - pg-gvm stays off TCP with `POSTGRES_LISTEN_ADDRESSES=" "`. `verify.yml` checks its log for a TCP listener and fails loudly if one appears.
  - gsad serves the GSA files from the `gsa:stable-slim` volume via `--static-content`. If the page is blank, fall back to Greenbone's nginx on another port.
  - The gsa and feed-data images exit after copying when `KEEP_ALIVE` isn't set (the units are oneshots).
  - gvmd's container has a `gvmd` user and creates `admin` on first start; `greenbone.yml` waits for it, then sets the vault password.
  - `vigil-gvm.py` uses python-gvm calls whose enum module has moved between releases. It tries the known locations and falls back to the scan config's default alive test.
  - In DefectDojo 3.5, `/django_metrics` answers without auth and `api-token-auth` still issues tokens for the import script.
- **Things to know:**
  - **An IDS will see the scans.** Greenbone/Nuclei trip ET SCAN rules on the LAN. Either suppress by source IP or treat the alerts as the canary that IDS works.
  - **A public range scanned from inside** shows what the LAN sees: the firewall answers for its WAN addresses. That isn't what the internet sees; an outside view needs a scanner outside.
  - **Empty Nuclei reports aren't imported,** so findings fixed between runs close at the next run that finds anything, not immediately.
  - **ZAP is passive (baseline).** Active scanning is a deliberate later step, per site.
