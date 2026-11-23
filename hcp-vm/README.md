# hcp-vm

Deploys a single-node HCP (Hitachi Content Platform) VM on KVM from the HCP ISO, unattended,
from an AAP job template whose survey carries every installer answer and the VM sizing, then
does the post-install setup: first login, HCP's Prometheus, an initial tenant, and node_exporter.
Tested with HCP 10.1.1.14 (ISO `HS222_10.1.1.14.iso`): deploy, destroy and the deploy +
first-login workflow end to end; Prometheus, tenant and node_exporter as separate jobs on an
existing system. The full workflow with all four post-install steps is not yet tested.

## What deploy.yml does

1. **Preflight** on the chosen hypervisor: survey checks, ISO present, `virt-install`, `virsh`,
   `qemu-img`, `qemu-nbd`, `python3`.
2. **Front-end IP**: the survey value, or the next free address in `hcp_frontend_network` from NIOS
   (pinged first), recorded as an IPAM-only host record (`configure_for_dns: false`).
   Reruns reuse the record.
3. **Delegation**: `<cluster>.<domain>` becomes a delegated zone in NIOS with the node
   (`hcp-node-<n>.<cluster>.<domain>`) as name server and its front-end address as glue. NIOS
   creates the PTR itself. Done before the install because arc-deploy checks DNS.
4. **VM**: isolated libvirt network `hcp-backend` (created once: no DHCP, no NAT, no host
   address), thin qcow2 disks, q35, `--os-variant centos-stream9`, NIC 1 on `br0` (eth0,
   front-end), NIC 2 on `hcp-backend` (eth1, back-end), ISO on SATA.
5. **OS install on the serial console** (`files/hcp_console.py`, stdlib Python on the hypervisor).
   The ISO's boot line ends with `console=tty0`, so its prompts go to the VGA screen. The VM's
   install boot therefore loads the ISO's kernel and initrd directly with
   `console=tty0 console=ttyS0,9600n1`: the installer's own serial mode. The driver answers
   *Clear storage volumes*, then the installer's Network Setup (front-end IPv4, mask, gateway,
   back-end IPv4; the node number is the back-end last octet). It only confirms when the summary
   shows exactly those addresses. The install boot has `on_reboot=destroy`, so the VM powers off
   when the installer finishes. Transcript: `/var/log/hcp-vm/<VM>-console.log` on the hypervisor.
6. **Root access, first boot**: with the VM off, the public half of the HCP root key is added to
   `/root/.ssh/authorized_keys_fe` on the `root0` partition (`qemu-nbd`). arc-deploy puts the same
   support key there itself; this just makes it usable before the install. Then the final
   (disk-boot) definition is applied and the VM started.
7. **HCP install** over root SSH: sets the `install` password (`passwd --stdin`, nothing in argv),
   extracts the setup files from the ISO if the OS installer didn't, writes `arc-deploy/cluster.cfg`
   from `templates/cluster.cfg.j2`, and starts
   `arc-deploy.py --installUser -f -c cluster.cfg --noprompt` as `install` under `ssh-agent`
   in a transient systemd unit (detached: arc-deploy reconfigures the network and reboots).
   No wizard. The template reproduces the 10.1.1.14 wizard's output key for key.
8. **Checks**: arc-deploy succeeded (log, or the console already up after its reboot), port 8000
   answers, and `admin.<cluster>.<domain>` resolves to the node through the LAN resolver.

**Reruns:** an installed HCP closes SSH on the front-end, so port 8000 answering is the "already
installed" marker and the install stages are skipped (a rerun on an installed system reports
`changed=0`). A VM left off part-way resumes from the right stage. `hcp_replace=true` destroys and
rebuilds the VM, but only one whose libvirt description carries the hcp-vm marker.

Everything after the install is `post_install.yml` (below).

## post_install.yml and its steps

`post_install.yml` runs four playbooks in order. Each is also a playbook (and an AAP job
template) of its own, so any step can be rerun alone:

| Playbook | Does | Skipped in post_install.yml when |
|---|---|---|
| `first_login.yml` | security account password and roles, SSH, MAPI | never |
| `prometheus.yml` | Monitoring > Prometheus > *Enable Prometheus* | never (`hcp_prometheus=false` turns it off) |
| `tenant.yml` | initial tenant, its quotas and namespace defaults | `hcp_tenant_name` is blank |
| `node_exporter.yml` | node_exporter, firewalled, registered with Prometheus | `hcp_node_exporter_allow` is empty |

All of them reach the node the same way (`tasks/backend_open.yml` / `backend_close.yml`): a new
system has front-end SSH off, so they run from the hypervisor over the back-end. The hypervisor
gets a temporary address on `hcp-backend` (`<back-end /24>.1`, or `hcp_backend_host_ip`), and the
HCP root key (in root's `authorized_keys_be`) reaches `root` on the node's back-end address. The
key is copied to the hypervisor for the run and removed after, as is the address. HCP's Python
(3.6) is too old for Ansible modules, so the node is driven with `ssh`/`scp` and HCP's own
`admin jvm` commands. Every step is rerun-safe.

### first_login.yml

The steps the System Management Console asks for at first login, without the console:

| Step | How |
|---|---|
| Security account password | `admin jvm userAccount update cluster <id> --password …`, only while HCP still demands a change |
| Security account roles | same command, `--roles` (survey multiselect; default all six) |
| SSH on the front-end | `admin jvm config set cluster ssh.enabled true` |
| Management API | `admin jvm gateway mapi start <cluster-uuid>` |

Checks: port 22 answers on the front-end, and a MAPI call
(`/mapi/tenants`) as the security account returns 200, which proves the password and MAPI together.
Reruns change nothing in HCP.

### prometheus.yml

The console checkbox is the cluster setting `prometheus.server.enabled`
(`admin jvm config set cluster prometheus.server.enabled true`). HCP then runs its own Prometheus
server on the node (`:9097`); the play waits for it to listen. Console:
`https://admin.<system>:9094`. TSDB retention (`hcp_prometheus_retention_days`, default 365;
`hcp_prometheus_retention_gb`, default 300) is set first: HCP writes it into the server's command
line when it starts it. If the running server still shows other values, the play turns it off and
on again and checks the command line.

### tenant.yml

Creates the tenant through MAPI as the security account (MAPI is the documented way to say
*namespace quota: none*): name, description, hard quota (`hcp_tenant_quota` in GB or TB), soft
quota (85%), namespace quota none, versioning allowed (`hcp_tenant_versioning`), retention
compliance not (`hcp_tenant_compliance`; neither can be turned off later), and its first user
(`hcp_tenant_admin_user`). HCP gives that user only the Security role, so the play sets its roles
to `hcp_tenant_admin_roles` with `admin jvm userAccount update <tenant>`: Monitor, Administrator
(`SYSTEM`), Security and Compliance by default. It also ticks *Allow system-level users to
manage this tenant and search its namespaces* (`hcp_tenant_admin_allowed`,
`admin jvm tenant update <tenant> --administrationAllowed true`). An existing tenant keeps its user's password;
quotas and roles are brought in line. Then its namespace defaults, with
`admin jvm namespaceDefaults update`:

| Console setting | Value | Variable |
|---|---|---|
| Protocol optimization | cloud protocols only (`CLOUD`) | `hcp_ns_cloud_optimized` |
| Directory usage | balanced (`BALANCED`) | `hcp_ns_directory_usage` |

### node_exporter.yml

The hypervisor downloads the node_exporter release (`hcp_node_exporter_version`, checked against
the release's `sha256sums.txt`) into `/var/cache/hcp-vm` and copies to the node only what differs:

| On the node | What |
|---|---|
| `/var/node-exporter/node_exporter` | the binary, `--collector.systemd --collector.processes --web.disable-exporter-metrics` |
| `/var/node-exporter/firewall.sh` | own iptables chain `node-exporter`: ACCEPT each source in `hcp_node_exporter_allow`, DROP the rest; one `INPUT` jump ahead of HCP's `block` chain |
| `node-exporter.service` | runs `firewall.sh` first (`ExecStartPre`), then node_exporter |
| `node-exporter-firewall.timer` | reapplies the chain every 5 minutes, in case HCP rebuilds its firewall |

Then the node is added to Prometheus's file_sd targets: a file
`<system>-<node>.yml` in `hcp_prometheus_targets_dir` on `hcp_prometheus_host`, owned like that
directory. That is the host path of a directory Prometheus reads with `file_sd_configs`; for a
containerized Prometheus, the host side of the mount (e.g. `<config dir>/targets/nodes` when the
config dir is mounted at `/etc/prometheus`). Prometheus picks it up without a reload;
`destroy.yml` removes it again. An HCP upgrade or node rebuild may drop the files: rerun the playbook.

## destroy.yml

Survey: hypervisor, short name, back-end IP, and the VM name typed again as `confirm`.
Removes the domain and its disks (never the ISO), this node from the delegation (the zone when it
was the last node; NIOS removes its auto-created PTR with it), the IPAM host record, and any plain
PTR left at one of its addresses, and the node's Prometheus targets. Refuses anything without
the hcp-vm marker.

## Setup

1. **ISO on each hypervisor**, path in host vars: `hcp_iso_path`, plus `hcp_os_disk_dir` and
   `hcp_data_disk_dir` (see `inventory.example.yml`). The installer kernel and initrd are copied
   out of it once, to `<hcp_os_disk_dir>/.hcp-installer-<version>/`.
2. **NIOS**: the `aap-ipam` account (see `ipam-vm`) also needs permission to create and delete
   delegated zones under the parent zone.
3. **AAP credential type and job templates**: run once (rerun to refresh survey defaults after
   changing site values):

   ```bash
   # aap.local.yml (git-ignored, 0600): aap_url, aap_token (AAP API token with write scope),
   # aap_project, aap_inventory, aap_machine_credential, aap_secrets_credential
   ansible-playbook hcp-vm/playbooks/aap-setup.yml -e @site-vars.yml -e @aap.local.yml
   ```

   It creates the credential type **HCP VM secrets** (NIOS API password; HCP root SSH private
   key, injected as a 0600 file), the job templates **HCP VM deploy**, **HCP VM destroy**,
   **HCP post-install** (all steps) and one per step (**HCP first-login**, **HCP Prometheus**,
   **HCP tenant**, **HCP node_exporter**) with their surveys, and the workflow
   **HCP VM deploy + post-install** (post-install runs when deploy succeeds; its survey asks
   every question once). Create a
   credential of the new type, then rerun with `aap_secrets_credential` set so it gets attached.
   AAP 2.5+ gateways may refuse Basic auth on the API ("Authentication credentials were not
   provided"): use `aap_token` (user menu, API tokens, write scope) instead of user/password.
   Turn on **Update Revision on Launch** for the AAP project, so every job runs the latest
   commit without a manual sync.
4. **AAP project**: turn on *Update Revision on Launch* so each job runs the latest commit;
   without it a job keeps using whatever revision the last sync pulled.
5. **CLI** instead of AAP: `playbooks/group_vars/all/vault.yml.example` lists the secrets.
   `inventory.yml`, `site-vars.yml`, `vault.yml` and `*.local.yml` are git-ignored.

## Survey (HCP VM deploy)

| Group | Fields |
|---|---|
| Placement | hypervisor, vCPUs (≥4), memory GB (≥32), OS disk GB (≥100), data disk GB (≥500), data disk count (≥2), replace existing |
| Identity | short name, DNS domain, VM name, serial number, distributor key |
| Front-end | IP (blank = NIOS), prefix, gateway, DNS servers, downstream DNS mode, use NIOS |
| Back-end | IP (last octet 101-254 = node number), multicast address |
| Time | time server, time zone |
| Options | replication, encryption, minimum TLS |
| Secret | new `install` password (14+) |

The post-install templates all start with hypervisor, short name and back-end IP, then:

| Template | Asks |
|---|---|
| HCP first-login | security account password (14+), roles, SSH on/off, MAPI on/off |
| HCP Prometheus | enable on/off, TSDB retention days (default 365) and size GB (default 300) |
| HCP tenant | tenant name, hard quota and unit (GB/TB), tenant admin user, password and roles, allow system-level management, security account password (for MAPI) |
| HCP node_exporter | allowed scrapers (comma-separated addresses/CIDRs), version, Prometheus server (inventory host) and its file_sd directory |
| HCP post-install | all of the above once; a blank tenant name or allow list skips that step |

**HCP VM destroy** also asks for the Prometheus server and file_sd directory, to remove the
node's target. Defaults come from your site values: put `hcp_node_exporter_allow`,
`hcp_prometheus_host` and `hcp_prometheus_targets_dir` (your Prometheus server's address,
inventory name and file_sd directory) and,
if you like, `hcp_tenant_name` in `site-vars.yml` so the workflow opens pre-filled.

Everything else (MTUs, lockdown, MDPL, filesystem, timeouts, installer kernel arguments) is a role
default in `playbooks/roles/hcp_vm_defaults/defaults/main.yml`, overridable from inventory.

## Things learned the hard way

- The ISO's boot line puts `console=tty0` last, so the installer talks to VGA, not serial.
  `virsh send-key` works for a one-off; booting its kernel with `tty0` first and `ttyS0` last
  is the repeatable fix.
- On a VM the OS installer's Network Setup already configures front-end and back-end; the
  install menu's *Configure HCP Network* step is not needed.
- A freshly installed node accepts no SSH key and offers only public-key auth until arc-deploy
  runs; root's front-end key file is `authorized_keys_fe`, not `authorized_keys`.
- The OS installer extracts arc-deploy into `/home/install` itself (`Packages/postInstall`).
- NIOS auto-creates a PTR for a delegation's glue and refuses to delete it; it goes with the zone.
- `pgrep -f arc-deploy.py` matches the very shell running it; match the `install` user's
  Python process instead.
- arc-deploy hands the install to a daemon and switches the default systemd target; a
  transient unit holding arc-deploy needs `IgnoreOnIsolate=yes` or it is stopped mid-install.
- An installed HCP closes front-end SSH, but the back-end sshd stays up for the cluster's own
  use, which is what post-install goes through.
- The console's first-login settings map to `admin jvm` commands; the web actions in
  `/var/ris/logs/admin_request.log` show which page did what. Console checkboxes are cluster
  settings (`admin jvm config get|set cluster <name>`), named in
  `/opt/arc/config/configObjectNames.properties`.
- HCP 10.1's Python is 3.6: current Ansible can't run modules on the node.
- MAPI won't create a tenant without `versioningConfigurationEnabled` and
  `complianceConfigurationEnabled` in the request, and gives the tenant's first user only the
  Security role. In `admin jvm`, the console's Administrator role is `SYSTEM`.
- HCP bakes Prometheus's TSDB retention into the server's command line
  (`/opt/arc/prometheus-server/prometheus.sh`) when it starts the server: change the setting,
  then turn Prometheus off and on.
- An HCP node's `hostname` is already fully qualified (`hcp-node-101.<system>`).
- Ansible's `find` `contains` matches from the start of each line: prefix the pattern with `.*`.
