# hearth

Publishes a local, OpenAI-compatible LLM server on the LAN at `https://llm.<domain>/v1`,
through Nginx Proxy Manager with the wildcard certificate and the LAN-only access list.
Written for [Readest](https://readest.com)'s X-Ray (a spoiler-free, AI-built reference of the
characters, places and terms in the book you're reading); anything that speaks the OpenAI API
can use it.

It doesn't run a model. It publishes one that already runs: llama.cpp's `llama-server` as a user
unit on the GPU host (here, the one that also serves a RAG app on loopback). That server keeps
its own unit and flags, and stays on `127.0.0.1`; a systemd socket on the LAN address relays to it.

```
iPad (Readest) ──https──► NPM  llm.<domain>  (LAN-only, wildcard cert, 600 s timeouts)
                                  │ http
                                  ▼
                 GPU host  <LAN IP>:8088   hearth.socket ─► hearth.service (systemd-socket-proxyd)
                                                                   │
                                                                   ▼
                                           127.0.0.1:8080   llama-server.service (API key required)
```

Why not Ollama: it is a wrapper around the same engine (llama.cpp's ggml), adding model
downloads by name and automatic loading/unloading. A second server would load a second copy of
an 8B model next to the one already in VRAM, and Ollama has no authentication; `llama-server`
requires its API key.

## What `playbooks/deploy.yml` does

0. **Preflight:** secrets present; lingering on for the owner; `hearth_upstream_unit` active and
   answering `/health`; the server's API key read from `hearth_api_key_file` (used only by the
   test, never written anywhere); `systemd-socket-proxyd` present; the LAN port free on a first deploy.
1. **Listener:** user units `hearth.socket` (`<LAN IP>:8088`, `FreeBind`) and `hearth.service`
   (`systemd-socket-proxyd 127.0.0.1:8080`), enabled; checks `/health` through the LAN address.
2. **NPM and DNS:** proxy host `llm.<domain>` → `http://<LAN IP>:8088` (wildcard certificate,
   LAN-only list, 600 s read/send timeouts, buffering off), updated if the target or timeouts
   drift; an A record in NIOS pointing at NPM.
3. **Verify:** `GET /v1/models` through `https://llm.<domain>` with the key, and refused without
   it; then a `response_format: json_object` chat completion, the same kind of request X-Ray
   sends, with thinking off; checks the answer parses and names the characters. Prints the
   Readest settings.

`playbooks/remove.yml` stops and removes the two units. The LLM server, the NPM host and the
DNS record are left alone.

## Variables

| Variable | Default | |
|---|---|---|
| `hearth_upstream` | `127.0.0.1:8080` | The server on loopback |
| `hearth_upstream_unit` | `llama-server.service` | Its user unit: checked, never changed |
| `hearth_api_key_file` | `~/.config/llama/apikey` | The server's key, for the test; blank sends none |
| `hearth_owner` | `baseline_admin_user` | Owner of the user units |
| `hearth_ip` / `hearth_port` | the host's default IPv4 / `8088` | Where the listener binds and NPM forwards |
| `hearth_fqdn` | `llm.<hearth_domain>` | `hearth_domain` defaults to `vm_domain` |

The NIOS and NPM variables (`nios_ip`, `npm_api`, `npm_ip`, `npm_cert_domain`,
`npm_access_list_name`) are the ones ipam-vm, vigil and pki use, so an AAP inventory that runs
those needs nothing new. Everything else: `playbooks/roles/hearth_defaults/defaults/main.yml`.

## Running it

**AAP:** `ansible-playbook playbooks/aap-setup.yml -e @../hcp-vm/aap.local.yml -e @../site-vars.yml`
creates the job templates **Hearth deploy** and **Hearth remove** (survey: the host), with the
machine credential and `homelab-secrets` (NIOS and NPM logins) attached. Privilege escalation is
on: it reads the key file and enables lingering; the units run as the owner.

**CLI:** copy `inventory.example.yml` to `inventory.yml` and
`playbooks/group_vars/all/vault.yml.example` to `vault.yml` (fill in, `ansible-vault encrypt`), then

```bash
ansible-playbook playbooks/deploy.yml -e @../site-vars.yml --ask-vault-pass -K -e hearth_host=gpu01
```

Tags: `listener`, `dns`, `verify`.

## Readest

Readest 0.12.26 or later; on a standard (store) install, Custom Translators needs Plus, Pro or
the Full Customization unlock.

1. **Settings → Language → Custom Translators → Add Translator**
   - **Type:** OpenAI Compatible
   - **Base URL:** `https://llm.<domain>/v1`
   - **API Key:** the server's key
   - **Model:** the refresh button next to it reads `/v1/models`
   - **Temperature:** 0
   - **Thinking:** Off. Qwen3 otherwise reasons first: in testing, the same request took
     6–10 s with thinking and under 2 s without, with the same answer.
   - **Test**, then **Save**
2. **Settings → Language → Manage Dictionaries:** turn on **X-Ray** and **Update While Reading**.
3. In a book: quill icon → **Notebook** → **X-Ray** → **Update X-Ray** builds it up to the
   current page; after that it updates as you read.

`llama-server` runs with `--parallel 1`: an X-Ray update and a request from anything else on
the same server take turns.

## Checking it

On the GPU host, as the owner:

```sh
systemctl --user status hearth.socket hearth.service llama-server.service
curl -s http://<LAN IP>:8088/health
```

From anywhere on the LAN:

```sh
curl -s -H "Authorization: Bearer $KEY" https://llm.example.com/v1/models | jq '.data[].id'
```
