# vtm-config

Configuration of an Ivanti vTM (Virtual Traffic Manager) appliance after it is built, for
example by [`vtm-vm`](../vtm-vm/README.md). Everything goes through the appliance's REST API
(8.6 on 22.9) from the controller, except local users, which REST 8.6 doesn't expose: those
use the SOAP control API, the same interface `zcli` talks to.

Steps: 1. platform settings (`platform.yml`, `backup.yml`); 2. the admin certificate
(`certificate.yml`) and the lab CA in the SSL catalogs (`ca.yml`); 3. an HCP cluster behind vTM
(`hcp.yml`, with the opt-in DNS step `hcp-dns.yml`).

## platform.yml

| Play | Runs on | What it does |
|---|---|---|
| Alloy listener | `vtm_alloy_host` | Writes `<config dir>/vtm.alloy`: a UDP syslog listener (`vtm_syslog_port`, default 1518) labelled `job="vtm"`, then restarts the rootless Alloy user service if the file changed. Checks the port isn't taken by something else first. |
| Platform settings | controller | Logs in as `vtm_api_user`. If that fails (first run, rebuilt appliance), logs in once as `vtm_admin_user` and creates the user in the `admin` group, or resets its password, over SOAP. Takes a full backup, then sets NTP on every traffic manager in the cluster, creates the **Syslog to Loki** action and adds it to the built-in **All Events** type (keeping any actions already there), and turns on Analytics Export to Splunk HEC when `vtm_hec_token` is set. |
| Admin TLS (in Platform settings) | controller, SSH to each traffic manager | Sets `global_settings` `admin.support_tls1`/`support_tls1_1` from `vtm_admin_tls1`/`vtm_admin_tls1_1` (default: off, leaving TLS 1.2 and 1.3), then tests on each traffic manager which versions the admin server and REST accept and restarts only the daemon that still serves the old set. |
| Check Loki | `vtm_alloy_host` | Asks Loki for `{job="vtm"}` lines in the last 24 hours; fails if there are none. |

Every REST write reads the resource first and only PUTs when the merge would change it, so a
repeat run changes nothing except the backup it takes.

## backup.yml

A full configuration backup on the appliance (`vtm-config-<UTC timestamp>`), then prunes ours
beyond `vtm_backup_keep` (default 14). Backups taken by hand or by anything else are never
touched. Backups stay on the appliance: they don't survive a destroy and rebuild by vtm-vm.

## certificate.yml

Replaces vTM's self-signed admin server certificate (`CN=zxtm`) with one from the lab CA
([`pki/`](../pki/README.md), step-ca), on every traffic manager in the cluster. One certificate
secures the admin UI and SOAP (:9090) and REST (:9070).

1. Asks each traffic manager over SOAP (`getAdminSSLCertificateInfo`) for its current
   certificate. It renews when that is self-signed, lacks a SAN, is within
   `vtm_cert_renew_days` (30) of expiry, or `vtm_cert_force` is set; otherwise nothing changes.
2. Issues the certificate inside the `pki-ca` container on `vtm_pki_host` with the admin
   provisioner: SANs are the traffic manager's name, its IPv4 address(es) from DNS
   (`vtm_cert_ip_san`) and `vtm_cert_extra_sans`; `vtm_cert_days` (90), `vtm_cert_key` (EC-P256).
   The public certificate is recorded under `vtm_pki_issued_dir`/vtm-<name>/ for the
   `PKIIssuedCertExpiring` alert.
3. Installs it with REST `status/local_tm/admin_ssl_certificate?import`, the same installer as
   *System > Security*, which restarts the admin server. The key goes from the container straight
   into that request: it is never written to disk on the controller or the CA host.
4. Proves the result: :9090 must verify against the lab root (chain and host name), and the
   certificate :9070 serves (checked with openssl from the CA host) must have the installed
   fingerprint. If it doesn't, it restarts only the REST daemon over SSH as `vtm_ssh_user`
   (`vtm_cert_restart_rest`), then checks again.

The weekly AAP schedule reruns it, so it renews itself a month before expiry.

## ca.yml

The lab CA's root and intermediate and the intermediate's CRL, from the `pki-ca` container on
`vtm_pki_host`, into the CA catalog (what pools verify backends against) and the admin CA catalog,
as `Lab-Root-CA.pem`, `Lab-Intermediate-CA.pem` and `Lab-Intermediate-CA.crl`. step-ca reissues
the CRL every day with a one-day lifetime, so AAP reruns this every 4 hours; it writes only the
entries that differ. `hcp.yml` runs the same tasks.

## hcp.yml

An HCP cluster behind vTM. Per service: a monitor, a pool and a virtual server named
`<prefix>_<service>` (`vtm_hcp_prefix`, default `HCP`):

| Service | Port | Monitor (per node, every 3 s, 3 failures) | Persistence |
|---|---|---|---|
| Data (S3, REST, namespace browser) | 443 | HTTPS `/node_status`, status `204` | none |
| MAPI | 9090 | TCP connect | none |
| Management (system and tenant consoles) | 8000 | HTTPS `/`, status `302` | `<prefix>_Persistence` |
| Prometheus (`vtm_hcp_prometheus`, on) | 9094 | HTTPS `/-/ready`, status `200` | `<prefix>_Persistence` |
| Search (`vtm_hcp_search`, off) | 8888 | HTTPS `/`, status `302`, back-off | `<prefix>_Persistence` |

- **Pools:** the nodes' front-end addresses (`vtm_hcp_nodes`), least connections, TLS to the
  nodes (1.2+). With `vtm_hcp_verify_backend` (on), HCP's certificate must chain to the lab CA and
  carry the cluster FQDN (strict verification). HCP's Prometheus runs on some nodes and fails over;
  its monitor marks the others down, so the pool follows it.
- **Persistence:** `<prefix>_Persistence`, transparent session affinity (a cookie), only on the
  console pools: S3 and API clients ignore cookies.
- **Certificate:** issued by the lab CA under the cluster FQDN in the SSL server certificate
  catalog, with `<fqdn>`, `*.<fqdn>`, the names HCP's own certificate has (so tenants are covered)
  and `vtm_hcp_cert_extra_sans`; renewed when due like the admin certificate, and recorded for
  `PKIIssuedCertExpiring`.
- **VIPs:** `vtm_hcp_vips`, or allocated from NIOS as host records `<system>-vip<N>.<domain>` in
  `vtm_hcp_vip_network` (reused on reruns). The traffic IP group, named after the cluster FQDN,
  holds them on every traffic manager (single-hosted). Each traffic manager gets a traffic IP
  network for that subnet on whichever of its interfaces is in it.
- **Virtual servers:** on the traffic IP group, SSL decryption with the cluster certificate,
  TLS 1.2+, X-Forwarded-For added, cluster IP header not.
- **Proof:** from the CA host, each service is requested through the VIP with vTM's certificate
  checked against the lab root, expecting what its monitor expects.

DNS is not touched. Until it changes, clients reach HCP directly as before.

## hcp-dns.yml (opt-in)

`cutover` replaces the NIOS delegation of `<system>.<domain>` to the HCP nodes with an
authoritative zone (same name servers as the parent zone) holding A records for `<fqdn>` and
`*.<fqdn>` pointing at the VIPs, so DNS rather than HCP answers. The delegation's name servers are
kept in the zone's comment; `revert` deletes the zone (only one this playbook made) and restores
the delegation. Either restarts NIOS DNS only if NIOS needs it. It never allocates VIPs: those
come from `hcp.yml`.

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
- **Admin TLS versions** (`global_settings` `admin.*`) cover the admin server, REST and internal
  connections. REST (`zeus.configd`) applies a change at once; the admin server only when it
  restarts (`$ZEUSHOME/admin/rc restart`, which leaves traffic alone). REST doesn't answer on
  127.0.0.1, so the probe connects by name.
- **SSL catalogs:** the CA and admin CA catalogs (`ssl/cas`, `ssl/admin_cas`) are files: REST
  `PUT` with `Content-Type: application/octet-stream` (certificates and CRLs, PEM). Server
  certificates (`ssl/server_keys`) take `public` (the chain is served as given) and `private`.
- **Pool `strict_verify`** checks the backend's chain against the CA catalog and its name against
  `common_name_match`; the node is addressed by IP, so the cluster FQDN goes there. With a wrong
  name vTM refuses the backend (clients get HTTP 500).
- **Traffic IP networks** are `trafficip` (interface -> networks) on each traffic manager; its
  interfaces and addresses are in `appliance.ip`.
- **The admin server port** (System > Security, `port` in the `security` file; 9090 out of the box)
  is not in REST 8.6. If you change it, set `vtm_admin_port` to match: SOAP and the certificate
  checks use it.
- **NTP** is `appliance.ntpservers` on each traffic manager (out of the box: `zeus.pool.ntp.org`).
- The **admin server certificate** is `$ZEUSHOME/admin/etc/admin.public` / `admin.private`,
  per traffic manager, outside the replicated configuration. REST 8.6 imports it
  (`status/local_tm/admin_ssl_certificate?import`, fields `public_cert` (the chain is served as
  given) and `private_key`: PEM, unencrypted, RSA/DSA/EC); SOAP has the same as
  `System.Management.importAdminSSLCertificate`. Either restarts the admin server (:9090), but
  REST itself is served by `zeus.configd`, which reads the file only at start and ignores HUP:
  until `$ZEUSHOME/zxtm/rc.configd restart` (or a reboot) :9070 keeps the old certificate, and
  its status then reports an empty fingerprint. Restarting configd leaves the traffic processes
  (`zeus.zxtm`) running. The UI upload has the same gap.

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
   added. You get the templates **vTM platform**, **vTM backup** and **vTM certificate**, a daily
   backup schedule for `vtm_target`, and, when `vtm_pki_host` is set (also pass
   `vtm_pki_issued_dir`), a weekly certificate renewal schedule (Sundays 03:37).
4. **Certificate:** the machine credential must reach `vtm_pki_host` (with sudo to the user
   running `pki-ca`) and `vtm_ssh_user`@ each traffic manager, for the REST restart. The
   appliance has no automation user, so put the machine credential's public key (the
   `automation_pubkey` that `bootstrap-host.yml` installs) in root's `authorized_keys` on each
   traffic manager. Without it, the certificate still lands on :9090, the job fails at the restart,
   and :9070 keeps the old one until a reboot or a later run.
5. **HCP:** pass `aap_nios_credential` (the credential with `nios_api_password`) and the cluster's
   `vtm_hcp_cluster`, `vtm_hcp_domain`, `vtm_hcp_nodes` and `vtm_hcp_vip_network` to
   `aap-setup.yml`; you get **vTM CA**, **vTM HCP** (weekly schedule) and **vTM HCP DNS**.

The admin password only matters until the API user exists. After that you can blank it in the
credential: runs log in as the API user and never touch `admin`.

## Status

- **hcp.yml and ca.yml, tested from the CLI** against vtm01 and hcp01 (one node), with a VIP
  given (NIOS allocation and hcp-dns.yml need the NIOS credential: AAP only so far): all objects
  created, the VIP raised, Data `/node_status` 204, MAPI answers, Management 302 (as
  `admin.<fqdn>`), Prometheus `/-/ready` 200, each through the VIP with vTM's certificate verified
  against the lab root. Strict backend verification proven by a wrong `common_name_match` (HTTP 500),
  which the next run corrected. Repeat runs change only the backup; ca.yml's second run nothing.

- **Tested end to end from AAP 2.7** against vtm01 (22.9r7): the first run created the `ansible`
  API user over SOAP, took a backup, and turned on Analytics Export (the exporter holds a connection
  to Splunk HEC on 8088 and raises no errors); NTP, the syslog action, All Events and the Alloy
  drop-in were already in place from CLI runs and were left unchanged. The Loki check passed
  (real events arrive as `{job="vtm", host="vtm01…", app="zeus.eventd"}`).
- **Also tested from the CLI:** creating the API user from scratch, backup retention, the Alloy
  drop-in and restart. A repeat run changes only the backup.
- `aap-setup.yml` created the credential type and both templates.
- **certificate.yml, tested from the CLI** against vtm01 with the lab CA on fedora: replaced the
  self-signed certificate; a forced renewal issued, recorded and installed a new one, restarted
  only the REST daemon (traffic process ids unchanged) and verified both ports against the lab
  root; a repeat run changes nothing; a run after a REST daemon left on the old certificate
  restarted just that.
- **certificate.yml, tested from AAP 2.7:** a forced run issued, recorded and installed a new
  certificate; the following run found :9070 still on the old one, restarted only the REST daemon
  over SSH and verified both ports (same serial on :9090 and :9070, traffic process ids unchanged).
  `aap-setup.yml` syncs the project, then creates the template and the weekly schedule.
