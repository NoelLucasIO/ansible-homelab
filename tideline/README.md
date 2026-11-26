# tideline

What can be updated, everywhere, in one Grafana dashboard: for every system, each pending
update with the **installed version and when it was installed**, the **version on offer**,
and **the advisory or release notes behind it** (text and link). Read-only: Tideline never
installs, upgrades or removes anything.

| Source | Where | What it reads | Notes / links |
|---|---|---|---|
| `dnf` | Fedora, RHEL, CentOS Stream | `dnf repoquery --upgrades`; dnf5 `advisory list/info --json`, dnf4 `updateinfo.xml` | Bodhi update or Red Hat erratum (title, description, severity, CVEs); Fedora package page and dist-git history |
| `pacman` | Arch, CachyOS | `checkupdates` (private sync db, no partial upgrade) | security.archlinux.org AVGs (severity, CVEs); archlinux.org / packages.cachyos.org and Arch GitLab history |
| `aur` | Arch, CachyOS | foreign packages vs the AUR RPC (VCS `-git` packages skipped) | AUR page, AUR git log, upstream site |
| `apt` | Debian, Ubuntu, Proxmox (PBS) | `apt-get -s dist-upgrade`; security pockets flagged | top `apt-get changelog` entry as the notes; tracker / security-tracker / Launchpad / git.proxmox.com |
| `flatpak` | system installation | `flatpak remote-ls --updates` | Flathub page |
| `fwupd` | any host with fwupd | `fwupdmgr get-updates --json` (LVFS) | release description, urgency, CVEs |
| `container` | Podman hosts, root and rootless | running containers vs their registry: same tag rebuilt (digest), or a newer tag of the same shape (`0.17.2-tensorrt` → `0.18.0-tensorrt`, `18` → `19`) | GitHub / Codeberg release notes for that version; registry tag page |
| `opnsense` | OPNsense (API only) | `core/firmware/check` + `status`; major upgrades too | release changelog text, docs.opnsense.org release page |
| `appliance` | vendor appliances | **your catalog** (`tideline_appliances`), with live version probes for vTM, NIOS, any HTTP endpoint or SSH command | your release-notes and download links, your notes |

Registry checks use `HEAD` requests and tag lists, which Docker Hub doesn't count against its
anonymous pull limit. Huge tag lists (docker.elastic.co) are skipped in favour of probing the
next likely versions with `HEAD`.

## How it fits

```
AAP workflow "Tideline" (every 6 h)
  ├─ Tideline collect   (inventory homelab)
  │    each Linux host: files/tideline_collect.py → JSON      (Python 3 stdlib only)
  │    localhost: appliance catalog + probes
  │    store host: tideline.ingest(json) in PostgreSQL → tideline.metrics() → node_exporter textfile
  └─ Tideline OPNsense  (inventory network)
       firewall API → same ingest
Grafana "Tideline: available updates"  ← PostgreSQL datasource (SELECT-only role)
Prometheus alerts                       ← tideline_* metrics
```

The database keeps history: an update is **open** until a later successful check of the same
source no longer offers it (then `resolved_at` is set), so the dashboard shows how long each
update has waited and what was applied when. A source that fails on a run keeps its last known
rows; an unreachable host keeps everything and shows up as stale.

## Dashboard

*Tideline: available updates* (folder `tideline_grafana_folder`), filters for system, source,
security-only and a search box (package, advisory ID, CVE):

- **Pending updates / Health**: totals, security and critical/high counts, hosts needing a
  reboot, the oldest waiting security update, stale systems, systems with collection errors.
- **Systems**: one row per host, firewall and appliance.
- **Pending updates**: system, source, package, installed version, installed on, available
  version, severity, advisory, notes, pending since. Package, Advisory and Notes are links
  (distro package page, erratum or release notes, change history).
- **Advisories and release notes**: one row per advisory with its full description, CVEs and
  affected systems (Inspect a cell to read long text).
- **Appliances**: installed (live or manual) vs latest, release-notes link, your notes.
- **Pending updates over time** and **Applied or withdrawn** (what got installed, when).

## Vendor appliances: the catalog

Vendor portals need a login, so "latest" for appliances is yours to keep. The list lives in
`tideline_appliances`: in AAP under **Inventories → homelab → Variables** (edit it there any
time; `aap-setup.yml` only seeds it when it's absent, so a rerun never overwrites your edits),
or `site-vars.yml` for CLI runs.

```yaml
tideline_appliances:
  - name: vtm01.example.com            # shown as the system name
    product: Ivanti vTM
    vendor: Ivanti
    installed: "22.9r7"                # used when there's no probe or it fails
    installed_on: 2026-01-15
    latest: "22.9r7"                   # update it when the vendor releases
    latest_released: 2025-12-01
    release_notes: https://...
    download: https://...
    notes: anything you want on the dashboard
    probe: {type: vtm, url: "https://vtm01.example.com:9070"}
```

Probes (optional) read the running version, so only `latest` needs your hand:

| `probe.type` | Reads | Credentials |
|---|---|---|
| `vtm` | REST `status/local_tm/information` → `tm_version` | `vtm_api_user` / `vtm_api_password` (a "vTM API" credential) |
| `nios` | WAPI `upgradestatus` → `current_version` | `nios_api_user` / `nios_api_password` (`homelab-secrets`); the user needs read access to the grid's upgrade status |
| `http` | `url`, then `regex` (one capture group) | optional `user` and `password_var` (name of a variable holding it) |
| `command` | `command` on inventory host `host`, then `regex` | the job's machine credential |
| `raw` | same over plain SSH (network gear, e.g. Junos `show version`) | the job's machine credential |

The comparison is natural-version (`22.9r7` < `22.9r10`, `9.1.0-45678` < `9.1.1-1`). A failed
probe falls back to `installed` and shows its error in the Appliances table. Hosts named in the
catalog are never treated as Linux hosts, even if they are in the inventory; `tideline_skip: true`
leaves out any other host.

## Running it

### In AAP

```bash
cd tideline
ansible-playbook playbooks/aap-setup.yml -e @../hcp-vm/aap.local.yml -e @../site-vars.yml
```

It prompts for the Grafana database password, an optional GitHub token, and an OPNsense API
key (Enter skips any of them), then creates or updates:

| Object | Notes |
|---|---|
| credential type **Tideline secrets**, credential **homelab-tideline** | `tideline_grafana_db_password`, `tideline_github_token` |
| credential **`<firewall>-tideline-api`** (type *OPNsense API*) | a separate OPNsense user with only **System: Firmware**; that privilege can also start an update, so the key lives only in AAP |
| inventory variables | `tideline_*` site values merged into **homelab** and **network** (other keys untouched) |
| job template **Tideline deploy** | `deploy.yml` on the store host: run once, and after changing the dashboard |
| job template **Tideline collect** | `collect.yml`; survey: which Linux hosts (default `all`). Credentials: `homelab-ssh`, `homelab-tideline`, `homelab-secrets` (NIOS probe) and a vTM API credential if you set `tl_vtm_credential` |
| job template **Tideline OPNsense** | `opnsense.yml` on the **network** inventory |
| workflow **Tideline** | collect, then OPNsense (always); schedule **every 6 hours** (05:13, 11:13, 17:13, 23:13) |

Then run **Tideline deploy** once and launch the **Tideline** workflow.

### On the CLI

```bash
cd tideline
cp inventory.example.yml inventory.yml            # real hosts; git-ignored
cp playbooks/group_vars/all/vault.yml.example playbooks/group_vars/all/vault.yml
ansible-vault encrypt playbooks/group_vars/all/vault.yml
ansible-playbook -i inventory.yml playbooks/deploy.yml  -e @../site-vars.yml --ask-vault-pass -K
ansible-playbook -i inventory.yml playbooks/collect.yml -e @../site-vars.yml --ask-vault-pass -K
```

The collector also runs by hand on any host (it is installed at `tideline_collector_path`):

```bash
sudo /usr/local/libexec/tideline/collect.py --container-users root,alice | python3 -m json.tool
TIDELINE_DEBUG=1 sudo -E /usr/local/libexec/tideline/collect.py --no-refresh >/dev/null   # timings on stderr
```

## Requirements

- **Store host**: the existing rootless PostgreSQL (16+), Prometheus and Grafana Quadlets of
  `tideline_owner`, host network, plus node_exporter as a system unit (`deploy.yml` adds the
  textfile collector to it with a drop-in if it isn't there).
- **Linux hosts**: `/usr/bin/python3` (standard library only) and the automation user with
  sudo. On Arch/CachyOS, `pacman-contrib` for `checkupdates` (installed when
  `tideline_install_prereqs` is true, the only thing Tideline ever installs).
- **Rootless containers**: list the owners in `tideline_container_users` (host or group var);
  owners without a running user session are skipped.
- **Network** from the hosts: their package mirrors, security.archlinux.org and
  aur.archlinux.org (Arch), the container registries, api.github.com / codeberg.org
  (release notes), and LVFS (fwupd).

## Variables

All defaults are in `playbooks/roles/tideline_defaults/defaults/main.yml`. The ones a site sets:

| Variable | Default | |
|---|---|---|
| `tideline_store_host` | `fedora` | Postgres/Prometheus/Grafana host |
| `tideline_owner` | `CHANGE_ME` | owner of those rootless Quadlets |
| `tideline_textfile_dir` | `/var/lib/node_exporter/textfile_collector` | must match node_exporter's, if it already has one |
| `tideline_prom_rules_dir` / `tideline_prom_url` | `/srv/prometheus/config/rules.d` / `http://127.0.0.1:9090` | |
| `tideline_grafana_provisioning` | `/srv/grafana-provisioning` | mounted at `/etc/grafana/provisioning` |
| `tideline_container_users` | `[root]` | per host: add rootless owners |
| `tideline_container_sources` | common images | release-note repo for images without an `org.opencontainers.image.source` label |
| `tideline_appliances` | `[]` | the catalog |
| `tideline_keep_days` | `365` | history of resolved updates |

## Alerts

| Alert | When |
|---|---|
| `TidelineCollectionStale` | a system hasn't had a complete check for `tideline_stale_hours` (30) |
| `TidelineMetricsMissing` | no Tideline metrics at all for 12 h |
| `TidelineCriticalSecurityUpdate` | a critical security update has waited over `tideline_alert_critical_days` (3) |
| `TidelineHighSecurityUpdate` | a high/important one over `tideline_alert_high_days` (14) |
| `TidelineRebootPending` | a reboot has been needed for `tideline_alert_reboot_days` (14) |

Metrics: `tideline_updates_pending{host,kind,source}`, `tideline_security_pending{host,severity}`,
`tideline_security_oldest_timestamp_seconds{host,severity}`, `tideline_reboot_required{host}`,
`tideline_last_success_timestamp_seconds{host,kind}`, `tideline_collect_errors{host}`,
`tideline_appliance_update_available{appliance}`.

## Limits worth knowing

- Severity comes from the distribution: Fedora rates many security updates `None` (shown as
  *unrated*), Debian's apt doesn't rate them at all, and Arch AVGs rate per issue group.
- `apt`: "installed on" is the package's dpkg file-list time (dpkg keeps no install date).
- Containers: "installed on" is when the running container was created; a newer *major* tag
  (`postgres:18` → `19`) is reported as `major` so you can filter it out.
- OPNsense packages have no install date in the API.
