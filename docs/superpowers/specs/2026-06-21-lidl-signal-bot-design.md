# Lidl Signal Bot — Design Spec

**Date:** 2026-06-21  
**Status:** Approved

## Overview

A self-hosted system on Google Compute Engine that fetches Lidl receipt data and sends weekly shopping suggestions to a Signal group. The existing `scripts/lidl_receipts.py` script is wrapped in a thin HTTP service and orchestrated by n8n. Deployment is fully automated via GitHub Actions.

---

## 1. Infrastructure

### GCE VM
- **Machine type:** `e2-micro` (1 vCPU, 1 GB RAM)
- **Zone:** `us-west1-a`
- **OS:** Debian 12 (latest)
- **Static external IP:** one `google_compute_address` resource

### Persistent Disk
Controlled by a Terraform variable `existing_data_disk_name`:
- If set: `data "google_compute_disk"` lookup — attaches the named existing disk
- If empty: creates a new `google_compute_disk` (10 GB, `pd-standard`)

Disk is mounted at `/data` on the VM. All service data lives here.

### Swap
The startup script adds a 1 GB swap file (`/swapfile`) to give Playwright headroom on the 1 GB RAM machine.

### Networking
- Firewall allows port 22 (SSH) only — n8n is never exposed publicly
- Access to n8n UI is via SSH tunnel: `ssh -L 5678:localhost:5678 user@VM_IP`

### Terraform State
Stored in a GCS bucket. Bucket name is a Terraform variable `tf_state_bucket`.

### GitHub Actions Auth to GCP
Workload Identity Federation (OIDC) — no service account key files stored as secrets.

### Terraform Variables

| Variable | Description | Default |
|---|---|---|
| `project_id` | GCP project ID | required |
| `zone` | GCE zone | `us-west1-a` |
| `existing_data_disk_name` | Name of existing persistent disk to attach; empty = create new | `""` |
| `tf_state_bucket` | GCS bucket for Terraform state | required |
| `ssh_public_key` | Public key added to VM for GitHub Actions + manual access | required |

---

## 2. Services (Docker Compose)

Three containers on an internal bridge network (`lidl-net`). No ports are exposed to the host except n8n's `5678` bound to `127.0.0.1`.

### Containers

| Service | Image | Internal port |
|---|---|---|
| `n8n` | `n8nio/n8n:latest` | `127.0.0.1:5678` |
| `signal-cli-rest-api` | `bbernhard/signal-cli-rest-api:latest` | `8080` (internal only) |
| `lidl-api` | `./docker/lidl-api` (custom) | `8000` (internal only) |

### Volume mounts (all on `/data`)

| Container | Host path | Container path |
|---|---|---|
| n8n | `/data/n8n` | `/home/node/.n8n` |
| signal-cli-rest-api | `/data/signal-cli` | `/home/.local/share/signal-cli` |
| lidl-api | `/data/receipts` | `/data` |
| lidl-api | `./scripts` | `/scripts` (read-only) |

### Environment variables (injected at deploy time from GitHub Secrets)

| Variable | Used by | Purpose |
|---|---|---|
| `LIDL_EMAIL` | lidl-api | Playwright re-auth |
| `LIDL_PASSWORD` | lidl-api | Playwright re-auth |
| `LIDL_COUNTRY` | lidl-api | API country code (default: `GB`) |
| `N8N_ENCRYPTION_KEY` | n8n | Encrypts stored credentials |
| `SIGNAL_PHONE_NUMBER` | signal-cli-rest-api | The linked Signal account number |

---

## 3. lidl-api

A thin FastAPI application at `docker/lidl-api/app.py`. Calls `lidl_receipts.py` as a subprocess, returns its JSON stdout. Long-running operations (update, reauth) run in a background thread and return a job ID; callers poll for completion.

### Dockerfile
Base: `python:3.11-slim`. Installs `fastapi`, `uvicorn`, `playwright`, and Chromium. Copies nothing from `scripts/` — the scripts directory is volume-mounted at runtime so changes don't require a rebuild.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"ok": true}` |
| `GET` | `/status` | Auth valid?, last sync time, receipt count |
| `POST` | `/update` | Kicks off `lidl_receipts.py update` in background; returns `{"job_id": "..."}` |
| `GET` | `/query` | Runs `lidl_receipts.py query`; params: `days`, `start`, `end`, `include_articles` |
| `GET` | `/top` | Returns top items by purchase frequency; params: `days` (default 30) |
| `POST` | `/reauth` | Runs Playwright headless login; returns job ID |
| `GET` | `/jobs/{id}` | Poll job status: `{"status": "running"|"done"|"error", "output": ...}` |

### `/top` — item grouping

On first call, `/top` reads all parsed receipts and builds `/data/item_groups.json` — a keyword-to-group-name map auto-seeded by extracting the first significant word from each unique item name (lowercased, accents stripped, common words removed: French articles `le`, `la`, `les`, `de`, `du`, `des`, `et`, `au`, `aux`, plus product modifiers `bio`, `vrac`, `fairtrade`, `xl`, `kg`, `g`).

Example auto-seeded entry: `"banane": "Banane"`.

Items are matched by scanning their normalised name for each keyword in the map (longest match wins). Unmatched items use their first significant word as the group name and are added to the map.

The map file is user-editable. Changes take effect on the next `/top` call without restarting the container.

---

## 4. n8n Workflows

Stored as JSON files in `docker/n8n/workflows/`. A `setup-workflows.sh` script imports and activates them via the n8n REST API on first deploy.

### Workflow 1: Receipt Sync (cron)
- **Trigger:** Every day at 06:00 (cron)
- **Steps:** `POST http://lidl-api:8000/update` → poll `/jobs/{id}` until done → log result
- **On error:** no Signal alert (silent background job)

### Workflow 2: Weekly Shopping List (cron)
- **Trigger:** Every Saturday at 09:00 (cron)
- **Steps:** `GET http://lidl-api:8000/top?days=30` → format message → send to Signal group via signal-cli-rest-api

**Message format:**
```
🛒 Weekly shopping suggestions (last 30 days):

1. Banane (7×)
2. Café (5×)
3. Poulet (4×)
4. Yaourt (4×)
5. Pain (3×)
```

### Workflow 3: On-demand Shopping List (Signal command)
- **Trigger:** Webhook — signal-cli-rest-api POSTs incoming group messages to `http://n8n:5678/webhook/signal`
- **Filter:** Message text starts with `/shopping` (case-insensitive); message is from the configured Signal group ID
- **Parsing:** Extract `--days N` if present; default to 30
- **Steps:** `GET /top?days=N` → format same as weekly list → reply to Signal group

### Workflow 4: Auth Failure Alert
- **Trigger:** Error workflow — Workflows 1, 2, and 3 each have this workflow set as their n8n Error Workflow. It fires when a lidl-api call returns a non-2xx response with body containing `"unauthorized"` or `"expired"`.
- **Steps:** Send Signal message to group: `"⚠️ Lidl auth has expired. SSH into the VM and run: make reauth"`
- No interactive loop — re-auth is handled manually via SSH.

### Signal group configuration
`SIGNAL_GROUP_ID` is a Docker Compose environment variable passed to n8n, where workflows read it via the `$SIGNAL_GROUP_ID` expression. signal-cli-rest-api is configured via its `MODE=normal` and `SIGNAL_WEBHOOK_URL=http://n8n:5678/webhook/signal` environment variables in docker-compose.yml.

---

## 5. GitHub Actions Pipelines

### `deploy-infra.yml`
- **Triggers:** push to `main` with changes under `infra/**`
- **Steps:** Checkout → authenticate to GCP via OIDC → `terraform init` (GCS backend) → `terraform plan` → `terraform apply -auto-approve`

### `deploy-app.yml`
- **Triggers:** push to `main` with changes under `docker/**` or `scripts/**`
- **Steps:** Checkout → SSH into VM (using `SSH_PRIVATE_KEY` secret) → `git pull` → `docker compose pull` → `docker compose up -d --build`

---

## 6. Project Structure

```
lidl/
├── .github/
│   └── workflows/
│       ├── deploy-infra.yml
│       └── deploy-app.yml
├── infra/
│   ├── main.tf          # VM, disk, firewall, static IP
│   ├── variables.tf
│   ├── outputs.tf       # VM IP address
│   └── startup.sh       # Docker install + disk mount + swap
├── docker/
│   ├── docker-compose.yml
│   ├── lidl-api/
│   │   ├── Dockerfile
│   │   └── app.py
│   └── n8n/
│       ├── workflows/
│       │   ├── receipt-sync.json
│       │   ├── weekly-shopping-list.json
│       │   ├── ondemand-shopping-list.json
│       │   └── auth-failure-alert.json
│       └── setup-workflows.sh
├── scripts/
│   └── lidl_receipts.py  (existing — unchanged)
├── Makefile              (existing + new `reauth` target)
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-06-21-lidl-signal-bot-design.md
```

---

## 7. One-time Manual Setup (ordered)

1. **GCP:** Create project, enable Compute Engine API + Secret Manager API, create GCS bucket for Terraform state
2. **OIDC:** Configure Workload Identity Federation for GitHub Actions
3. **GitHub Secrets:** Add `GCP_PROJECT_ID`, `TF_STATE_BUCKET`, `SSH_PRIVATE_KEY`, `SSH_PUBLIC_KEY`, `LIDL_EMAIL`, `LIDL_PASSWORD`, `N8N_ENCRYPTION_KEY`, `SIGNAL_PHONE_NUMBER`, `SIGNAL_GROUP_ID`
4. **Deploy infra:** Push a change to `infra/` or manually trigger `deploy-infra.yml` — Terraform creates the VM
5. **Deploy app:** Push a change to `docker/` or manually trigger `deploy-app.yml`
6. **Link Signal:** SSH into VM → `docker exec signal-cli-rest-api signal-cli link -n "LidlBot"` → scan QR code in Signal app (Settings → Linked Devices)
7. **Join group:** Add the bot's number to your Signal group from your phone
8. **Get group ID:** SSH → `docker exec signal-cli-rest-api signal-cli listGroups` → copy the group ID → update `SIGNAL_GROUP_ID` secret → redeploy
9. **Seed receipts:** From VM: `docker exec lidl-api curl -X POST localhost:8000/update` to run the first full sync
10. **Test:** Send `/shopping` to the Signal group — bot should reply

---

## 8. Makefile targets (additions)

```makefile
reauth:
    # Headless login using LIDL_EMAIL + LIDL_PASSWORD env vars.
    # If Lidl shows a CAPTCHA, run this locally and scp the auth state:
    #   scp data/lidl_auth_state.json $(VM_USER)@$(VM_IP):/data/receipts/lidl_auth_state.json
    ssh $(VM_USER)@$(VM_IP) \
        "cd /opt/lidl && docker compose exec lidl-api \
        python3 /scripts/lidl_receipts.py auth-check --login"

ssh-tunnel:
    ssh -L 5678:localhost:5678 $(VM_USER)@$(VM_IP)
```
