# opnsense-backup: encrypted OPNsense configuration backups

A nightly AAP job that pulls the firewall's full `config.xml` through the OPNsense API
(`/api/core/backup/download/this`), checks it, and writes it **encrypted with
[age](https://age-encryption.org)** to a storage host. The plaintext is never written to
any disk: it goes from the API response, in memory, to `age` on the storage host's stdin.

| Piece | What it does |
|---|---|
| Inventory `network` | Network appliances, apart from the Linux hosts' `all`. The firewall is in group `opnsense` with `ansible_connection: local`: every call is an API call from the execution environment. No SSH, Python or root on the firewall. |
| Credential type *OPNsense API* | `opnsense_api_key` / `opnsense_api_secret` for an API user with only the privilege **Diagnostics: Configuration History**. |
| `playbooks/backup.yml` | Download → validate (XML, `<opnsense>` root, minimum size, revision time) → skip if unchanged (SHA-256 of the last one) → `age` encrypt to the recipients → `config-<UTC stamp>.xml.age` (0400, in a 0700 root-only directory) → retention → metrics. |
| Retention | Files older than `opnsense_backup_keep_days` (90) are removed, but the newest `opnsense_backup_min_keep` (10) are always kept: a config that hasn't changed for months still has its backups. |
| Monitoring | Metrics through node_exporter's textfile collector on the storage host (enabled with a drop-in that keeps the unit's other flags). Alerts `OPNsenseBackupStale` (no good backup for 36 h) and `OPNsenseBackupMissing`. |

## Keys: who can decrypt

Backups are encrypted to ordinary age recipients (`age1…`), so the storage host only needs
the packaged `age`. Keep the matching private key off the network: the recommended layout
puts it **encrypted to both YubiKeys** with
[age-plugin-yubikey](https://github.com/str4d/age-plugin-yubikey), plus one offline copy.

On the workstation (Arch/CachyOS: `pacman -S age age-plugin-yubikey`):

```bash
mkdir -p ~/age && cd ~/age
age-keygen -o opnsense-backup.key            # prints "Public key: age1..." (the recipient)
age-plugin-yubikey                           # once per YubiKey: new identity in a retired PIV slot
age-plugin-yubikey --list                    # both YubiKey recipients (age1yubikey1...)
age -e -r age1yubikey1...5Ci -r age1yubikey1...Nano -o opnsense-backup.key.age opnsense-backup.key
# print or copy opnsense-backup.key to offline media, then:
shred -u opnsense-backup.key
```

Restore (either YubiKey plugged in; PIN and touch):

```bash
age -d -i <(age -d -i <(age-plugin-yubikey -i) ~/age/opnsense-backup.key.age) \
    config-20261123T071700Z.xml.age > config.xml
# OPNsense: System > Configuration > Backups > Restore
```

## Setup

1. **OPNsense:** System › Access › Groups: a group with only *Diagnostics: Configuration
   History*; System › Access › Users: user `aap-backup` in it (no shell, random password),
   then *API keys* › **+** (downloads a file with `key=` and `secret=`).
2. **Site values** (`site-vars.yml`, and they become the `network` inventory's variables):
   ```yaml
   opnsense_firewall: gw.example.com
   opnsense_backup_store: fedora.example.com
   opnsense_backup_dir: /data/backups/opnsense
   opnsense_backup_prom_rules_dir: /srv/prometheus/config/rules.d
   opnsense_backup_prom_owner: youruser
   opnsense_backup_prom_url: http://127.0.0.1:9090
   opnsense_backup_age_recipients: [age1...]
   ```
3. **AAP:** `ansible-playbook playbooks/aap-setup.yml -e @../hcp-vm/aap.local.yml -e @../site-vars.yml`
   (prompts for the API key and secret). Creates the inventory, credential, job template
   *OPNsense backup* (machine credential for the storage host attached) and a nightly schedule.
4. Run *OPNsense backup* once by hand and check the result line and
   `opnsense_backup_last_success_timestamp_seconds` in Prometheus.

Change the recipients (e.g. a new key) by editing `opnsense_backup_age_recipients`; files
already written stay readable with the keys they were written for.
