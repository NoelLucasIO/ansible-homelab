# chrony_source

Points chronyd at the time server(s) you name, restarts it, waits for it to synchronise, and
prints `chronyc tracking` and `chronyc sources` as proof. A host that doesn't synchronise
within `chrony_source_wait` seconds fails, so a wrong name or blocked UDP 123 shows up as a
failed host, not a silent drift.

| Distro | File | Service |
|---|---|---|
| Fedora, RHEL family, Arch/CachyOS | `/etc/chrony.conf` | `chronyd` |
| Debian, Proxmox VE/PBS | `/etc/chrony/chrony.conf` | `chrony` |

Only the time-source lines change:

- a managed block at the top of the file holds `server <name> iburst`, one per server;
- every other `pool`, `server`, `peer` and `sourcedir` line is commented out (marked
  `# off: chrony-source.yml`), which also drops DHCP-supplied servers (`sourcedir
  /run/chrony-dhcp`) and Debian's `sources.d`;
- the distro's own settings (`driftfile`, `makestep`, `rtcsync`, `ntsdumpdir`, ...) stay.

Rerunning with the same server changes nothing; with a new one, the block is rewritten.
Not covered: `server` lines in Debian's `confdir` (`/etc/chrony/conf.d/*.conf`).

Skipped, with the reason printed: hosts without chrony installed, `chrony_source_skip: true`,
`tideline_skip: true`, and hosts named in `tideline_appliances`.

**Proof** is three checks after the restart: `chronyc waitsync` succeeds, exactly one source
is selected (`^*`), and `Leap status` is `Normal`. All other sources are off, so the selected
one is yours.

## Running it

```
# from the repo root
ansible-playbook -i inventory.yml playbooks/chrony-source.yml -e chrony_source_server=ntp.example.com --check --diff -K
ansible-playbook -i inventory.yml playbooks/chrony-source.yml -e chrony_source_server=ntp.example.com -K
```

**In AAP:** `ansible-playbook playbooks/chrony-source-aap.yml -e @hcp-vm/aap.local.yml` creates
the job template **Chrony time source** (inventory `homelab`, credential `homelab-ssh`, survey
for the time server and the host pattern, job type asked on launch). No schedule.

## Settings

| Variable | Default | |
|---|---|---|
| `chrony_source_server` | (required) | DNS name or IP; several, comma-separated |
| `chrony_source_hosts` | `all` | inventory pattern |
| `chrony_source_options` | `iburst` | appended to each `server` line |
| `chrony_source_only` | `true` | comment out every other source |
| `chrony_source_wait` | `30` | seconds to wait for synchronisation |
| `chrony_source_skip` | (per host) | leave a host out |

VMs built by `ipam-vm/` get `chrony.conf` from the `baseline` role (`baseline_ntp_servers`).
Rerunning that role overwrites what this playbook set, so change `baseline_ntp_servers` too.
