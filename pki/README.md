# pki: lab certificate authority

A private CA for the lab, built on [step-ca](https://github.com/smallstep/certificates)
(Smallstep, Apache-2.0), deployed as a **rootless Podman pod** of Quadlets.

| Piece | What it does |
|---|---|
| Root CA | ECDSA P-384, 20 years. Made once by `ceremony.yml` on the workstation, stored in PIV slot 9c of two YubiKeys, plus a passphrase-encrypted copy in an AAP credential. Never on the CA host. |
| Intermediate CA | ECDSA P-256, 5 years, `pathlen:0`. Key made on the CA host (encrypted at rest) and never leaves it; the CSR is signed on the controller with the root from AAP. Renewed by `deploy.yml` once less than a year is left. |
| `pki-ca` | step-ca: ACME (`http-01`, `dns-01`, `tls-alpn-01`), an `admin` JWK provisioner for the step CLI, CRL (regenerated on every revocation), Prometheus metrics, JSON logs. State in the existing PostgreSQL. |
| `pki-web` | nginx: landing page (root/intermediate downloads, Apple profile, how-to), the CRL and AIA URLs over plain HTTP, expiry gauges for Prometheus. |
| `pki_trust` role | Puts the root in the OS trust store (Fedora/RHEL/CentOS, Debian/Ubuntu, Arch, SUSE). Runs from `playbooks/bootstrap-host.yml`, the ipam-vm baseline, and `trust.yml`. |

Every leaf certificate carries `http://ca.<domain>/crl` (CRL distribution point) and
`http://ca.<domain>/intermediate_ca.der` (AIA), so relying parties can check revocation.

## Names and ports

| Name | Goes to | Notes |
|---|---|---|
| `https://ca.<domain>:9443` | step-ca directly | ACME directory `/acme/acme/directory`, API, `/health`. Its own certificate, from the intermediate. |
| `http://ca.<domain>/crl` | NPM → nginx → step-ca | CRL (DER). LAN-only access list. |
| `https://pki.<domain>` | NPM (wildcard cert) → nginx | Landing page. LAN-only access list. |
| `127.0.0.1:9444` | step-ca | `/metrics` for Prometheus |
| `127.0.0.1:8446` | nginx | Behind NPM; `/metrics` (expiry gauges) only for local scrapes |

`ca.<domain>` must point at the Podman host, so NPM must run there too (`deploy.yml` checks).

## Observability

* **Prometheus:** `scrape.d/pki.yml` (jobs `pki-stepca`, `pki-expiry`) and `rules.d/pki.yml`:
  CA down, intermediate/root/serving certificate expiry, CRL unavailable or stale,
  signing and renewal failures, expiry check stale.
* **Grafana:** dashboard *PKI (step-ca)* in the *Security* folder (Prometheus and Loki
  picked from datasource dropdowns).
* **Loki:** nothing to configure. The pod logs to the owner's user journal, which Alloy
  already ships (`{container="pki-ca"}`).
* **Splunk:** a local `pki_stepca_inputs` app on the host's Universal Forwarder
  (journald input, `index=linux sourcetype=stepca:journald` by default). Outside any
  deployment-server class, so the deployment server leaves it alone.

## First run

1. **Ceremony** (workstation, both YubiKeys at hand; not AAP). Each key's PIV management key
   must be PIN-protected (`ykman piv access change-management-key --generate --protect`).
   ```bash
   cd pki
   ansible-playbook playbooks/ceremony.yml -e @../site-vars.yml
   ```
   It writes `~/pki-ceremony/root_ca.crt` and `root_ca.key.enc.pem`, imports the key into
   slot 9c of the primary, asks you to swap to the backup, imports again, and checks both by
   reading the public key back. The plaintext key only ever exists in `/dev/shm` and is shredded.
2. **Inventory:** set `pki_root_ca_pem` (the text of `root_ca.crt`) for **all** hosts, so
   `pki_trust` can use it everywhere, plus the `pki_*` site values (`inventory.example.yml`).
3. **AAP credentials:** put the three passwords and the root key + passphrase in the vault
   (`vault.yml.example`) and run `playbooks/aap-setup.yml`, or create them in the UI:
   * *PKI secrets* (`homelab-pki`): `pki_intermediate_password`, `pki_admin_password`, `pki_db_password`
   * *PKI root CA* (`homelab-pki-root`): `pki_root_key_pem` (multi-line), `pki_root_key_passphrase`

   Then move `root_ca.key.enc.pem` to offline media, or shred it.
4. **Deploy:** job template *PKI deploy* (or `ansible-playbook playbooks/deploy.yml -e @../site-vars.yml --ask-vault-pass -K`).
   It ends by issuing a test certificate, checking its chain and CRL pointer, revoking it,
   and finding it on the CRL.
5. **Trust:** job template *PKI trust* with `pki_trust_hosts=all` for existing hosts. New hosts
   get it from bootstrap and the VM baseline. Phones, Macs, Windows and browsers with their own
   store: see the landing page.

## Day 2

| Task | How |
|---|---|
| Certificate for a host | ACME client against `https://ca.<domain>:9443/acme/acme/directory` (certbot, acme.sh, Caddy, Traefik, NPM's custom ACME is not supported), or `step ca certificate` with the admin provisioner. |
| Revoke | `step ca revoke <serial>` (admin provisioner). The CRL is regenerated at once. |
| Intermediate renewal | Automatic on any deploy within `pki_intermediate_renew_days` (365) of expiry; alerts start before that. Needs the *PKI root CA* credential. Force with `pki_reissue_intermediate=true`. |
| Upgrade step-ca | Change `pki_stepca_image`, rerun deploy. |
| Remove | *PKI remove* stops the pod and removes its units; keys, certificates, database, DNS and NPM hosts stay. |

Root key compromise or a new root means a new ceremony and re-running *PKI trust* everywhere;
`ceremony.yml` refuses to overwrite an existing root without `-e pki_ceremony_force=true`.
