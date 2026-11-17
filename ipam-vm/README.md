# ipam-vm

Builds KVM VMs on fedora whose address and DNS come from NIOS IPAM, and tears them down again.

## provision.yml (`-e vm_name=web01`)

1. **Address:** reuses the NIOS host record if `web01.example.com` already exists (reruns are safe). Otherwise it asks NIOS for 5 free addresses in 192.0.2.0/24 and pings each. It takes the first that doesn't answer, which avoids devices that have no DNS record.
2. **Host record:** creates a NIOS host record (A + PTR in one object), tagged with `vm_owner_comment`. NIOS notifies Technitium; Unbound serves it to clients.
3. **Disk:** a qcow2 overlay on `rocky-9-genericcloud-base.qcow2`, sized `vm_disk_gb`. cloud-init grows the root filesystem on first boot.
4. **Seed ISO:** a NoCloud ISO (`cidata`) with hostname, static IP, gateway, DNS, and user `ansible` holding the homelab-ssh key with passwordless sudo.
5. **VM:** `virt-install --import` on br0.
6. **Checks:** waits for SSH on the new address, then checks the name resolves through Unbound.

Optional: `-e vm_memory_mb=8192 -e vm_vcpus=4 -e vm_disk_gb=80`

## decommission.yml (`-e vm_name=web01 -e confirm=web01`)

Destroys and undefines the VM, removes its disk and ISO, and deletes the host record, which frees the IP. It refuses unless the NIOS record carries the ipam-vm comment, so hand-built VMs can't be removed with it.

## Setup

1. **SSH keys:** set `vm_ssh_pubkeys` (inventory variable). The key whose comment contains `aap-automation` goes to the automation user; the rest go to `baseline_admin_user`.
2. **Secrets.** The playbooks need four: `nios_api_password`, `npm_identity`, `npm_secret`, `baseline_admin_password_hash` (`openssl passwd -6`).
   - **CLI:** copy `playbooks/group_vars/all/vault.yml.example` to `vault.yml`, fill it in, then `ansible-vault encrypt` it (it is git-ignored).
   - **AAP:** create a custom credential type that injects them as extra vars, and attach a credential of that type to both job templates:

     ```yaml
     # Input configuration
     fields:
       - {id: nios_api_password, label: NIOS API password, type: string, secret: true}
       - {id: npm_identity, label: NPM login email, type: string}
       - {id: npm_secret, label: NPM password, type: string, secret: true}
       - {id: baseline_admin_password_hash, label: Admin password hash (SHA-512 crypt), type: string, secret: true}
     required: [nios_api_password, npm_identity, npm_secret, baseline_admin_password_hash]
     ```

     ```yaml
     # Injector configuration
     extra_vars:
       nios_api_password: '{{ nios_api_password }}'
       npm_identity: '{{ npm_identity }}'
       npm_secret: '{{ npm_secret }}'
       baseline_admin_password_hash: '{{ baseline_admin_password_hash }}'
     ```
3. **AAP job templates**, each with the machine credential and the secrets credential:
   - **VM provision**, playbook `ipam-vm/playbooks/provision.yml`. Survey: `vm_name` (text, required), `vm_vcpus`, `vm_memory_mb`, `vm_disk_gb` (integers, with defaults 2 / 4096 / 40).
   - **VM decommission**, playbook `ipam-vm/playbooks/decommission.yml`. Survey: `vm_name`, `confirm`.

## Tested here vs. on first run

- **Tested:** syntax check of both playbooks; cloud-init user-data and network-config pass cloud-init 26.2's schema validator; the seed ISO builds with label `cidata` and the three files.
- **First run will show:**
  - that `rocky9` is a known `--os-variant` on fedora. If not, use `rhel9.0`.
  - that the NIC name matches `e*` in the Rocky cloud image.
  - that the WAPI `record:host` create returns 201 with the record's reference.
- **Ping caveat:** a device that drops ICMP looks free. Run NIOS Discovery on the subnet occasionally to catch those.
- **Credentials:** this uses the NIOS `admin` account. Next step: a dedicated NIOS API user limited to IPAM/DNS on 192.0.2.0/24 and example.com.

## Baseline (roles/baseline)

`provision.yml` now hands each new VM to a second play that applies the baseline. `baseline.yml` applies the same role to existing hosts: `-e baseline_target=<inventory name>`.

| Area | What it sets |
| --- | --- |
| Users | `ansible`: AAP key, passwordless sudo. `baseline_admin_user`: your workstation key, wheel, sudo asks for the password (hash in the vault: `baseline_admin_password_hash`). |
| Time | America/New_York; chrony uses gw (192.0.2.1) only. |
| Packages | Admin basics, cockpit, qemu-guest-agent, dnf-automatic (daily security updates, applied). |
| Journal | Persistent across reboots. |
| Firewall | firewalld on with ssh, cockpit, 9100/tcp. Only for new VMs. `baseline.yml` leaves firewalld alone unless `-e baseline_manage_firewalld=true`. |
| Monitoring | node_exporter 1.9.1 on :9100, checksum-verified. Registers itself in Prometheus via file_sd on fedora; decommission removes it. |
| Logs | rsyslog sends everything to Loki (Alloy TCP 1516, RFC 5424) and `info`+ to Splunk (TCP 1516, index `linux`), with disk queues. Under SELinux, 1516/tcp is labelled as a syslog port first. |
| NIOS | Optional `vm_owner` / `vm_role` set the Owner and Role extensible attributes on the host record. |

### One-time server-side setup

**Prometheus**

1. `mkdir -p <baseline_prometheus_targets_dir>` on the Prometheus host (mounted in the container as /etc/prometheus/targets).
2. Add this job to prometheus.yml:
   ```yaml
     - job_name: node-vms
       file_sd_configs:
         - files: ['/etc/prometheus/targets/nodes/*.yml']
   ```
3. Check the config with promtool and reload.

**Alloy (netmon)**

1. Append to the Alloy config on the log relay:
   ```hcl
   loki.source.syslog "linux" {
     listener {
       address  = "0.0.0.0:1516"
       protocol = "tcp"
       labels   = { job = "linux" }
     }
     relabel_rules = loki.relabel.syslog.rules
     forward_to    = [loki.write.local.receiver]
   }
   ```
2. Restart: `systemctl --user restart alloy`

**Splunk (idx)**

1. Create the app /opt/splunk/etc/apps/lab_linux_input/local/ with two files.

   inputs.conf:
   ```
   [tcp://1516]
   acceptFrom = 192.0.2.0/24
   sourcetype = syslog
   index = linux
   connection_host = dns
   ```
   indexes.conf:
   ```
   [linux]
   homePath   = $SPLUNK_DB/linux/db
   coldPath   = $SPLUNK_DB/linux/colddb
   thawedPath = $SPLUNK_DB/linux/thaweddb
   ```
2. `sudo -u splunk /opt/splunk/bin/splunk restart`

**NIOS (optional)**

Under Administration → Extensible Attributes, create `Owner` and `Role` (String). Then add `vm_owner` and `vm_role` to the VM provision survey.

## Cockpit through NPM

Provision creates two things:

- an NPM proxy host `<vm>-cockpit.example.com` → `https://<vm-ip>:9090`, using NPM's newest `*.example.com` certificate and the NPM access list `LAN only`;
- an A record for that name, pointing at NPM (192.0.2.45), via `aap-ipam`.

The baseline writes `/etc/cockpit/cockpit.conf` so Cockpit accepts that origin. Decommission removes the proxy host and the A record, but only an A record that carries the ipam-vm comment.

One-time setup in NPM:

- **Access Lists → Add**: `LAN only`, allow `192.0.2.0/24`, then deny all.
- **Users → Add**: an AAP user. Set visibility to All items, Proxy Hosts to Manage, Certificates and Access Lists to View Only, and everything else to Hidden.

Its login is `npm_identity` (email) and `npm_secret` (see Secrets above).
