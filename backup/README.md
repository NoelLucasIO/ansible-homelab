# backup

Backups for the lab, built around **Proxmox Backup Server 4.2**: a PBS VM on KVM installed
unattended from the vendor ISO, file-level backups of the Linux hosts, image-level backups of
selected KVM domains, client-side encryption, an offsite copy on **Backblaze B2** (S3), and
metrics, alerts, logs and a dashboard in the existing Prometheus / Loki / Grafana stack.

The PBS web UI is the console for jobs, tasks, snapshots, verify state and restores.

```
 fedora (KVM host)                                    PBS VM (pbs01)
 ┌───────────────────────────────┐   HTTPS 8007     ┌──────────────────────────────┐
 │ pbs-backup-vm.timer           │  token per host  │ datastore store1 (vdb, XFS)  │
 │  virtnbdbackup -> local chain ├─────────────────►│  ns kvm / ns hosts           │
 │ pbs-backup-host.timer         │  encrypted       │  prune, verify, GC           │
 │  /etc /home /990 + pg dumps   │  chunks only     │ b2-offsite (S3, cache vdc)   │
 └───────────────────────────────┘                  │  sync job: encrypted only ───┼──► Backblaze B2
 p620 (CachyOS): pbs-backup-host.timer ────────────►│                              │
                                                    └──────────────────────────────┘
 Prometheus: pbs-exporter + textfile metrics from each job  ·  Loki: job journal + PBS syslog
```

## What runs where

| Piece | Where | Notes |
|---|---|---|
| PBS | KVM VM on `pbs_kvm_host` | vda OS, vdb datastore, vdc S3 cache. Configured only through its REST API from the controller (no SSH, no agent). |
| File backups | every host in `pbs_clients` | `pbs-backup-host.timer`: one pxar archive per path, database dumps first, metadata change detection (unchanged files are not re-read). |
| VM backups | hypervisors (`pbs_vm_backup_enabled`) | `pbs-backup-vm.timer`: [virtnbdbackup](https://github.com/abbbi/virtnbdbackup) keeps a monthly chain per domain (full, then incrementals from libvirt dirty bitmaps); each night the chain is uploaded, so every PBS snapshot is a complete, restorable chain. |
| Restore test | every client | `pbs-restore-test.timer`, weekly: restores the newest `etc` archive and compares it with live files. |
| Offsite | inside PBS | S3 datastore on a B2 bucket and a local sync job with `encrypted-only`, so B2 only ever holds ciphertext. Its own, longer retention. |
| Monitoring | `pbs_prom_host` | pbs-exporter (rootless Quadlet), scrape config, alert rules, node_exporter on the PBS VM, Grafana dashboard *Backups (PBS)*. |

The jobs are systemd timers on the hosts, not AAP schedules: backups keep running when AAP
(itself one of the VMs being backed up) is down. AAP deploys and changes them.

## Playbooks

| Playbook | Does |
|---|---|
| `pbs-deploy.yml` | Address from NIOS, auto-install ISO, VM, unattended install, first boot, datastore and jobs, users and tokens, offsite, NPM proxy for the UI, monitoring. Rerun-safe; an existing VM is left alone. |
| `clients.yml` | On `pbs_clients`: verified client binary, one API token per host, the shared encryption key, textfile collector, job scripts and timers. `-e pbs_run_now=true` runs the jobs once and waits. |
| `restore-vm.yml` | Restores a domain's chain from PBS and rebuilds its disks with `virtnbdrestore` into a scratch directory (never over the running VM). |
| `aap-setup.yml` | Credential type *PBS secrets*, credential, group `pbs_clients`, job templates *PBS deploy* and *Backup clients*. |

### How the install works

1. `proxmox-auto-install-assistant` doesn't exist for Fedora, so it runs in a `debian:trixie`
   container, installed from Proxmox's repository with the archive keyring. It validates the
   answer file (`templates/answer.toml.j2`, PBS 4 kebab-case keys) and embeds it, plus the
   first-boot script, into a copy of the ISO.
2. The VM boots that ISO; the installer picks *Automated Installation* after 10 s, installs onto
   vda only (matched by MAC for the NIC), and powers off (`reboot-mode = "power-off"`), which is
   how the play knows it finished. The ISO is ejected and deleted (it holds the root password
   hash; the plaintext never goes into it).
3. First boot (`templates/first-boot.sh.j2`, log `/var/log/pbs-first-boot.log`): enterprise
   repository off, no-subscription on, full upgrade, qemu-guest-agent, node_exporter, rsyslog to
   Alloy (RFC 5424/TCP). The play waits for node_exporter to answer.
4. API: vdb is formatted as an XFS directory datastore, namespaces `kvm` and `hosts`, prune
   (keep-last 3, daily 14, weekly 8, monthly 12, yearly 2), verify (daily, re-verify after 30
   days), weekly GC, user `backup@pbs` (DatastoreBackup), user `monitor@pbs` (Audit) with the
   exporter's token.

### Clients, tokens and the encryption key

* **Binary:** `proxmox-backup-client-static` from `download.proxmox.com/debian/pbs-client`,
  checked before it runs: keyring over HTTPS → `gpgv` on the signed `InRelease` → Packages
  index hash → `.deb` SHA-256 → the binary. Pinned by `pbs_client_version`.
* **Tokens:** `backup@pbs!<host>`, DatastoreBackup on the datastore only. A host can write and
  read its own groups, and can't delete, prune or see other hosts' backups. The secret is shown
  once by PBS and kept only in the host's `/etc/pbs-backup/client.env` (root 0600). Delete that
  file (or pass `-e pbs_rotate_tokens=true`) to rotate.
* **Encryption key:** one key for all hosts, so any host can restore any backup. The first run
  makes it on the first client and prints only its fingerprint (the key never goes into job
  output): **read `/etc/pbs-backup/encryption-key.json` on that host as root, put the JSON into
  the credential as `pbs_client_encryption_key`, and print its paper key**
  (`proxmox-backup-client key paperkey <file> --output-format text`). Without it, nothing can
  be restored, including from B2. Later runs take the key from the credential and refuse to
  overwrite a different key on a host.

### Offsite on Backblaze B2

1. In B2: a **private** bucket, lifecycle **"Keep only the last version"** (PBS garbage
   collection deletes chunks; with B2's default "keep all versions" the deleted chunks stay as
   hidden versions and are still billed), and an application key restricted to that bucket.
2. Inventory: `pbs_offsite_enabled: true`, `pbs_offsite_bucket`, `pbs_offsite_region` (e.g.
   `us-east-005`, from the bucket's endpoint). Credential: `pbs_offsite_access_key` (keyID),
   `pbs_offsite_secret_key` (applicationKey).
3. Rerun `pbs-deploy.yml`. The S3 endpoint uses PBS's `skip-if-none-match-header` quirk (B2
   rejects that header) and `rate-out 12500KB` (100 Mbit/s, half the 200 Mbit/s uplink; raise `pbs_offsite_rate_out` later). The sync
   job runs at 06:00 every day with `encrypted-only`, so an unencrypted snapshot is never uploaded.
   B2 keeps 7 daily, 8 weekly, 24 monthly, 3 yearly; verify runs monthly (it downloads,
   within B2's free egress of 3× storage).

The first sync is the whole datastore: at 100 Mbit/s that's about 1.08 TB a day.

## Monitoring

| Alert | When |
|---|---|
| BackupStale | a host or VM job hasn't succeeded in `pbs_stale_hours` (30) |
| BackupFailed | the last run of a job exited non-zero |
| RestoreTestStale | no passing restore test in `pbs_restore_stale_days` (9) |
| PBSDown | the exporter can't reach PBS for 10 min |
| PBSDatastoreFilling | a datastore is over `pbs_store_full_pct` (85 %) |
| PBSVerifyOverdue | a group's newest verify is over 45 days old |
| OffsiteBehind | the B2 copy of a group is more than 2 days behind the local one |

Job metrics (`pbs_backup_last_success_timestamp_seconds`, `_last_exit_code`, `_duration_seconds`,
labels `kind` = host / vm / restore and `target`) are written by the jobs to node_exporter's
textfile collector; `clients.yml` adds the collector to node_exporter with a drop-in if it's
missing. The job logs are in the journal (`journalctl -u pbs-backup-host -u pbs-backup-vm`),
which Alloy already ships to Loki; the PBS VM's syslog goes to Alloy at first boot.

## Restores and on-demand backups

Three AAP job templates with surveys (made by `aap-setup.yml`; machine credential only, since they
use the token and key already on each host). From the CLI, pass the same variables with `-e`.

| Template | Playbook | Survey |
|---|---|---|
| **Backup now** | `backup-now.yml` | hosts; files / VMs / both; VMs (empty = all the host backs up). Waits for the jobs; shares a lock with the nightly timers, so it never doubles a run. |
| **Restore files** | `restore-files.yml` | target host; source `store1` (local) or `b2-offsite` (B2); whose backup (empty = the target's own); snapshot `latest`, a UTC date (newest that day) or a time; archive; optional paths, one per line, inside the archive or as full paths (a directory brings its contents); mode; confirmation. |
| **Restore VM** | `restore-vm.yml` | hypervisor; source; VM; snapshot; optional new name, optional checkpoint (`--until`). |

* **Files** land in `/var/tmp/pbs-restore/<id>-<archive>-<snapshot>-<now>/`; live files are not
  touched. **In place** needs mode `in-place` *and* the confirmation `OVERWRITE <short host name>`, and
  only takes the host's own archives: it restores to staging first, then copies over the live path
  (rsync, nothing deleted, directory times left alone); every file it replaces is kept in
  `<staging>.replaced/`.
* **VMs** come back as a **new** domain beside the original (`<VM>-restored-<UTC time>` by default)
  in `<pbs_vm_backup_dir>/restore/<name>/`: new name, no UUID or MAC carried over, **network links
  down**, defined but not started and not autostarted. Safe to start next to the original; put it on
  the LAN with `virsh domif-setlink <name> <mac> up` only while the original is off (same guest IP).
  qemu gets traverse-only ACLs (`u:qemu:x`) on the path to the disks; the decrypted chain is in a
  root-only work directory and removed at the end; disks are 0600.
* **Tokens:** restores use a separate read-only token per host (`restore@pbs!<host>`, DatastoreReader
  on `store1` and `b2-offsite`, in `/etc/pbs-backup/restore.env`), so a host can restore any host's
  backups from either store; the nightly backup token still sees only its own groups. Restores from
  B2 count as egress (free up to 3x what's stored).
* **Facts:** `clients.yml` writes `/etc/ansible/facts.d/pbs_backup.fact` (archives and their paths,
  VM list, directories); these playbooks read it, so they need no inventory host vars (the AAP
  inventory doesn't carry them). Rerun `clients.yml` after changing a host's backup settings.
* By hand, on any client:
  ```bash
  set -a; . /etc/pbs-backup/restore.env; set +a
  export PBS_REPOSITORY="$PBS_RESTORE_AUTH@$PBS_RESTORE_SERVER:store1"   # or :b2-offsite
  proxmox-backup-client catalog shell --ns hosts host/fedora etc.pxar --keyfile /etc/pbs-backup/encryption-key.json
  ```
  The local chains in `pbs_vm_backup_dir` (current and previous month) restore faster still:
  `virtnbdrestore -i <dir>/<domain>/<YYYY-MM> -o <out>`.
* **PBS itself lost:** a new VM from `pbs-deploy.yml` with the datastore disk reattached reuses it
  (the play mounts nothing it didn't make; see the error message it prints). From B2 only: create
  the S3 datastore with *reuse existing* on any PBS 4.2, then restore with the key.

## Setup

1. **ISO** on the hypervisor at `pbs_iso`; `podman`, `virt-install`, `qemu-img`, `openssl` there.
2. **Site values** in the inventory (or `site-vars.yml`): see `inventory.example.yml`. Per host:
   `pbs_host_backup_paths`, `pbs_host_backup_extra_excludes`, `pbs_pg_dumps`, and on hypervisors
   `pbs_vm_backup_enabled`, `pbs_vm_backup_domains`, `pbs_vm_backup_dir`.
3. **Secrets:** `pbs_root_password` (12+), later `pbs_client_encryption_key`, and the B2 keys:
   credential *PBS secrets* (`aap-setup.yml`), or `playbooks/group_vars/all/vault.yml`
   (`vault.yml.example`). NIOS/NPM: the existing *ipam-vm secrets* credential.
4. **Run** *PBS deploy*, then *Backup clients*. CLI (ipam-vm's vault supplies the NIOS/NPM keys):
   ```bash
   ansible-playbook playbooks/pbs-deploy.yml -i inventory.yml -e @../site-vars.yml \
     -e @../ipam-vm/playbooks/group_vars/all/vault.yml --ask-vault-pass
   ansible-playbook playbooks/clients.yml -i inventory.yml -e @../site-vars.yml --ask-vault-pass -e pbs_run_now=true
   ```

Exclude patterns are gitignore-style. `pbs_host_backup_excludes` and `_extra_excludes` apply
to every archive; a leading `/` anchors at that archive's root. For an allow-list on one
archive, give the path a `pxarexclude:` list: it is written to `<path>/.pxarexclude`, which PBS
applies to that tree only (`/*` excludes everything, `!/dir` brings a directory back, last match
wins). The lab's `/990` archive works this way: configs and Forgejo, no data directories.
Live database directories are excluded and dumped instead; telemetry stores (Prometheus TSDB,
Loki, OpenSearch, ClickHouse) are excluded by the lab inventory as replaceable.

## Choices

* **virtnbdbackup, not Veeam/Commvault:** neither protects plain libvirt; both need oVirt,
  Proxmox VE or (Commvault 11.46) Incus. virtnbdbackup uses QEMU's backup API and dirty bitmaps
  directly (qcow2 disks; raw disks are skipped unless `pbs_vm_backup_raw_disks`).
* **Shut-off domains** get a full copy when a disk changed since the last one, otherwise the
  previous copy is uploaded again (no read).
* **Appliance VMs** (NIOS, HCP, vTM) are best restored from their own backups; the images are a
  fallback.

## Status

Run on 2026-11-22 and 2026-11-23 against the lab (PBS 4.2-1 ISO, Fedora 44 KVM host, CachyOS workstation,
CLI from the workstation):

* **pbs-deploy.yml:** unattended install from the ISO (about 2 minutes to power-off), first boot,
  NIOS host record, NPM proxy, datastore on vdb, namespaces, prune/verify/GC, users, exporter
  token; reruns change nothing. Exporter up (`pbs_up 1`), both Prometheus targets up, 7 rules loaded.
* **clients.yml:** verified client install (signed index → SHA-256 → package name), one token per
  host, shared encryption key (never printed), textfile collector added on the workstation,
  timers; reruns change nothing.
* **First file backup (fedora):** 30 s, exit 0, metrics written. The `/990` allow-list was checked
  in the snapshot's catalog: only the listed directories, the trimmed ones down to their config,
  569 Forgejo repository files, nothing from the excluded directories; Postgres dump present.
* **2026-11-23, VM backups:** all four domains (Ansible, Splunk, Infoblox-9.1.0, vtm01), exit 0,
  after removing `PrivateTmp` from the unit (virtnbdbackup needs qemu's NBD socket in `/var/tmp`).
  The Ansible VM's internal snapshots did not block checkpoints.
* **2026-11-23, workstation:** first file backup, 209 GiB read, 178 GiB uploaded, 43 min, exit 0.
* **2026-11-23, offsite (Backblaze B2):** S3 endpoint capped at 100 Mbit/s, local pull sync with
  encrypted-only; first sync of the fedora snapshot OK, full first copy (~374 GiB) running.
* **2026-11-23, on-demand jobs:** from the CLI, file restores from `store1` and from `b2-offsite`
  (single file, directory), in-place restore refused without the confirmation and a no-op when
  identical, `restore-vm.yml` of vtm01 as a new domain (new UUID and MAC, link down) that booted,
  `backup-now.yml` for one VM. In AAP: `aap-setup.yml` made the three templates and surveys; a
  "Restore files" job ran successfully.
* **Not yet run:** the weekly restore test (first run Sunday), an in-place restore that actually
  replaces files, a Restore VM from `b2-offsite` (waits for the first full sync).
