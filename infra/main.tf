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
