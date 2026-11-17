# ansible-homelab

[![ci](https://github.com/NoelLucasIO/ansible-homelab/actions/workflows/ci.yml/badge.svg)](https://github.com/NoelLucasIO/ansible-homelab/actions/workflows/ci.yml)

Ansible content for my home lab, run from **Ansible Automation Platform 2.7**
with a custom execution environment (ansible-core 2.20).

## What's here

| Path | What it does |
|---|---|
| `playbooks/bootstrap-host.yml` | Prepares a new host for AAP: automation user, validated sudoers, key, RHEL registration (`rhc`), SELinux mode, optional sysctls and updates. RHEL, Fedora and Arch. |
| `playbooks/observability-agents.yml` | Ships the system journal to Loki with Grafana Alloy (Podman quadlet), and installs the Splunk Universal Forwarder as a deployment client. Never installs a forwarder beside Splunk Enterprise. |
| `playbooks/ping.yml` | Connectivity and facts smoke test. |
| `ipam-vm/` | Provisions KVM virtual machines with addresses allocated from Infoblox NIOS (WAPI), cloud-init, DNS records, a baseline role (users, chrony, rsyslog forwarding, node_exporter, firewalld) and Cockpit published through Nginx Proxy Manager. Includes decommissioning. See [`ipam-vm/README.md`](ipam-vm/README.md). |
| `ee/` | `ansible-builder` definition for the execution environment: Red Hat `ee-minimal-rhel9:2.20` plus the collections in `collections/requirements.yml`. |

## Site values

No real addresses, hostnames or keys are committed. Defaults use the RFC 5737
documentation range (`192.0.2.0/24`) and `example.com`; real values are set as
inventory variables in AAP (or passed with `-e @site-vars.yml` on the CLI).
Defaults live only in role defaults (the `*_defaults` roles), the lowest precedence
there is, so inventory values always win. Not in play `vars:`, and not in a
playbook-adjacent `group_vars/all/`, both of which outrank inventory-level variables.

Secrets are never committed. In AAP they come from a custom credential type that
injects them as extra vars (see [`ipam-vm/README.md`](ipam-vm/README.md)); on the CLI,
from a git-ignored Ansible Vault file. Each `vault.yml.example` lists the expected keys.

## Execution environment

```bash
cd ee
ansible-builder build -t registry.example.com/homelab-ee:1.0 --container-runtime podman
```

## Guard rails

- `gitleaks` runs in CI and as a pre-commit hook, with an extra rule that
  rejects RFC 1918 addresses.
- `ansible-lint` (production profile) runs on every push.

```bash
pipx install pre-commit && pre-commit install
```

## License

MIT
