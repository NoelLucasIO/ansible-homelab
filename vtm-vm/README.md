# vtm-vm

Deploys one Ivanti vTM (Virtual Traffic Manager, formerly Zeus/Stingray/Pulse vADC) virtual
appliance on KVM from the vendor's KVM zip, fully unattended, from an AAP job template whose
survey carries every Initial Configuration answer and the VM sizing.

## What deploy.yml does

1. **Preflight** on the chosen hypervisor: survey checks, the zip present, installs
   `guestfs-tools`, `guestfish` and `unzip` if missing.
2. **Address**: the survey value, or the next free address in `vtm_network` from NIOS (pinged
   first), recorded as a NIOS host record (A + PTR). Reruns reuse the record.
3. **Base image**: the zip is unpacked once per version into `vtm_base_dir` and made read-only.
   The play checks the partition layout it relies on (root on partition 3, `/logs` on 6).
4. **Disk and VM**, prepared offline before the first boot:
   - `virt-resize` copies the base image into a thin qcow2 of `vtm_disk_gb` and grows `/logs`.
     Root stays at the appliance's fixed 4 GiB (partitions 3 and 4 are its A/B upgrade slots),
     so all space above the image's 16 GiB goes to `/logs`, where vTM keeps logs and data.
     Everything except `/logs` takes ~9.3 GiB, so `/logs` ≈ `vtm_disk_gb` − 9.3 GiB. The minimum
     for `/logs` is 8 GiB; the default of 30 gives ~20.7 GiB.
   - `guestfish` puts a `z-initial-config` answers file, a first-boot script and a one-shot
     systemd unit (`vtm-firstboot.service`) on the root filesystem.
   - `virt-install --import`: q35, `--os-variant ubuntu24.04` (the appliance is Ubuntu 24.04.5),
     NIC 1 on `br0` with a fixed MAC, NICs 2..n on `vtm_extra_nic_network`, serial console.
5. **First boot**: the unit finds the NIC carrying NIC 1's MAC, writes its name into the
   answers and runs `z-initial-config --replay-from=… --noninteractive`. That sets licence
   acceptance, hostname, address, gateway, DNS, time zone, admin password, SSH intrusion
   protection and the REST API, then starts the traffic manager. The answers file (it holds
   the password) and the licence copy are shredded afterwards.
   Log: `/logs/vtm-firstboot.log` in the VM.
6. **Checks**: the admin server answers on `https://<ip>:9090`, and the REST API accepts the
   new admin password. On failure the play prints the first-boot and startup logs, read from
   the VM's disk with `virt-cat`.
7. **NPM**: a LAN-only proxy host `https://<name>-admin.<domain>` to the admin UI, with NPM's
   wildcard certificate, plus an A record for it in NIOS.

A rerun against an existing VM skips the build and only runs the checks and the NPM step.
`vtm_replace=true` destroys and rebuilds the VM, but only one whose libvirt description carries
the vtm-vm marker.

## destroy.yml

Survey: hypervisor, name, domain, and the name typed again as `confirm`. It removes the domain
and its disk (never the base image), the NPM proxy host, and the NIOS host and A records that
carry the vtm-vm comment. It refuses any VM without the vtm-vm marker.

## Where the answers come from

The keys are the appliance's own (`z-initial-config --list-options`, vTM 22.9r7):
`accept_license`, `zta_register`, `register`, `license_key`, `hostname`, `net_<if>_addr`,
`net_<if>_mask`, `net_gateway`, `net_management`, `dns`, `search`, `timezone`, `password`,
`ssh_intrusion`, `rest_enabled`, `rest_port`. In non-interactive mode, a missing or invalid
**required** answer stops the run with `Failed to set <key>`. An invalid **optional** answer
is silently dropped. So the template leaves out optional keys that have no value instead of
sending them empty. See `templates/vtm-answers.j2`.

## Setup

1. **Image on each hypervisor**: copy the vendor zip to the path in `vtm_image_zip` (host vars;
   see `inventory.example.yml`). Licence key files (optional) also go on the hypervisor.
2. **Secrets**: the same keys as ipam-vm (`nios_api_password`, `npm_identity`, `npm_secret`),
   so the ipam-vm secrets credential works unchanged. The admin password is a survey answer.
   CLI: `playbooks/group_vars/all/vault.yml.example`.
3. **AAP job templates**: run once, and again after changing site values to refresh the survey defaults:

   ```bash
   ansible-playbook vtm-vm/playbooks/aap-setup.yml -e @site-vars.yml \
     -e aap_url=https://aap.example.com -e aap_user=admin -e aap_password=... \
     -e aap_machine_credential='homelab ansible' -e aap_secrets_credential='ipam-vm secrets'
   ```

   This creates **vTM VM deploy** and **vTM VM destroy** with their surveys, and attaches both credentials.

## Survey (vTM VM deploy)

| Group | Fields |
|---|---|
| Placement | hypervisor, vCPUs (≥1), memory GB (≥2), disk GB (≥20), NICs (1-8), where NICs 2..n attach, replace existing |
| Identity | name, DNS domain |
| Network | management IP (blank = NIOS), use NIOS, prefix, gateway, DNS servers, search domains, bind admin to the management IP |
| Initial configuration | time zone, SSH intrusion protection, REST API and port, licence key file (blank = developer mode), NPM proxy |
| Secret | admin password (8+) |

Everything else is a role default in `playbooks/roles/vtm_vm_defaults/defaults/main.yml` and can
be overridden from the inventory.

## Status

- **Read from the 22.9r7 KVM image itself, not from documentation:** the answer keys and their
  validation (from `/usr/bin/z-initial-config`), the partition layout, the OS release, and the
  appliance's boot services. vtm-firstboot runs after them.
- **Not yet run end to end.** The first real deploy will confirm:
  - that `vtm-firstboot.service` runs after the appliance's own network bring-up
    (`z-initial-network` raises DHCP on NIC 1 first, then `z-initial-config` replaces it with the static address);
  - the REST API path and port (`https://<ip>:9070/api/tm/`) on 22.9;
  - that `guestfish` is its own package on the hypervisor's distribution (it is on Fedora).
- Not automated: anything after the Initial Configuration, such as traffic IP groups, pools,
  virtual servers and syslog targets. With REST on, those can be a follow-up playbook.
