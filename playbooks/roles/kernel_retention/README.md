# kernel_retention

At most **two kernels** on every Linux host: the **running** kernel plus the **newest other**
one. The running kernel is never removed, so a host that hasn't rebooted into its newest
kernel keeps exactly those two (fallback = what it is running now).

| Host | What a run does | Between runs |
|---|---|---|
| Fedora, RHEL, CentOS Stream, Rocky, Alma, Oracle | `installonly_limit=2` in `/etc/dnf/dnf.conf`; removes the kernels over the limit (`dnf remove` of their installonly packages: `kernel`, `-core`, `-modules*`, `-devel*`, `-uki-virt*`, and any kmods built for them) | dnf enforces the limit on every kernel update |
| Debian | removes `linux-image-<release>` (+ `-unsigned`, headers, modules, and the `-common` headers / `linux-kbuild` of an ABI no longer kept), purged | apt has no limit (autoremove keeps three); the weekly run enforces it |
| Arch, CachyOS | skipped: pacman keeps one version per kernel package | |
| Proxmox VE, PBS | skipped: `proxmox-boot-tool` manages their kernels and pins | |
| Containers (LXC, Podman, ...) | skipped: no kernel of their own | |
| Appliances, other distros | skipped: `kernel_retention_skip: true`, `tideline_skip: true`, a `tideline_appliances` entry, or not in `kernel_retention_distributions` | |

Never touched: `kernel-headers` and `kernel-tools` (one version, track the newest), the
`linux-image-amd64` metapackage, and variant flavours (`kernel-debug*`, `kernel-rt*`,
`kernel-64k*`). Versions are ordered with rpm's comparison (`filter_plugins/` in this role),
so `6.17.10` sorts above `6.17.9`. Each run ends with a check that at most two remain, and
prints per host what was kept and removed.

## Running it

```
# from the repo root
ansible-playbook -i inventory.yml playbooks/kernel-retention.yml --check -K   # the plan
ansible-playbook -i inventory.yml playbooks/kernel-retention.yml -K
```

**In AAP:** `ansible-playbook playbooks/kernel-retention-aap.yml -e @hcp-vm/aap.local.yml` creates the
job template **Kernel retention** (inventory `homelab`, credential `homelab-ssh`, survey for the
host pattern, job type asked on launch: pick **Check** for a dry run) and a weekly schedule,
Sundays 04:37 America/New_York.

## Settings

`defaults/main.yml`:

| Variable | Default | |
|---|---|---|
| `kernel_retention_hosts` | `all` | inventory pattern |
| `kernel_retention_keep` | `2` | 2 or more |
| `kernel_retention_set_config` | `true` | write `installonly_limit` (dnf) |
| `kernel_retention_purge` | `true` | remove the extras now |
| `kernel_retention_skip` | (per host) | leave a host out |
| `kernel_retention_distributions` | Fedora, RHEL family, Debian | anything else is skipped |

Removing a kernel takes its DKMS/akmod-built modules for that kernel with it (e.g.
`kmod-nvidia-<release>`); the kept kernels' modules are untouched.
