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
| `vigil/` | A security-scanning pod as Podman Quadlets: Greenbone CE, DefectDojo, and timer-driven Nuclei/ZAP scans imported into DefectDojo, with Prometheus alerts, a Grafana findings dashboard, NPM proxy hosts and NIOS records. See [`vigil/README.md`](vigil/README.md). |
| `ee/` | `ansible-builder` definition for the execution environment: Red Hat `ee-minimal-rhel9:2.20` plus the collections in `collections/requirements.yml`. |

## Running it

**Requirements:** tested with ansible-core 2.20 (in the EE) and 2.22. `ipam-vm/` uses only built-in modules;
`playbooks/` and `vigil/` also need the collections in `ci/requirements.yml`
(ansible.posix, community.general, containers.podman). The `ee/` image has everything.

**In AAP:** one project pointing at this repo, an inventory whose variables hold your
site values (see below), a Machine credential for the automation user, and the
custom secrets credential described in [`ipam-vm/README.md`](ipam-vm/README.md).
Each playbook is a job template; ipam-vm's README lists the surveys.

**On the CLI:** copy the area's `inventory.example.yml` to `inventory.yml`, put
site values in a vars file, and pass it as extra vars:

```bash
cd ipam-vm
ansible-playbook -i inventory.yml playbooks/provision.yml \
  -e @site-vars.yml --ask-vault-pass -K -e vm_name=web01
```

Each area has an `ansible.cfg` that turns on SSH pipelining, which also avoids the
"junk after the JSON data" warning from hosts that print terminal escape codes at login.

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

- `gitleaks` scans the full git history in CI and runs as a pre-commit hook, with
  an extra rule that rejects RFC 1918 addresses.
- `ansible-lint` (production profile) runs on every push.

```bash
pipx install pre-commit && pre-commit install
```

## License

MIT
