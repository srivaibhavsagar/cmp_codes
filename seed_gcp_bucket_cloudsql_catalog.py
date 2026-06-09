#!/usr/bin/env python3
"""
GCP Cloud Storage Bucket & Cloud SQL — Seed Script

Creates all CMP resources needed to provision:
  1. GCP Cloud Storage Bucket (Terraform + Native SDK)
  2. GCP Cloud SQL Instance (Terraform + Native SDK)

Both approaches use secure short-lived credentials from the selected
GCP cloud credential — the original service account key is never exposed.

Usage:
    python seed_gcp_bucket_cloudsql_catalog.py --url https://your-cmp.example.com --token <admin_jwt>
    python3 seed_gcp_bucket_cloudsql_catalog.py --url https://cmp-app.srivaibhavsagar.com/ --token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjNTk0M2U4NS02ZjMzLTQ4ZDAtYTcwYy00NzI2ZTM4MjUxZmMiLCJ0ZW5hbnRfaWQiOiJkZWZhdWx0IiwianRpIjoiZGNlNTM1MWItOTllNS00ZGU3LWJlZjctYjU5YmFlMTkxYThjIiwiZXhwIjoxNzg4NzM3NDk0LCJ0b2tlbl90eXBlIjoiYXBpIn0.e9n21HjwSCj0Z98c0HGmm0mGMOXB6FtC4ZM-qjc1TdQ
    
    # Or with environment variables:
    export CMP_URL=http://localhost:8001
    export CMP_TOKEN=eyJhbGciOiJIUzI1NiIs...
    python seed_gcp_bucket_cloudsql_catalog.py
"""

import argparse
import json
import os
import sys
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Terraform HCL Templates
# ─────────────────────────────────────────────────────────────────────────────

GCS_BUCKET_HCL = r'''
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_storage_bucket" "bucket" {
  name          = var.bucket_name
  location      = var.location
  project       = var.project_id
  storage_class = var.storage_class
  force_destroy = var.force_destroy

  uniform_bucket_level_access = var.uniform_access

  dynamic "versioning" {
    for_each = var.versioning_enabled ? [1] : []
    content {
      enabled = true
    }
  }

  dynamic "lifecycle_rule" {
    for_each = var.lifecycle_age_days > 0 ? [1] : []
    content {
      action {
        type = "Delete"
      }
      condition {
        age = var.lifecycle_age_days
      }
    }
  }

  labels = var.labels
}

output "bucket_name" {
  value       = google_storage_bucket.bucket.name
  description = "The name of the bucket"
}

output "bucket_url" {
  value       = google_storage_bucket.bucket.url
  description = "The gs:// URL of the bucket"
}

output "bucket_self_link" {
  value       = google_storage_bucket.bucket.self_link
  description = "Self link of the bucket"
}

output "storage_class" {
  value       = google_storage_bucket.bucket.storage_class
  description = "Storage class of the bucket"
}

output "location" {
  value       = google_storage_bucket.bucket.location
  description = "Location of the bucket"
}
'''

CLOUD_SQL_HCL = r'''
terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_sql_database_instance" "instance" {
  name             = var.instance_name
  database_version = var.database_version
  region           = var.region
  project          = var.project_id

  deletion_protection = var.deletion_protection

  settings {
    tier              = var.tier
    availability_type = var.availability_type
    disk_size         = var.disk_size_gb
    disk_type         = var.disk_type
    disk_autoresize   = var.disk_autoresize

    ip_configuration {
      ipv4_enabled    = var.public_ip_enabled
      private_network = var.private_network != "" ? var.private_network : null
    }

    backup_configuration {
      enabled            = var.backup_enabled
      start_time         = var.backup_start_time
      binary_log_enabled = var.database_version == "MYSQL_8_0" ? var.binary_log_enabled : false
    }

    database_flags {
      name  = "max_connections"
      value = var.max_connections
    }

    user_labels = var.labels
  }
}

resource "google_sql_database" "database" {
  name     = var.db_name
  instance = google_sql_database_instance.instance.name
  project  = var.project_id
}

resource "google_sql_user" "user" {
  name     = var.db_user
  instance = google_sql_database_instance.instance.name
  password = var.db_password
  project  = var.project_id
}

output "instance_name" {
  value       = google_sql_database_instance.instance.name
  description = "Cloud SQL instance name"
}

output "connection_name" {
  value       = google_sql_database_instance.instance.connection_name
  description = "Connection name for Cloud SQL Proxy"
}

output "public_ip" {
  value       = google_sql_database_instance.instance.public_ip_address
  description = "Public IP address (if enabled)"
}

output "private_ip" {
  value       = google_sql_database_instance.instance.private_ip_address
  description = "Private IP address"
}

output "database_version" {
  value       = google_sql_database_instance.instance.database_version
  description = "Database engine version"
}

output "self_link" {
  value       = google_sql_database_instance.instance.self_link
  description = "Self link of the instance"
}
'''

# ─────────────────────────────────────────────────────────────────────────────
# Native Python Task Code
# ─────────────────────────────────────────────────────────────────────────────

GCS_NATIVE_TASK_CODE = r'''"""
GCP Cloud Storage Bucket — Native Python Task

Creates a GCP Cloud Storage bucket using the Google Cloud Storage JSON API
with a short-lived OAuth2 access token. The original service account key
is never visible to this task.

CMP injects context as:
  cmp["credential"]["temp_access_token"]  — 1-hour OAuth2 token
  cmp["credential"]["project_id"]         — GCP project ID
  params["bucket_name"]                   — form data / step inputs
"""

import json
import sys

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package is required. Add it to task requirements.")
    sys.exit(1)

# ─── Extract credential context ─────────────────────────────────────────────
credential = cmp.get("credential", {})
token = credential.get("temp_access_token", "")
project_id = params.get("GCP_PROJECT_ID") or credential.get("project_id", "")

# ─── Extract form inputs ────────────────────────────────────────────────────
bucket_name = params.get("bucket_name", "")
location = params.get("location", "US")
storage_class = params.get("storage_class", "STANDARD")
uniform_access = params.get("uniform_access", "true").lower() == "true"
versioning_enabled = params.get("versioning_enabled", "false").lower() == "true"

# ─── Validation ─────────────────────────────────────────────────────────────
if not token:
    print("ERROR: No temp_access_token in credential context.")
    print(f"  Available credential keys: {list(credential.keys())}")
    sys.exit(1)

if not project_id:
    print("ERROR: No project_id found in credential or params.")
    sys.exit(1)

if not bucket_name:
    print("ERROR: bucket_name is required.")
    sys.exit(1)

print(f"[GCP] Creating bucket '{bucket_name}' in project {project_id}")
print(f"[GCP] Location: {location}, Storage class: {storage_class}")

# ─── Create bucket via JSON API ─────────────────────────────────────────────
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}

body = {
    "name": bucket_name,
    "location": location,
    "storageClass": storage_class,
    "iamConfiguration": {
        "uniformBucketLevelAccess": {"enabled": uniform_access}
    },
    "versioning": {"enabled": versioning_enabled},
    "labels": {"managed-by": "cmp"},
}

url = f"https://storage.googleapis.com/storage/v1/b?project={project_id}"

try:
    resp = requests.post(url, headers=headers, json=body, timeout=60)

    if resp.status_code in (200, 201):
        result = resp.json()
        print(f"[GCP] Bucket created successfully!")
        output = {
            "bucket_name": result["name"],
            "bucket_url": f"gs://{result['name']}",
            "self_link": result.get("selfLink", ""),
            "location": result.get("location", location),
            "storage_class": result.get("storageClass", storage_class),
            "project_id": project_id,
            "status": "created",
            "message": f"Cloud Storage bucket '{result['name']}' created in {result.get('location', location)}",
        }
        print(json.dumps(output))
    else:
        error = resp.json().get("error", {})
        msg = error.get("message", resp.text[:500])
        print(f"ERROR: GCP Storage API error [{resp.status_code}]: {msg}")
        sys.exit(1)

except Exception as e:
    print(f"ERROR: Failed to create bucket: {e}")
    sys.exit(1)
'''

CLOUD_SQL_NATIVE_TASK_CODE = r'''"""
GCP Cloud SQL Instance — Native Python Task

Creates a GCP Cloud SQL instance using the Cloud SQL Admin API
with a short-lived OAuth2 access token. The original service account key
is never visible to this task.

CMP injects context as:
  cmp["credential"]["temp_access_token"]  — 1-hour OAuth2 token
  cmp["credential"]["project_id"]         — GCP project ID
  params["instance_name"]                 — form data / step inputs
"""

import json
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package is required. Add it to task requirements.")
    sys.exit(1)

# ─── Extract credential context ─────────────────────────────────────────────
credential = cmp.get("credential", {})
token = credential.get("temp_access_token", "")
project_id = params.get("GCP_PROJECT_ID") or credential.get("project_id", "")

# ─── Extract form inputs ────────────────────────────────────────────────────
instance_name = params.get("instance_name", "")
region = params.get("region", "us-central1")
database_version = params.get("database_version", "POSTGRES_15")
tier = params.get("tier", "db-f1-micro")
disk_size_gb = int(params.get("disk_size_gb", "10"))
disk_type = params.get("disk_type", "PD_SSD")
availability_type = params.get("availability_type", "ZONAL")
public_ip_enabled = params.get("public_ip_enabled", "true").lower() == "true"
backup_enabled = params.get("backup_enabled", "true").lower() == "true"
db_name = params.get("db_name", "appdb")
db_user = params.get("db_user", "appuser")
db_password = params.get("db_password", "")

# ─── Validation ─────────────────────────────────────────────────────────────
if not token:
    print("ERROR: No temp_access_token in credential context.")
    print(f"  Available credential keys: {list(credential.keys())}")
    sys.exit(1)

if not project_id:
    print("ERROR: No project_id found in credential or params.")
    sys.exit(1)

if not instance_name:
    print("ERROR: instance_name is required.")
    sys.exit(1)

if not db_password:
    print("ERROR: db_password is required.")
    sys.exit(1)

print(f"[GCP] Creating Cloud SQL instance '{instance_name}' in project {project_id}")
print(f"[GCP] Region: {region}, Version: {database_version}, Tier: {tier}")
print(f"[GCP] Disk: {disk_size_gb}GB {disk_type}, Availability: {availability_type}")

# ─── Create Cloud SQL instance via Admin API ────────────────────────────────
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}

body = {
    "name": instance_name,
    "region": region,
    "databaseVersion": database_version,
    "settings": {
        "tier": tier,
        "availabilityType": availability_type,
        "dataDiskSizeGb": str(disk_size_gb),
        "dataDiskType": disk_type,
        "ipConfiguration": {
            "ipv4Enabled": public_ip_enabled,
        },
        "backupConfiguration": {
            "enabled": backup_enabled,
            "startTime": "03:00",
        },
        "userLabels": {"managed-by": "cmp"},
    },
}

base_url = f"https://sqladmin.googleapis.com/v1/projects/{project_id}/instances"

try:
    print(f"[GCP] Sending instances.insert request...")
    resp = requests.post(base_url, headers=headers, json=body, timeout=60)

    if resp.status_code in (200, 201):
        operation = resp.json()
        op_name = operation.get("name", "")
        print(f"[GCP] Operation started: {op_name}")

        # Poll for completion (Cloud SQL can take several minutes)
        op_url = f"https://sqladmin.googleapis.com/v1/projects/{project_id}/operations/{op_name}"
        max_wait = 600  # 10 minutes
        elapsed = 0
        poll_interval = 15

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval
            op_resp = requests.get(op_url, headers=headers, timeout=30)
            if op_resp.status_code == 200:
                op_data = op_resp.json()
                status = op_data.get("status", "UNKNOWN")
                print(f"[GCP] Operation status: {status} ({elapsed}s elapsed)")
                if status == "DONE":
                    break
            else:
                print(f"[GCP] Warning: Could not check operation status [{op_resp.status_code}]")

        # Fetch instance details
        instance_url = f"{base_url}/{instance_name}"
        inst_resp = requests.get(instance_url, headers=headers, timeout=30)

        if inst_resp.status_code == 200:
            instance_data = inst_resp.json()
            connection_name = instance_data.get("connectionName", "")
            ip_addresses = instance_data.get("ipAddresses", [])
            public_ip = ""
            private_ip = ""
            for ip_entry in ip_addresses:
                if ip_entry.get("type") == "PRIMARY":
                    public_ip = ip_entry.get("ipAddress", "")
                elif ip_entry.get("type") == "PRIVATE":
                    private_ip = ip_entry.get("ipAddress", "")

            # Create database
            print(f"[GCP] Creating database '{db_name}'...")
            db_url = f"{base_url}/{instance_name}/databases"
            db_body = {"name": db_name, "project": project_id, "instance": instance_name}
            db_resp = requests.post(db_url, headers=headers, json=db_body, timeout=60)
            if db_resp.status_code in (200, 201):
                print(f"[GCP] Database '{db_name}' created.")
            else:
                print(f"[GCP] Warning: Database creation returned [{db_resp.status_code}]")

            # Create user
            print(f"[GCP] Creating user '{db_user}'...")
            user_url = f"{base_url}/{instance_name}/users"
            user_body = {"name": db_user, "password": db_password, "project": project_id, "instance": instance_name}
            user_resp = requests.post(user_url, headers=headers, json=user_body, timeout=60)
            if user_resp.status_code in (200, 201):
                print(f"[GCP] User '{db_user}' created.")
            else:
                print(f"[GCP] Warning: User creation returned [{user_resp.status_code}]")

            output = {
                "instance_name": instance_name,
                "connection_name": connection_name,
                "public_ip": public_ip,
                "private_ip": private_ip,
                "database_version": database_version,
                "tier": tier,
                "region": region,
                "db_name": db_name,
                "db_user": db_user,
                "project_id": project_id,
                "status": "running",
                "message": f"Cloud SQL instance '{instance_name}' created in {region}",
            }
            print(json.dumps(output))
        else:
            print(f"[GCP] Instance created but could not fetch details [{inst_resp.status_code}]")
            output = {
                "instance_name": instance_name,
                "region": region,
                "database_version": database_version,
                "project_id": project_id,
                "status": "created",
                "message": f"Cloud SQL instance '{instance_name}' created (details pending)",
            }
            print(json.dumps(output))
    else:
        error = resp.json().get("error", {})
        msg = error.get("message", resp.text[:500])
        print(f"ERROR: Cloud SQL Admin API error [{resp.status_code}]: {msg}")
        sys.exit(1)

except requests.exceptions.RequestException as e:
    print(f"ERROR: Request failed: {e}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: Unexpected error: {e}")
    sys.exit(1)
'''

# ─────────────────────────────────────────────────────────────────────────────
# Form Field Definitions
# ─────────────────────────────────────────────────────────────────────────────

GCS_BUCKET_FORM_FIELDS = [
    {
        "field_id": "bucket_name",
        "label": "Bucket Name",
        "type": "string",
        "required": True,
        "placeholder": "my-project-data-bucket",
        "description": "Globally unique bucket name (lowercase, numbers, hyphens, underscores, 3-63 chars)",
        "validation": {
            "pattern": "^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$",
            "message": "Must be 3-63 chars: lowercase letters, numbers, hyphens, underscores, dots",
        },
    },
    {
        "field_id": "location",
        "label": "Location",
        "type": "select",
        "required": True,
        "default": "US",
        "options": [
            {"label": "US (Multi-region)", "value": "US"},
            {"label": "EU (Multi-region)", "value": "EU"},
            {"label": "ASIA (Multi-region)", "value": "ASIA"},
            {"label": "us-central1 (Iowa)", "value": "us-central1"},
            {"label": "us-east1 (South Carolina)", "value": "us-east1"},
            {"label": "us-west1 (Oregon)", "value": "us-west1"},
            {"label": "europe-west1 (Belgium)", "value": "europe-west1"},
            {"label": "europe-west2 (London)", "value": "europe-west2"},
            {"label": "asia-east1 (Taiwan)", "value": "asia-east1"},
            {"label": "asia-southeast1 (Singapore)", "value": "asia-southeast1"},
            {"label": "australia-southeast1 (Sydney)", "value": "australia-southeast1"},
        ],
        "description": "Bucket location — multi-region or single region",
    },
    {
        "field_id": "storage_class",
        "label": "Storage Class",
        "type": "select",
        "required": True,
        "default": "STANDARD",
        "options": [
            {"label": "Standard — Frequent access, highest availability", "value": "STANDARD"},
            {"label": "Nearline — Accessed less than once/month (30-day min)", "value": "NEARLINE"},
            {"label": "Coldline — Accessed less than once/quarter (90-day min)", "value": "COLDLINE"},
            {"label": "Archive — Accessed less than once/year (365-day min)", "value": "ARCHIVE"},
        ],
        "description": "Storage class affecting cost and access frequency",
    },
    {
        "field_id": "uniform_access",
        "label": "Uniform Bucket-Level Access",
        "type": "select",
        "required": True,
        "default": "true",
        "options": [
            {"label": "Enabled (recommended)", "value": "true"},
            {"label": "Disabled (allows ACLs)", "value": "false"},
        ],
        "description": "When enabled, access is controlled solely via IAM (recommended)",
    },
    {
        "field_id": "versioning_enabled",
        "label": "Object Versioning",
        "type": "select",
        "required": True,
        "default": "false",
        "options": [
            {"label": "Disabled", "value": "false"},
            {"label": "Enabled", "value": "true"},
        ],
        "description": "Keep previous versions of objects when overwritten or deleted",
    },
    {
        "field_id": "lifecycle_age_days",
        "label": "Auto-Delete After (days)",
        "type": "select",
        "required": False,
        "default": "0",
        "options": [
            {"label": "No auto-delete", "value": "0"},
            {"label": "30 days", "value": "30"},
            {"label": "60 days", "value": "60"},
            {"label": "90 days", "value": "90"},
            {"label": "180 days", "value": "180"},
            {"label": "365 days", "value": "365"},
        ],
        "description": "Automatically delete objects older than this. Set 0 to disable.",
    },
]

CLOUD_SQL_FORM_FIELDS = [
    {
        "field_id": "instance_name",
        "label": "Instance Name",
        "type": "string",
        "required": True,
        "placeholder": "my-app-db-01",
        "description": "Cloud SQL instance name (lowercase, letters, numbers, hyphens, max 98 chars)",
        "validation": {
            "pattern": "^[a-z][a-z0-9-]{0,97}$",
            "message": "Must start with a letter, lowercase + numbers + hyphens, max 98 chars",
        },
    },
    {
        "field_id": "database_version",
        "label": "Database Engine",
        "type": "select",
        "required": True,
        "default": "POSTGRES_15",
        "options": [
            {"label": "PostgreSQL 15", "value": "POSTGRES_15"},
            {"label": "PostgreSQL 14", "value": "POSTGRES_14"},
            {"label": "PostgreSQL 13", "value": "POSTGRES_13"},
            {"label": "MySQL 8.0", "value": "MYSQL_8_0"},
            {"label": "MySQL 5.7", "value": "MYSQL_5_7"},
            {"label": "SQL Server 2019 Standard", "value": "SQLSERVER_2019_STANDARD"},
            {"label": "SQL Server 2019 Express", "value": "SQLSERVER_2019_EXPRESS"},
        ],
        "description": "Database engine and version",
    },
    {
        "field_id": "region",
        "label": "Region",
        "type": "select",
        "required": True,
        "default": "us-central1",
        "options": [
            {"label": "us-central1 (Iowa)", "value": "us-central1"},
            {"label": "us-east1 (South Carolina)", "value": "us-east1"},
            {"label": "us-east4 (Northern Virginia)", "value": "us-east4"},
            {"label": "us-west1 (Oregon)", "value": "us-west1"},
            {"label": "europe-west1 (Belgium)", "value": "europe-west1"},
            {"label": "europe-west2 (London)", "value": "europe-west2"},
            {"label": "asia-east1 (Taiwan)", "value": "asia-east1"},
            {"label": "asia-southeast1 (Singapore)", "value": "asia-southeast1"},
            {"label": "australia-southeast1 (Sydney)", "value": "australia-southeast1"},
        ],
        "description": "Region for the Cloud SQL instance",
    },
    {
        "field_id": "tier",
        "label": "Machine Tier",
        "type": "select",
        "required": True,
        "default": "db-f1-micro",
        "options": [
            {"label": "db-f1-micro (Shared, 0.6 GB) — Dev/Test", "value": "db-f1-micro"},
            {"label": "db-g1-small (Shared, 1.7 GB) — Small apps", "value": "db-g1-small"},
            {"label": "db-custom-1-3840 (1 vCPU, 3.75 GB)", "value": "db-custom-1-3840"},
            {"label": "db-custom-2-7680 (2 vCPU, 7.5 GB)", "value": "db-custom-2-7680"},
            {"label": "db-custom-4-15360 (4 vCPU, 15 GB)", "value": "db-custom-4-15360"},
            {"label": "db-custom-8-30720 (8 vCPU, 30 GB)", "value": "db-custom-8-30720"},
            {"label": "db-custom-16-61440 (16 vCPU, 60 GB)", "value": "db-custom-16-61440"},
        ],
        "description": "Machine tier determining CPU and memory for the database",
    },
    {
        "field_id": "disk_size_gb",
        "label": "Disk Size (GB)",
        "type": "select",
        "required": True,
        "default": "10",
        "options": [
            {"label": "10 GB", "value": "10"},
            {"label": "20 GB", "value": "20"},
            {"label": "50 GB", "value": "50"},
            {"label": "100 GB", "value": "100"},
            {"label": "250 GB", "value": "250"},
            {"label": "500 GB", "value": "500"},
            {"label": "1000 GB (1 TB)", "value": "1000"},
        ],
        "description": "Storage capacity for the database",
    },
    {
        "field_id": "disk_type",
        "label": "Disk Type",
        "type": "select",
        "required": True,
        "default": "PD_SSD",
        "options": [
            {"label": "SSD (Recommended for production)", "value": "PD_SSD"},
            {"label": "HDD (Lower cost, lower performance)", "value": "PD_HDD"},
        ],
        "description": "Disk type for storage performance",
    },
    {
        "field_id": "availability_type",
        "label": "Availability",
        "type": "select",
        "required": True,
        "default": "ZONAL",
        "options": [
            {"label": "Zonal — Single zone (lower cost)", "value": "ZONAL"},
            {"label": "Regional — Multi-zone HA (automatic failover)", "value": "REGIONAL"},
        ],
        "description": "Zonal for dev/test, Regional for production HA with automatic failover",
    },
    {
        "field_id": "public_ip_enabled",
        "label": "Public IP",
        "type": "select",
        "required": True,
        "default": "true",
        "options": [
            {"label": "Enabled (accessible via public IP)", "value": "true"},
            {"label": "Disabled (private network only)", "value": "false"},
        ],
        "description": "Enable public IP for external access. Disable for private-only instances.",
    },
    {
        "field_id": "backup_enabled",
        "label": "Automated Backups",
        "type": "select",
        "required": True,
        "default": "true",
        "options": [
            {"label": "Enabled (recommended)", "value": "true"},
            {"label": "Disabled", "value": "false"},
        ],
        "description": "Enable daily automated backups",
    },
    {
        "field_id": "db_name",
        "label": "Database Name",
        "type": "string",
        "required": True,
        "default": "appdb",
        "placeholder": "appdb",
        "description": "Name of the initial database to create",
    },
    {
        "field_id": "db_user",
        "label": "Database User",
        "type": "string",
        "required": True,
        "default": "appuser",
        "placeholder": "appuser",
        "description": "Name of the initial database user",
    },
    {
        "field_id": "db_password",
        "label": "Database Password",
        "type": "password",
        "required": True,
        "placeholder": "••••••••",
        "description": "Password for the database user (min 8 chars, stored encrypted)",
        "validation": {
            "min_length": 8,
            "message": "Password must be at least 8 characters",
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# CMP API Client
# ─────────────────────────────────────────────────────────────────────────────


class CMPClient:
    """Minimal CMP API client with upsert support."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    def post(self, path: str, payload: dict) -> dict:
        resp = requests.post(self._url(path), json=payload, headers=self.headers, timeout=30)
        if resp.status_code not in (200, 201):
            print(f"  ERROR [{resp.status_code}] POST {path}: {resp.text[:500]}")
            resp.raise_for_status()
        return resp.json()

    def put(self, path: str, payload: dict) -> dict:
        resp = requests.put(self._url(path), json=payload, headers=self.headers, timeout=30)
        if resp.status_code not in (200, 201):
            print(f"  ERROR [{resp.status_code}] PUT {path}: {resp.text[:500]}")
            resp.raise_for_status()
        return resp.json()

    def get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(self._url(path), headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_safe(self, path: str, params: dict = None):
        """GET that returns None on 404 instead of raising."""
        resp = requests.get(self._url(path), headers=self.headers, params=params, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def find_by_name(self, path: str, name: str):
        """List resources and find one matching the exact name. Returns dict or None."""
        items = self.get(path, params={"name": name})
        if isinstance(items, list):
            for item in items:
                if item.get("name") == name:
                    return item
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Seed Functions — GCS Bucket
# ─────────────────────────────────────────────────────────────────────────────


def create_gcs_terraform_template(client: CMPClient) -> str:
    """Create or update the GCS Bucket Terraform template."""
    print("\n[GCS 1/6] Terraform template: GCP Cloud Storage Bucket...")
    name = "GCP Cloud Storage Bucket"
    payload = {
        "name": name,
        "description": (
            "Provisions a GCP Cloud Storage bucket with configurable location, "
            "storage class, versioning, lifecycle rules, and access controls. "
            "Uses google provider ~> 5.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": GCS_BUCKET_HCL},
        "input_variables": [
            {"name": "project_id", "type": "string", "description": "GCP Project ID", "required": True},
            {"name": "region", "type": "string", "description": "GCP region (for provider config)", "default": "us-central1"},
            {"name": "bucket_name", "type": "string", "description": "Globally unique bucket name", "required": True},
            {"name": "location", "type": "string", "description": "Bucket location (region or multi-region)", "default": "US", "required": True},
            {"name": "storage_class", "type": "string", "description": "Storage class", "default": "STANDARD", "required": True},
            {"name": "uniform_access", "type": "bool", "description": "Enable uniform bucket-level access", "default": True},
            {"name": "versioning_enabled", "type": "bool", "description": "Enable object versioning", "default": False},
            {"name": "force_destroy", "type": "bool", "description": "Allow bucket deletion even with objects", "default": False},
            {"name": "lifecycle_age_days", "type": "number", "description": "Auto-delete objects after N days (0 = disabled)", "default": 0},
            {"name": "labels", "type": "map", "description": "Labels to apply to the bucket", "default": {}},
        ],
        "output_definitions": [
            {"name": "bucket_name", "description": "The name of the bucket"},
            {"name": "bucket_url", "description": "The gs:// URL of the bucket"},
            {"name": "bucket_self_link", "description": "Self link of the bucket"},
            {"name": "storage_class", "description": "Storage class of the bucket"},
            {"name": "location", "description": "Location of the bucket"},
        ],
        "required_providers": {"google": "~> 5.0"},
        "supported_providers": ["gcp"],
        "tags": ["gcp", "storage", "bucket", "gcs", "day1", "seed"],
    }

    existing = client.find_by_name("/terraform/templates", name)
    if existing:
        template_id = existing["template_id"]
        client.put(f"/terraform/templates/{template_id}", payload)
        print(f"  ✓ Template updated: {template_id}")
    else:
        result = client.post("/terraform/templates", payload)
        template_id = result["template_id"]
        print(f"  ✓ Template created: {template_id}")
    return template_id


def create_gcs_native_task(client: CMPClient) -> str:
    """Create or update the native Python GCS bucket provisioning task."""
    print("\n[GCS 2/6] Native Python task: GCP Cloud Storage Bucket (Native SDK)...")
    name = "GCP Cloud Storage Bucket (Native SDK)"
    payload = {
        "name": name,
        "description": (
            "Creates a GCP Cloud Storage bucket using the Storage JSON API "
            "with a short-lived OAuth2 access token. Never accesses the raw service account key."
        ),
        "language": "python",
        "code": GCS_NATIVE_TASK_CODE,
        "requirements": "requests",
        "input_schema": {
            "type": "object",
            "properties": {
                "bucket_name": {"type": "string", "description": "Globally unique bucket name"},
                "location": {"type": "string", "description": "Bucket location"},
                "storage_class": {"type": "string", "description": "Storage class"},
                "uniform_access": {"type": "string", "description": "Enable uniform access (true/false)"},
                "versioning_enabled": {"type": "string", "description": "Enable versioning (true/false)"},
            },
            "required": ["bucket_name"],
        },
        "tags": ["gcp", "storage", "bucket", "gcs", "native", "sdk", "seed"],
        "write_output_to_payload": True,
    }

    existing = client.find_by_name("/tasks", name)
    if existing:
        task_id = existing["task_id"]
        client.put(f"/tasks/{task_id}", payload)
        print(f"  ✓ Task updated: {task_id}")
    else:
        result = client.post("/tasks", payload)
        task_id = result["task_id"]
        print(f"  ✓ Task created: {task_id}")
    return task_id


def create_gcs_terraform_workflow(client: CMPClient, template_id: str) -> str:
    """Create or update a workflow that uses Terraform to provision a GCS bucket."""
    print("\n[GCS 3/6] Terraform workflow...")
    name = "gcs-bucket-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions a GCP Cloud Storage bucket using Terraform with the google provider.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — GCS Bucket",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "project_id": "{{credential.project_id}}",
                    "bucket_name": "{{form.bucket_name}}",
                    "location": "{{form.location}}",
                    "storage_class": "{{form.storage_class}}",
                    "uniform_access": "{{form.uniform_access}}",
                    "versioning_enabled": "{{form.versioning_enabled}}",
                    "lifecycle_age_days": "{{form.lifecycle_age_days}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 300,
            }
        ],
        "tags": ["gcp", "storage", "gcs", "terraform", "seed"],
    }

    existing = client.find_by_name("/workflows", name)
    if existing:
        workflow_id = existing["workflow_id"]
        client.put(f"/workflows/{workflow_id}", payload)
        print(f"  ✓ Workflow updated: {workflow_id}")
    else:
        result = client.post("/workflows", payload)
        workflow_id = result["workflow_id"]
        print(f"  ✓ Workflow created: {workflow_id}")
    return workflow_id


def create_gcs_native_workflow(client: CMPClient, task_id: str) -> str:
    """Create or update a workflow that uses the native Python task to create a GCS bucket."""
    print("\n[GCS 4/6] Native SDK workflow...")
    name = "gcs-bucket-native-provision"
    payload = {
        "name": name,
        "description": "Creates a GCP Cloud Storage bucket using the native Python SDK with a short-lived temp token.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Create GCS Bucket (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "bucket_name": "{{form.bucket_name}}",
                    "location": "{{form.location}}",
                    "storage_class": "{{form.storage_class}}",
                    "uniform_access": "{{form.uniform_access}}",
                    "versioning_enabled": "{{form.versioning_enabled}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 120,
            }
        ],
        "tags": ["gcp", "storage", "gcs", "native", "sdk", "seed"],
    }

    existing = client.find_by_name("/workflows", name)
    if existing:
        workflow_id = existing["workflow_id"]
        client.put(f"/workflows/{workflow_id}", payload)
        print(f"  ✓ Workflow updated: {workflow_id}")
    else:
        result = client.post("/workflows", payload)
        workflow_id = result["workflow_id"]
        print(f"  ✓ Workflow created: {workflow_id}")
    return workflow_id


# ─────────────────────────────────────────────────────────────────────────────
# Seed Functions — Cloud SQL
# ─────────────────────────────────────────────────────────────────────────────


def create_cloudsql_terraform_template(client: CMPClient) -> str:
    """Create or update the Cloud SQL Terraform template."""
    print("\n[SQL 1/6] Terraform template: GCP Cloud SQL Instance...")
    name = "GCP Cloud SQL Instance"
    payload = {
        "name": name,
        "description": (
            "Provisions a GCP Cloud SQL instance (PostgreSQL, MySQL, or SQL Server) "
            "with configurable tier, disk, HA, backups, and initial database/user. "
            "Uses google provider ~> 5.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": CLOUD_SQL_HCL},
        "input_variables": [
            {"name": "project_id", "type": "string", "description": "GCP Project ID", "required": True},
            {"name": "region", "type": "string", "description": "GCP region", "default": "us-central1", "required": True},
            {"name": "instance_name", "type": "string", "description": "Cloud SQL instance name", "required": True},
            {"name": "database_version", "type": "string", "description": "Database version (e.g. POSTGRES_15, MYSQL_8_0)", "default": "POSTGRES_15", "required": True},
            {"name": "tier", "type": "string", "description": "Machine tier", "default": "db-f1-micro", "required": True},
            {"name": "disk_size_gb", "type": "number", "description": "Disk size in GB", "default": 10, "required": True},
            {"name": "disk_type", "type": "string", "description": "Disk type (PD_SSD or PD_HDD)", "default": "PD_SSD", "required": True},
            {"name": "disk_autoresize", "type": "bool", "description": "Enable disk auto-resize", "default": True},
            {"name": "availability_type", "type": "string", "description": "ZONAL or REGIONAL", "default": "ZONAL", "required": True},
            {"name": "public_ip_enabled", "type": "bool", "description": "Enable public IP", "default": True},
            {"name": "private_network", "type": "string", "description": "VPC network self_link for private IP", "default": ""},
            {"name": "backup_enabled", "type": "bool", "description": "Enable automated backups", "default": True},
            {"name": "backup_start_time", "type": "string", "description": "Backup window start (HH:MM)", "default": "03:00"},
            {"name": "binary_log_enabled", "type": "bool", "description": "Enable binary logging (MySQL only)", "default": False},
            {"name": "max_connections", "type": "string", "description": "Max database connections", "default": "100"},
            {"name": "deletion_protection", "type": "bool", "description": "Prevent accidental deletion", "default": True},
            {"name": "db_name", "type": "string", "description": "Initial database name", "default": "appdb", "required": True},
            {"name": "db_user", "type": "string", "description": "Initial database user", "default": "appuser", "required": True},
            {"name": "db_password", "type": "string", "description": "Database user password", "required": True, "sensitive": True},
            {"name": "labels", "type": "map", "description": "Labels for the instance", "default": {}},
        ],
        "output_definitions": [
            {"name": "instance_name", "description": "Cloud SQL instance name"},
            {"name": "connection_name", "description": "Connection name for Cloud SQL Proxy"},
            {"name": "public_ip", "description": "Public IP address (if enabled)"},
            {"name": "private_ip", "description": "Private IP address"},
            {"name": "database_version", "description": "Database engine version"},
            {"name": "self_link", "description": "Self link of the instance"},
        ],
        "required_providers": {"google": "~> 5.0"},
        "supported_providers": ["gcp"],
        "tags": ["gcp", "database", "cloudsql", "rds", "day1", "seed"],
    }

    existing = client.find_by_name("/terraform/templates", name)
    if existing:
        template_id = existing["template_id"]
        client.put(f"/terraform/templates/{template_id}", payload)
        print(f"  ✓ Template updated: {template_id}")
    else:
        result = client.post("/terraform/templates", payload)
        template_id = result["template_id"]
        print(f"  ✓ Template created: {template_id}")
    return template_id


def create_cloudsql_native_task(client: CMPClient) -> str:
    """Create or update the native Python Cloud SQL provisioning task."""
    print("\n[SQL 2/6] Native Python task: GCP Cloud SQL Instance (Native SDK)...")
    name = "GCP Cloud SQL Instance (Native SDK)"
    payload = {
        "name": name,
        "description": (
            "Creates a GCP Cloud SQL instance using the Cloud SQL Admin API "
            "with a short-lived OAuth2 access token. Creates instance, database, and user. "
            "Never accesses the raw service account key."
        ),
        "language": "python",
        "code": CLOUD_SQL_NATIVE_TASK_CODE,
        "requirements": "requests",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_name": {"type": "string", "description": "Cloud SQL instance name"},
                "region": {"type": "string", "description": "GCP region"},
                "database_version": {"type": "string", "description": "Database version"},
                "tier": {"type": "string", "description": "Machine tier"},
                "disk_size_gb": {"type": "string", "description": "Disk size in GB"},
                "disk_type": {"type": "string", "description": "Disk type"},
                "availability_type": {"type": "string", "description": "ZONAL or REGIONAL"},
                "public_ip_enabled": {"type": "string", "description": "Enable public IP"},
                "backup_enabled": {"type": "string", "description": "Enable backups"},
                "db_name": {"type": "string", "description": "Initial database name"},
                "db_user": {"type": "string", "description": "Initial database user"},
                "db_password": {"type": "string", "description": "Database password"},
            },
            "required": ["instance_name", "db_password"],
        },
        "tags": ["gcp", "database", "cloudsql", "rds", "native", "sdk", "seed"],
        "write_output_to_payload": True,
    }

    existing = client.find_by_name("/tasks", name)
    if existing:
        task_id = existing["task_id"]
        client.put(f"/tasks/{task_id}", payload)
        print(f"  ✓ Task updated: {task_id}")
    else:
        result = client.post("/tasks", payload)
        task_id = result["task_id"]
        print(f"  ✓ Task created: {task_id}")
    return task_id


def create_cloudsql_terraform_workflow(client: CMPClient, template_id: str) -> str:
    """Create or update a workflow that uses Terraform to provision Cloud SQL."""
    print("\n[SQL 3/6] Terraform workflow...")
    name = "cloudsql-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions a GCP Cloud SQL instance using Terraform with the google provider.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — Cloud SQL",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "project_id": "{{credential.project_id}}",
                    "region": "{{form.region}}",
                    "instance_name": "{{form.instance_name}}",
                    "database_version": "{{form.database_version}}",
                    "tier": "{{form.tier}}",
                    "disk_size_gb": "{{form.disk_size_gb}}",
                    "disk_type": "{{form.disk_type}}",
                    "availability_type": "{{form.availability_type}}",
                    "public_ip_enabled": "{{form.public_ip_enabled}}",
                    "backup_enabled": "{{form.backup_enabled}}",
                    "db_name": "{{form.db_name}}",
                    "db_user": "{{form.db_user}}",
                    "db_password": "{{form.db_password}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 900,
            }
        ],
        "tags": ["gcp", "database", "cloudsql", "terraform", "seed"],
    }

    existing = client.find_by_name("/workflows", name)
    if existing:
        workflow_id = existing["workflow_id"]
        client.put(f"/workflows/{workflow_id}", payload)
        print(f"  ✓ Workflow updated: {workflow_id}")
    else:
        result = client.post("/workflows", payload)
        workflow_id = result["workflow_id"]
        print(f"  ✓ Workflow created: {workflow_id}")
    return workflow_id


def create_cloudsql_native_workflow(client: CMPClient, task_id: str) -> str:
    """Create or update a workflow that uses the native Python task to provision Cloud SQL."""
    print("\n[SQL 4/6] Native SDK workflow...")
    name = "cloudsql-native-provision"
    payload = {
        "name": name,
        "description": "Creates a GCP Cloud SQL instance using the Cloud SQL Admin API with a short-lived temp token.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Create Cloud SQL Instance (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "instance_name": "{{form.instance_name}}",
                    "region": "{{form.region}}",
                    "database_version": "{{form.database_version}}",
                    "tier": "{{form.tier}}",
                    "disk_size_gb": "{{form.disk_size_gb}}",
                    "disk_type": "{{form.disk_type}}",
                    "availability_type": "{{form.availability_type}}",
                    "public_ip_enabled": "{{form.public_ip_enabled}}",
                    "backup_enabled": "{{form.backup_enabled}}",
                    "db_name": "{{form.db_name}}",
                    "db_user": "{{form.db_user}}",
                    "db_password": "{{form.db_password}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 900,
            }
        ],
        "tags": ["gcp", "database", "cloudsql", "native", "sdk", "seed"],
    }

    existing = client.find_by_name("/workflows", name)
    if existing:
        workflow_id = existing["workflow_id"]
        client.put(f"/workflows/{workflow_id}", payload)
        print(f"  ✓ Workflow updated: {workflow_id}")
    else:
        result = client.post("/workflows", payload)
        workflow_id = result["workflow_id"]
        print(f"  ✓ Workflow created: {workflow_id}")
    return workflow_id


# ─────────────────────────────────────────────────────────────────────────────
# Shared Helpers
# ─────────────────────────────────────────────────────────────────────────────


def create_flow(client: CMPClient, workflow_id: str, name: str, desc: str, tags: list) -> str:
    """Create or update a flow wrapping a single workflow."""
    payload = {
        "name": name,
        "description": desc,
        "workflows": [
            {
                "workflow_id": workflow_id,
                "order": 1,
                "depends_on": [],
                "input_mapping": {},
            }
        ],
        "tags": tags,
    }

    existing = client.find_by_name("/flows", name)
    if existing:
        flow_id = existing["flow_id"]
        client.put(f"/flows/{flow_id}", payload)
        print(f"  ✓ Flow updated: {flow_id}")
    else:
        result = client.post("/flows", payload)
        flow_id = result["flow_id"]
        print(f"  ✓ Flow created: {flow_id}")
    return flow_id


def create_catalog(
    client: CMPClient,
    flow_id: str,
    name: str,
    description: str,
    tags: list,
    form_fields: list,
) -> str:
    """Create or update a published Day-1 catalog item."""
    payload = {
        "name": name,
        "description": description,
        "tags": tags,
        "catalog_type": "day1",
        "cloud_provider": "gcp",
        "ui_mode": "form_builder",
        "flow_id": flow_id,
        "allowed_roles": ["admin", "developer", "user"],
        "status": "published",
        "show_cost_estimate": True,
        "show_live_pricing": True,
        "form_schema": {"fields": form_fields},
    }

    existing = client.find_by_name("/catalog", name)
    if existing:
        catalog_id = existing["catalog_id"]
        client.put(f"/catalog/{catalog_id}", payload)
        print(f"  ✓ Catalog updated: {catalog_id}")
    else:
        result = client.post("/catalog", payload)
        catalog_id = result["catalog_id"]
        print(f"  ✓ Catalog created: {catalog_id}")
    return catalog_id


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Seed GCP Cloud Storage Bucket & Cloud SQL catalogs into CMP"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CMP_URL", "http://localhost:8001"),
        help="CMP appliance URL (default: $CMP_URL or http://localhost:8001)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CMP_TOKEN", ""),
        help="Admin JWT token (default: $CMP_TOKEN)",
    )
    args = parser.parse_args()

    if not args.token:
        print("ERROR: No token provided. Use --token or set CMP_TOKEN env var.")
        sys.exit(1)

    client = CMPClient(args.url, args.token)

    print("=" * 70)
    print("  GCP Cloud Storage & Cloud SQL — CMP Seed Script")
    print("=" * 70)
    print(f"  Target: {args.url}")
    print(f"  Token:  {args.token[:20]}...")
    print("=" * 70)

    # ══════════════════════════════════════════════════════════════════════
    # GCS BUCKET
    # ══════════════════════════════════════════════════════════════════════

    print("\n" + "═" * 70)
    print("  GCP CLOUD STORAGE BUCKET")
    print("═" * 70)

    # ── Terraform-based ──────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    gcs_template_id = create_gcs_terraform_template(client)
    gcs_tf_workflow_id = create_gcs_terraform_workflow(client, gcs_template_id)

    print("\n[GCS 5/6] Creating Terraform flow...")
    gcs_tf_flow_id = create_flow(
        client,
        gcs_tf_workflow_id,
        name="gcs-bucket-terraform-flow",
        desc="Flow for GCP Cloud Storage Bucket provisioning via Terraform",
        tags=["gcp", "storage", "gcs", "terraform", "seed"],
    )

    print("\n[GCS 6/6] Creating Terraform catalog item...")
    gcs_tf_catalog_id = create_catalog(
        client,
        gcs_tf_flow_id,
        name="GCP Cloud Storage Bucket (Terraform)",
        description=(
            "Provision a Google Cloud Storage bucket using Terraform. "
            "Configure location, storage class, versioning, lifecycle rules, "
            "and access controls. Full IaC with state management."
        ),
        tags=["gcp", "storage", "bucket", "gcs", "terraform", "seed"],
        form_fields=GCS_BUCKET_FORM_FIELDS,
    )

    # ── Native SDK-based ─────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + Storage JSON API)")
    print("─" * 70)

    gcs_task_id = create_gcs_native_task(client)
    gcs_native_workflow_id = create_gcs_native_workflow(client, gcs_task_id)

    print("\n[GCS 5/6] Creating native flow...")
    gcs_native_flow_id = create_flow(
        client,
        gcs_native_workflow_id,
        name="gcs-bucket-native-flow",
        desc="Flow for GCP Cloud Storage Bucket provisioning via native Python SDK",
        tags=["gcp", "storage", "gcs", "native", "sdk", "seed"],
    )

    print("\n[GCS 6/6] Creating native catalog item...")
    gcs_native_catalog_id = create_catalog(
        client,
        gcs_native_flow_id,
        name="GCP Cloud Storage Bucket (Native SDK)",
        description=(
            "Create a Google Cloud Storage bucket using the native GCP Storage JSON API. "
            "Uses a short-lived OAuth2 access token (1hr) from the selected credential. "
            "The service account key is never exposed to the provisioning task."
        ),
        tags=["gcp", "storage", "bucket", "gcs", "native", "sdk", "seed"],
        form_fields=GCS_BUCKET_FORM_FIELDS,
    )

    # ══════════════════════════════════════════════════════════════════════
    # CLOUD SQL
    # ══════════════════════════════════════════════════════════════════════

    print("\n" + "═" * 70)
    print("  GCP CLOUD SQL INSTANCE")
    print("═" * 70)

    # ── Terraform-based ──────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    sql_template_id = create_cloudsql_terraform_template(client)
    sql_tf_workflow_id = create_cloudsql_terraform_workflow(client, sql_template_id)

    print("\n[SQL 5/6] Creating Terraform flow...")
    sql_tf_flow_id = create_flow(
        client,
        sql_tf_workflow_id,
        name="cloudsql-terraform-flow",
        desc="Flow for GCP Cloud SQL Instance provisioning via Terraform",
        tags=["gcp", "database", "cloudsql", "terraform", "seed"],
    )

    print("\n[SQL 6/6] Creating Terraform catalog item...")
    sql_tf_catalog_id = create_catalog(
        client,
        sql_tf_flow_id,
        name="GCP Cloud SQL Instance (Terraform)",
        description=(
            "Provision a Google Cloud SQL database instance using Terraform. "
            "Supports PostgreSQL, MySQL, and SQL Server with configurable tier, "
            "HA, backups, and initial database/user. Full IaC with state management."
        ),
        tags=["gcp", "database", "cloudsql", "rds", "terraform", "seed"],
        form_fields=CLOUD_SQL_FORM_FIELDS,
    )

    # ── Native SDK-based ─────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + Cloud SQL Admin API)")
    print("─" * 70)

    sql_task_id = create_cloudsql_native_task(client)
    sql_native_workflow_id = create_cloudsql_native_workflow(client, sql_task_id)

    print("\n[SQL 5/6] Creating native flow...")
    sql_native_flow_id = create_flow(
        client,
        sql_native_workflow_id,
        name="cloudsql-native-flow",
        desc="Flow for GCP Cloud SQL Instance provisioning via native Python SDK",
        tags=["gcp", "database", "cloudsql", "native", "sdk", "seed"],
    )

    print("\n[SQL 6/6] Creating native catalog item...")
    sql_native_catalog_id = create_catalog(
        client,
        sql_native_flow_id,
        name="GCP Cloud SQL Instance (Native SDK)",
        description=(
            "Create a Google Cloud SQL database instance using the Cloud SQL Admin API. "
            "Supports PostgreSQL, MySQL, and SQL Server. Uses a short-lived OAuth2 token. "
            "Creates instance, database, and user in one provisioning flow."
        ),
        tags=["gcp", "database", "cloudsql", "rds", "native", "sdk", "seed"],
        form_fields=CLOUD_SQL_FORM_FIELDS,
    )

    # ══════════════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════════════

    print("\n" + "=" * 70)
    print("  DONE! Summary of created resources:")
    print("=" * 70)
    print(f"""
  GCP CLOUD STORAGE BUCKET:
  ─────────────────────────
    Terraform Approach:
      Template ID:  {gcs_template_id}
      Workflow ID:  {gcs_tf_workflow_id}
      Flow ID:      {gcs_tf_flow_id}
      Catalog ID:   {gcs_tf_catalog_id}

    Native SDK Approach:
      Task ID:      {gcs_task_id}
      Workflow ID:  {gcs_native_workflow_id}
      Flow ID:      {gcs_native_flow_id}
      Catalog ID:   {gcs_native_catalog_id}

  GCP CLOUD SQL INSTANCE:
  ───────────────────────
    Terraform Approach:
      Template ID:  {sql_template_id}
      Workflow ID:  {sql_tf_workflow_id}
      Flow ID:      {sql_tf_flow_id}
      Catalog ID:   {sql_tf_catalog_id}

    Native SDK Approach:
      Task ID:      {sql_task_id}
      Workflow ID:  {sql_native_workflow_id}
      Flow ID:      {sql_native_flow_id}
      Catalog ID:   {sql_native_catalog_id}

  HOW TO TEST:
    1. Navigate to the CMP Catalog page
    2. Select one of the new GCP catalog items
    3. Choose your onboarded GCP credential
    4. Fill in the configuration form and submit
    5. Monitor execution in the Executions page

  CREDENTIAL SECURITY:
    - Terraform: SA JSON → temp file → GOOGLE_APPLICATION_CREDENTIALS → deleted after execution
    - Native:    SA JSON → OAuth2 token (1hr) → passed as temp_access_token
    - In BOTH cases, the raw service account key is never visible to the user or task code
""")


if __name__ == "__main__":
    main()
