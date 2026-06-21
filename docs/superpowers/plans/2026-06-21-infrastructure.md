# Lidl Signal Bot — Plan 1: Infrastructure

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision a GCE e2-micro VM in us-west1-a with Docker, 1 GB swap, and an attached persistent disk at `/data`, deployed via GitHub Actions using Workload Identity Federation (no service account key files).

**Architecture:** Terraform manages all GCP resources. A bash startup script (runs on every VM boot, idempotent) installs Docker and mounts the disk. GitHub Actions authenticates via OIDC. The `existing_data_disk_name` variable controls whether an existing disk is attached or a new 10 GB disk is created.

**Tech Stack:** Terraform >= 1.5, hashicorp/google ~5.0, GitHub Actions (`google-github-actions/auth@v2`, `hashicorp/setup-terraform@v3`), Debian 12

---

### Task 0: One-time GCP prerequisites (manual — run once before anything else)

**Files:** none (manual shell commands)

These steps authenticate you locally to GCP and create the resources Terraform needs before it can run. Substitute `PROJECT_ID`, `PROJECT_NUMBER`, and `GITHUB_OWNER/REPO` throughout.

- [ ] **Step 1: Authenticate locally and set project**

```bash
gcloud auth login
gcloud config set project PROJECT_ID
```

- [ ] **Step 2: Enable required APIs**

```bash
gcloud services enable \
  compute.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  storage.googleapis.com
```

- [ ] **Step 3: Create GCS bucket for Terraform state**

```bash
gcloud storage buckets create gs://YOUR_TF_STATE_BUCKET \
  --location=us-west1 \
  --uniform-bucket-level-access
```

- [ ] **Step 4: Create service account for Terraform**

```bash
gcloud iam service-accounts create terraform-lidl \
  --display-name="Terraform Lidl Bot"

SA_EMAIL="terraform-lidl@PROJECT_ID.iam.gserviceaccount.com"

# Permissions needed by Terraform
for role in \
  roles/compute.instanceAdmin.v1 \
  roles/compute.networkAdmin \
  roles/compute.storageAdmin \
  roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$role"
done

# Access to the Terraform state bucket
gcloud storage buckets add-iam-policy-binding gs://YOUR_TF_STATE_BUCKET \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.objectAdmin"
```

- [ ] **Step 5: Create Workload Identity Pool and Provider**

```bash
PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')

gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions Pool"

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='GITHUB_OWNER/REPO'"
```

- [ ] **Step 6: Bind the service account to the pool**

```bash
POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool"

gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${POOL_NAME}/attribute.repository/GITHUB_OWNER/REPO"
```

- [ ] **Step 7: Note the provider resource name — you'll need it as a GitHub Secret**

```bash
gcloud iam workload-identity-pools providers describe github-provider \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --format="value(name)"
# Output looks like: projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

- [ ] **Step 8: Generate SSH key pair for VM access**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/lidl_bot -C "github-actions-lidl-bot" -N ""
cat ~/.ssh/lidl_bot.pub   # → SSH_PUBLIC_KEY secret
cat ~/.ssh/lidl_bot       # → SSH_PRIVATE_KEY secret (also used in Plan 3)
```

- [ ] **Step 9: Add GitHub Secrets**

In your GitHub repo → Settings → Secrets and variables → Actions, add:

| Secret name | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | output from Step 7 |
| `GCP_SERVICE_ACCOUNT` | `terraform-lidl@PROJECT_ID.iam.gserviceaccount.com` |
| `GCP_PROJECT_ID` | your GCP project ID |
| `TF_STATE_BUCKET` | bucket name from Step 3 |
| `SSH_PUBLIC_KEY` | content of `~/.ssh/lidl_bot.pub` |
| `SSH_PRIVATE_KEY` | content of `~/.ssh/lidl_bot` |
| `EXISTING_DATA_DISK_NAME` | name of your existing disk, or empty string |

---

### Task 1: Terraform project skeleton and .gitignore

**Files:**
- Create: `infra/main.tf`
- Create: `infra/terraform.tfvars.example`
- Modify: `.gitignore`

- [ ] **Step 1: Create `infra/main.tf` with provider and backend only**

```hcl
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    # Bucket is passed via -backend-config in CI and local init.
    # See deploy-infra.yml and the local init instructions below.
    prefix = "terraform/lidl"
  }
}

provider "google" {
  project = var.project_id
  zone    = var.zone
}
```

- [ ] **Step 2: Create `infra/terraform.tfvars.example`**

```hcl
project_id              = "your-gcp-project-id"
zone                    = "us-west1-a"
existing_data_disk_name = ""  # Leave empty to create a new 10 GB disk
                              # Set to disk name (e.g. "my-data-disk") to attach existing
```

- [ ] **Step 3: Add Terraform artifacts to `.gitignore`**

Read the current `.gitignore` (create it if absent), then append:

```
# Terraform
infra/.terraform/
infra/.terraform.lock.hcl
infra/terraform.tfvars
infra/tfplan
infra/*.tfstate
infra/*.tfstate.backup

# Data directory (receipt JSON files — not committed)
data/receipts/
data/receipts_summaries.json
data/receipts_detail.json
data/lidl_auth_state.json

# Brainstorm artefacts
.superpowers/
```

- [ ] **Step 4: Validate syntax**

```bash
cd infra && terraform init -backend=false
```

Expected: `Terraform has been successfully initialized!`

- [ ] **Step 5: Commit**

```bash
git add infra/main.tf infra/terraform.tfvars.example .gitignore
git commit -m "feat(infra): terraform skeleton with GCS backend"
```

---

### Task 2: Terraform variables

**Files:**
- Create: `infra/variables.tf`

- [ ] **Step 1: Create `infra/variables.tf`**

```hcl
variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "zone" {
  description = "GCE zone"
  type        = string
  default     = "us-west1-a"
}

variable "existing_data_disk_name" {
  description = "Name of an existing persistent disk to attach. Empty string creates a new 10 GB pd-standard disk."
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "SSH public key content added to the VM's authorized_keys (debian user)"
  type        = string
}
```

- [ ] **Step 2: Validate**

```bash
cd infra && terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/variables.tf
git commit -m "feat(infra): terraform variables"
```

---

### Task 3: Startup script

**Files:**
- Create: `infra/startup.sh`

This script runs as root on every VM boot. Every block is idempotent — safe to run multiple times.

- [ ] **Step 1: Create `infra/startup.sh`**

```bash
#!/bin/bash
set -euo pipefail

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  apt-get update -q
  apt-get install -y -q ca-certificates curl gnupg lsb-release git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
  usermod -aG docker debian
fi

# ── Persistent disk ───────────────────────────────────────────────────────────
DATA_DEV="/dev/disk/by-id/google-data"
DATA_MOUNT="/data"

if [ -b "$DATA_DEV" ]; then
  # Format only if the disk has no filesystem yet
  if ! blkid "$DATA_DEV" | grep -q "TYPE="; then
    mkfs.ext4 -F "$DATA_DEV"
  fi
  mkdir -p "$DATA_MOUNT"
  if ! mountpoint -q "$DATA_MOUNT"; then
    mount "$DATA_DEV" "$DATA_MOUNT"
  fi
  # Add fstab entry only once
  if ! grep -q "$DATA_DEV" /etc/fstab; then
    echo "$DATA_DEV $DATA_MOUNT ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
fi

# ── Swap ──────────────────────────────────────────────────────────────────────
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ── Data directories ──────────────────────────────────────────────────────────
mkdir -p /data/n8n /data/signal-cli /data/receipts
chmod -R 777 /data
```

- [ ] **Step 2: Make it executable and commit**

```bash
chmod +x infra/startup.sh
git add infra/startup.sh
git commit -m "feat(infra): vm startup script — docker + disk mount + swap"
```

---

### Task 4: Infrastructure resources

**Files:**
- Modify: `infra/main.tf`

Append all resource blocks to `infra/main.tf`.

- [ ] **Step 1: Add `locals` block to derive region from zone**

Append to `infra/main.tf`:

```hcl
locals {
  region = join("-", slice(split("-", var.zone), 0, 2))
}
```

- [ ] **Step 2: Add static IP and firewall**

Append to `infra/main.tf`:

```hcl
resource "google_compute_address" "vm_ip" {
  name   = "lidl-bot-ip"
  region = local.region
}

resource "google_compute_firewall" "ssh" {
  name    = "lidl-bot-allow-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["lidl-bot"]
}
```

- [ ] **Step 3: Add conditional disk logic**

Append to `infra/main.tf`:

```hcl
resource "google_compute_disk" "data" {
  count = var.existing_data_disk_name == "" ? 1 : 0
  name  = "lidl-bot-data"
  size  = 10
  type  = "pd-standard"
  zone  = var.zone
}

data "google_compute_disk" "existing" {
  count = var.existing_data_disk_name != "" ? 1 : 0
  name  = var.existing_data_disk_name
  zone  = var.zone
}

locals {
  data_disk_self_link = (
    var.existing_data_disk_name != ""
    ? data.google_compute_disk.existing[0].self_link
    : google_compute_disk.data[0].self_link
  )
}
```

- [ ] **Step 4: Add the VM**

Append to `infra/main.tf`:

```hcl
resource "google_compute_instance" "vm" {
  name         = "lidl-bot"
  machine_type = "e2-micro"
  zone         = var.zone
  tags         = ["lidl-bot"]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
    }
  }

  attached_disk {
    source      = local.data_disk_self_link
    device_name = "data"
    mode        = "READ_WRITE"
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.vm_ip.address
    }
  }

  metadata = {
    ssh-keys       = "debian:${var.ssh_public_key}"
    startup-script = file("${path.module}/startup.sh")
  }

  # Prevent Terraform from re-running startup-script on every plan
  lifecycle {
    ignore_changes = [metadata["startup-script"]]
  }
}
```

- [ ] **Step 5: Validate**

```bash
cd infra && terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
git add infra/main.tf
git commit -m "feat(infra): gce vm, static ip, firewall, conditional disk"
```

---

### Task 5: Terraform outputs

**Files:**
- Create: `infra/outputs.tf`

- [ ] **Step 1: Create `infra/outputs.tf`**

```hcl
output "vm_ip" {
  description = "External IP address of the VM"
  value       = google_compute_address.vm_ip.address
}

output "vm_name" {
  description = "Name of the VM instance"
  value       = google_compute_instance.vm.name
}

output "ssh_command" {
  description = "SSH command to connect"
  value       = "ssh -i ~/.ssh/lidl_bot debian@${google_compute_address.vm_ip.address}"
}
```

- [ ] **Step 2: Validate**

```bash
cd infra && terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add infra/outputs.tf
git commit -m "feat(infra): terraform outputs — vm ip and ssh command"
```

---

### Task 6: GitHub Actions deploy-infra workflow

**Files:**
- Create: `.github/workflows/deploy-infra.yml`

- [ ] **Step 1: Create `.github/workflows/deploy-infra.yml`**

```yaml
name: Deploy Infrastructure

on:
  push:
    branches: [main]
    paths:
      - 'infra/**'
  workflow_dispatch:

permissions:
  contents: read
  id-token: write  # Required for OIDC token

jobs:
  terraform:
    name: Terraform Apply
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Authenticate to GCP via OIDC
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
          service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~1.7"

      - name: Terraform Init
        working-directory: infra
        run: |
          terraform init \
            -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}" \
            -backend-config="prefix=terraform/lidl"

      - name: Terraform Validate
        working-directory: infra
        run: terraform validate

      - name: Terraform Plan
        working-directory: infra
        env:
          TF_VAR_project_id: ${{ secrets.GCP_PROJECT_ID }}
          TF_VAR_ssh_public_key: ${{ secrets.SSH_PUBLIC_KEY }}
          TF_VAR_existing_data_disk_name: ${{ secrets.EXISTING_DATA_DISK_NAME }}
        run: terraform plan -out=tfplan

      - name: Terraform Apply
        working-directory: infra
        env:
          TF_VAR_project_id: ${{ secrets.GCP_PROJECT_ID }}
          TF_VAR_ssh_public_key: ${{ secrets.SSH_PUBLIC_KEY }}
          TF_VAR_existing_data_disk_name: ${{ secrets.EXISTING_DATA_DISK_NAME }}
        run: terraform apply -auto-approve tfplan
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/deploy-infra.yml
git commit -m "feat(infra): github actions deploy-infra workflow with oidc"
```

---

### Task 7: Makefile additions

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Read current Makefile to see existing targets**

Current content starts with `.PHONY: smoke card-totals`. Append the new targets at the end.

- [ ] **Step 2: Append to `Makefile`**

```makefile
VM_USER ?= debian
VM_IP   ?=

.PHONY: ssh-tunnel ssh-vm

ssh-tunnel:
	ssh -i ~/.ssh/lidl_bot -L 5678:localhost:5678 $(VM_USER)@$(VM_IP)

ssh-vm:
	ssh -i ~/.ssh/lidl_bot $(VM_USER)@$(VM_IP)
```

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "feat(infra): makefile targets for ssh-tunnel and ssh-vm"
```

---

### Task 8: First deploy and verification

**Files:** none (validation steps)

- [ ] **Step 1: Push to main to trigger GitHub Actions**

```bash
git push origin main
```

- [ ] **Step 2: Watch the workflow in GitHub Actions**

Open GitHub → Actions → "Deploy Infrastructure". The workflow should complete in 5–10 minutes (VM boot + startup script run).
Expected: all steps green, `Terraform Apply` shows `Apply complete! Resources: N added`.

- [ ] **Step 3: Get the VM IP from Terraform outputs**

In the GitHub Actions log, find the `Terraform Apply` step output. Or run locally after setting up local backend access:

```bash
cd infra
terraform init -backend-config="bucket=YOUR_TF_STATE_BUCKET" -backend-config="prefix=terraform/lidl"
terraform output vm_ip
```

- [ ] **Step 4: Update VM_IP in Makefile and test SSH**

```bash
# Replace the empty VM_IP line with the actual IP:
# VM_IP ?= 34.xxx.xxx.xxx
make ssh-vm
```

Expected: you land in a `debian@lidl-bot:~$` prompt.

- [ ] **Step 5: Verify Docker is installed on the VM**

```bash
# From inside the VM (after make ssh-vm):
docker --version
docker compose version
```

Expected: Docker version 26+ and Docker Compose version 2+.

- [ ] **Step 6: Verify /data is mounted**

```bash
# From inside the VM:
df -h /data
ls /data
```

Expected: `/data` shows ~10 GB available, contains `n8n/`, `signal-cli/`, `receipts/`.

- [ ] **Step 7: Verify swap**

```bash
# From inside the VM:
free -h
```

Expected: `Swap:` row shows `1.0Gi` total.

- [ ] **Step 8: Commit the updated VM_IP in Makefile**

```bash
git add Makefile
git commit -m "chore: set VM_IP in makefile after first deploy"
```

---

## Summary

After completing this plan you have:
- A running GCE e2-micro VM at a static IP in us-west1-a
- Docker + Docker Compose installed
- `/data` mounted from a persistent disk (new or existing)
- 1 GB swap to give headroom for Playwright
- SSH access via `make ssh-vm` and n8n UI tunnel via `make ssh-tunnel`
- GitHub Actions auto-deploys when `infra/**` changes

**Next:** Plan 2 (lidl-api) builds the FastAPI container independently — it can be developed locally in parallel with this plan.
