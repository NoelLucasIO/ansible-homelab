# hcp-vm

Deploys a single-node HCP (Hitachi Content Platform) VM on KVM from the HCP ISO, unattended,
from an AAP job template whose survey carries every installer answer and the VM sizing.
Tested end to end with HCP 10.1.1.14 (ISO `HS222_10.1.1.14.iso`).

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

The first-login setup is `post_install.yml` (below). Tenants and namespaces are not automated yet.

## post_install.yml

The steps the System Management Console asks for at first login, without the console:

| Step | How |
|---|---|
| Security account password | `admin jvm userAccount update cluster <id> --password …`, only while HCP still demands a change |
| Security account roles | same command, `--roles` (survey multiselect; default all six) |
| SSH on the front-end | `admin jvm config set cluster ssh.enabled true` |
| Management API | `admin jvm gateway mapi start <cluster-uuid>` |

A new system has front-end SSH off, so this runs from the hypervisor over the back-end: the
hypervisor gets a temporary address on `hcp-backend` (`<back-end /24>.1`, or
`hcp_backend_host_ip`), and the HCP root key (in root's `authorized_keys_be`) reaches `root` on
the node's back-end address. The key is copied to the hypervisor for the run and removed after,
as is the address. Checks: port 22 answers on the front-end, and a MAPI call
(`/mapi/tenants`) as the security account returns 200, which proves the password and MAPI together.
Reruns change nothing in HCP.

## destroy.yml

Survey: hypervisor, short name, back-end IP, and the VM name typed again as `confirm`.
Removes the domain and its disks (never the ISO), this node from the delegation (the zone when it
was the last node; NIOS removes its auto-created PTR with it), the IPAM host record, and any plain
PTR left at one of its addresses. Refuses anything without the hcp-vm marker.

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
   key, injected as a 0600 file), the job templates **HCP VM deploy**, **HCP VM destroy** and
   **HCP post-install** with their surveys, and the workflow **HCP VM deploy + post-install**
   (post-install runs when deploy succeeds; its survey asks both sets of questions). Create a
   credential of the new type, then rerun with `aap_secrets_credential` set so it gets attached.
   AAP 2.5+ gateways may refuse Basic auth on the API ("Authentication credentials were not
   provided"): use `aap_token` (user menu, API tokens, write scope) instead of user/password.
4. **CLI** instead of AAP: `playbooks/group_vars/all/vault.yml.example` lists the secrets.
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

**HCP post-install** asks: hypervisor, short name, back-end IP, security account password (14+),
roles, SSH on/off, MAPI on/off.

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
  `/var/ris/logs/admin_request.log` show which page did what.
