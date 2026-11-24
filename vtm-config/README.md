# vtm-config

Configuration of an Ivanti vTM (Virtual Traffic Manager) appliance after it is built, for
example by [`vtm-vm`](../vtm-vm/README.md). Everything goes through the appliance's REST API
(8.6 on 22.9) from the controller, except local users, which REST 8.6 doesn't expose: those
use the SOAP control API, the same interface `zcli` talks to.

This is step 1 of 3: platform settings. Certificates and services come next.

## platform.yml

| Play | Runs on | What it does |
|---|---|---|
| Alloy listener | `vtm_alloy_host` | Writes `<config dir>/vtm.alloy`: a UDP syslog listener (`vtm_syslog_port`, default 1518) labelled `job="vtm"`, then restarts the rootless Alloy user service if the file changed. Checks the port isn't taken by something else first. |
| Platform settings | controller | Logs in as `vtm_api_user`. If that fails (first run, rebuilt appliance), logs in once as `vtm_admin_user` and creates the user in the `admin` group, or resets its password, over SOAP. Takes a full backup, then sets NTP on every traffic manager in the cluster, creates the **Syslog to Loki** action and adds it to the built-in **All Events** type (keeping any actions already there), and turns on Analytics Export to Splunk HEC when `vtm_hec_token` is set. |
| Check Loki | `vtm_alloy_host` | Asks Loki for `{job="vtm"}` lines in the last 24 hours; fails if there are none. |

Every REST write reads the resource first and only PUTs when the merge would change it, so a
repeat run changes nothing except the backup it takes.

## backup.yml

A full configuration backup on the appliance (`vtm-config-<UTC timestamp>`), then prunes ours
beyond `vtm_backup_keep` (default 14). Backups taken by hand or by anything else are never
touched. Backups stay on the appliance: they don't survive a destroy and rebuild by vtm-vm.

## What the appliance does, as found on 22.9r7

- **Syslog** is an *action* attached to an *event type*, sent over UDP only, as RFC 5424:
  `<pri>1 <time> <host> zeus.eventd <pid> - - LEVEL<TAB>object<TAB>event tag<TAB>text`.
  Alloy's `loki.source.syslog` parses it without a relay.
- The built-in **All Events** matches everything with a wildcard (`type!*!event_tags *`) that
  REST doesn't show: a copy made over REST matches nothing, so the action is added to it rather
  than to a copy. Its wildcard covers operational events (faults, pools, monitors, licence,
  failover) but not configuration changes (`confadd`/`confmod`); those reach Splunk through
  Analytics Export's *Audit Log*.
- **Analytics Export** (`log_export`) ships the event, audit, admin-access, OS syslog and other
  logs to a Splunk HTTP Event Collector, with source and sourcetype already set per file.
  The index is the token's default.
- **Local users** are not a REST 8.6 resource; SOAP `Users.addUser` / `changePassword` /
  `listUsers` on `https://<appliance>:9090/soap` handle them.
- **NTP** is `appliance.ntpservers` on each traffic manager (out of the box: `zeus.pool.ntp.org`).

## Setup

1. **Alloy:** the drop-in references `loki.relabel.syslog` and `loki.write.local` from your
   existing Alloy config (`vtm_alloy_relabel_rules`, `vtm_alloy_forward_to`); set those to your
   component names. Alloy must load every `*.alloy` in `vtm_alloy_config_dir`.
2. **Splunk (optional):** create an HEC token whose default index is where vTM logs should go,
   and set `vtm_hec_endpoint`.
3. **AAP:**
   ```bash
   ansible-playbook vtm-config/playbooks/aap-setup.yml -e @site-vars.yml \
     -e aap_url=https://aap.example.com -e aap_user=admin -e aap_password=... \
     -e '{"aap_machine_credential": "homelab-ssh"}'
   ```
   That creates the credential type **vTM API**. Create a credential of that type (API password,
   admin password, optional HEC token), then rerun with `"aap_secrets_credential": "<its name>"`
   added. You get the templates **vTM platform** and **vTM backup**, and a daily backup schedule
   for `vtm_target`.

The admin password only matters until the API user exists. After that you can blank it in the
credential: runs log in as the API user and never touch `admin`.

## Status

- **Tested against vtm01 (22.9r7) from the CLI:** creating the API user over SOAP, backup and
  retention, NTP, the syslog action and All Events, the Alloy drop-in and restart, and the
  Loki check (a real `flippernobackends` warning arrived as `{job="vtm",host="vtm01…",app="zeus.eventd"}`).
  A repeat run changes only the backup.
- **Not yet run:** Analytics Export with a real HEC token (endpoint path to confirm), and the
  playbooks from AAP (`aap-setup.yml` is untested).
