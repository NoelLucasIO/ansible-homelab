# ansible-homelab

[![ci](https://github.com/NoelLucasIO/ansible-homelab/actions/workflows/ci.yml/badge.svg)](https://github.com/NoelLucasIO/ansible-homelab/actions/workflows/ci.yml)

Ansible content for my home lab, run from **Ansible Automation Platform 2.7**
with a custom execution environment (ansible-core 2.20).

## What's here

| Path | What it does |
|---|---|
| `playbooks/bootstrap-host.yml` | Prepares a new host for AAP: automation user, validated sudoers, key, RHEL registration (`rhc`), SELinux mode, optional sysctls and updates, and optional libvirt-guests clean shutdown for KVM hosts, and trust in the lab root CA (`pki_trust`). RHEL, Fedora and Arch. |
| `playbooks/observability-agents.yml` | Ships the system journal to Loki with Grafana Alloy (Podman quadlet), and installs the Splunk Universal Forwarder as a deployment client. Never installs a forwarder beside Splunk Enterprise. |
| `playbooks/ping.yml` | Connectivity and facts smoke test. |
| `ipam-vm/` | Provisions KVM virtual machines with addresses allocated from Infoblox NIOS (WAPI), cloud-init, DNS records, a baseline role (users, chrony, rsyslog forwarding, node_exporter, firewalld) and Cockpit published through Nginx Proxy Manager. Includes decommissioning, and `scripts/nios-ipam-access.py`, which checks or creates the NIOS side (network, reserved range, API-only admin group, permissions, API user). See [`ipam-vm/README.md`](ipam-vm/README.md). |
| `hcp-vm/` | Deploys a single-node HCP (VSP One Object classic) VM on KVM from the HCP ISO, unattended: NIOS IPAM and zone delegation, an isolated back-end network, a serial-console driver for the OS installer and network menus, then `arc-deploy` with a rendered `cluster.cfg`. Post-install steps as stand-alone playbooks: first login (security account, SSH, MAPI), system-wide namespace defaults, disabling physical-node services a VM can't run, HCP's Prometheus, syslog to Splunk and Loki (with a relay for Alloy), an initial tenant, and a firewalled node_exporter registered with Prometheus. Namespaces (S3 buckets), data access for application service accounts, and HCP's certificate from the lab CA (`pki/`, renewed by a weekly AAP schedule) have playbooks of their own. AAP surveys carry every answer. See [`hcp-vm/README.md`](hcp-vm/README.md). |
| `vigil/` | A security-scanning pod as Podman Quadlets: Greenbone CE, DefectDojo, timer-driven Nuclei/ZAP scans and Semgrep/Checkov scans of git repos imported into DefectDojo, Falco runtime detection with alerts to Loki, with Prometheus alerts, a Grafana findings dashboard, NPM proxy hosts and NIOS records. See [`vigil/README.md`](vigil/README.md). |
| `pki/` | The lab certificate authority: step-ca (ACME, CRL, admin provisioner) and a landing page in a rootless Podman pod, state on the existing PostgreSQL. A YubiKey root ceremony (root in PIV on two keys, encrypted copy in AAP), an intermediate signed and renewed from AAP, Prometheus alerts, a Grafana dashboard, Loki and Splunk logs, NPM and NIOS records, and an end-to-end self-test. The `pki_trust` role puts the root in every host's trust store (bootstrap, VM baseline). See [`pki/README.md`](pki/README.md). |
| `opnsense-backup/` | Nightly AAP job: the OPNsense `config.xml` through the API (no SSH on the firewall), validated, encrypted with age from memory onto the storage host (the plaintext never touches a disk), skipped when unchanged, 90-day retention that always keeps the newest 10, and a Prometheus "stale backup" alert via node_exporter's textfile collector. Firewalls live in their own AAP inventory, `network`. See [`opnsense-backup/README.md`](opnsense-backup/README.md). |
| `crowdsec/` | A CrowdSec agent (Podman Quadlet) that reads Nginx Proxy Manager's access logs and reports to the OPNsense `os-crowdsec` LAPI, so the firewall blocks attackers for every site; plus the OPNsense steps. See [`crowdsec/README.md`](crowdsec/README.md). |
| `vtm-vm/` | Deploys an Ivanti vTM (Virtual Traffic Manager) virtual appliance on KVM from the vendor's KVM zip, unattended: address and DNS from NIOS, disk grown and `z-initial-config` answers injected offline, first boot configures it, admin UI published through Nginx Proxy Manager. Includes destroy and an AAP setup playbook for both job templates and their surveys. See [`vtm-vm/README.md`](vtm-vm/README.md). |
| `vtm-config/` | Configures an Ivanti vTM appliance over its REST API (SOAP for local users, which REST lacks): its own API user, NTP, event syslog to Loki with the matching Alloy listener, Analytics Export to Splunk HEC, and scheduled configuration backups with retention. Includes an AAP setup playbook. See [`vtm-config/README.md`](vtm-config/README.md). |
| `backup/` | Proxmox Backup Server 4.2 as a KVM VM installed unattended from the ISO and configured through its API; nightly encrypted file backups of the hosts and image backups of selected KVM domains (virtnbdbackup), a weekly restore test, an encrypted offsite copy on Backblaze B2 (S3 datastore + sync job), and Prometheus alerts, Loki logs and a Grafana dashboard. See [`backup/README.md`](backup/README.md). |
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
