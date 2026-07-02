#!/usr/bin/env python3
"""
GCP VM Provisioning — Seed Script

Creates all CMP resources needed to provision GCP Compute Engine VMs:
  1. Terraform-based provisioning (IaC approach)
  2. Native Python task-based provisioning (SDK approach)

Both approaches use secure short-lived credentials from the selected
GCP cloud credential — the original service account key is never exposed.

Usage:
    python seed_gcp_vm_catalog.py --url https://your-cmp.example.com --token <admin_jwt>

    # Or with environment variables:
    export CMP_URL=http://localhost:8001
    export CMP_TOKEN=eyJhbGciOiJIUzI1NiIs...
    python seed_gcp_vm_catalog.py
"""

import argparse
import json
import os
import sys
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

GCP_COMPUTE_HCL = r'''
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
  zone    = var.zone
}

resource "google_compute_instance" "vm" {
  name         = var.instance_name
  machine_type = var.machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = var.disk_size_gb
    }
  }

  network_interface {
    network    = var.network
    subnetwork = var.subnetwork

    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []
      content {}
    }
  }

  labels = var.labels

  metadata = merge(
    {
      "enable-oslogin" = "TRUE"  # Linux only — ignored on Windows images
    },
    var.startup_script != "" ? { "startup-script" = var.startup_script } : {},
    var.windows_startup_script != "" ? { "windows-startup-script-ps1" = var.windows_startup_script } : {}
  )
}

output "instance_id" {
  value       = google_compute_instance.vm.instance_id
  description = "The instance ID"
}

output "instance_name" {
  value       = google_compute_instance.vm.name
  description = "The instance name"
}

output "internal_ip" {
  value       = google_compute_instance.vm.network_interface[0].network_ip
  description = "Internal IP address"
}

output "external_ip" {
  value       = var.assign_public_ip ? google_compute_instance.vm.network_interface[0].access_config[0].nat_ip : "none"
  description = "External IP address"
}

output "self_link" {
  value       = google_compute_instance.vm.self_link
  description = "Self link URL"
}

output "zone" {
  value       = google_compute_instance.vm.zone
  description = "Zone where the VM was created"
}
'''

NATIVE_TASK_CODE = r'''"""
GCP VM Provisioning — Native Python Task

Provisions a GCP Compute Engine VM using the Google Cloud Python SDK
with a short-lived OAuth2 access token. The original service account
key is never visible to this task.

CMP injects context as:
  cmp["credential"]["temp_access_token"]  — 1-hour OAuth2 token
  cmp["credential"]["project_id"]         — GCP project ID (from credential_ctx)
  cmp["user_data"]                        — ready-to-use startup script, format
                                            auto-selected by CMP based on provider:
                                            GCP → plain #!/bin/bash script
                                            AWS/Azure → #cloud-config YAML
                                            Always use cmp["user_data"] regardless
                                            of cloud — no provider-specific key needed.
  params["instance_name"]                 — form data / step inputs

The cmp["user_data"] bash script is passed as the instance's startup-script
metadata. GCP's guest agent executes it on first boot, installing the CMP
agent automatically — no SSH or manual steps required.
"""
import json
import sys
import time
import os

try:
    import google.oauth2.credentials
    from googleapiclient import discovery
    from googleapiclient.errors import HttpError
except ImportError:
    print("ERROR: google-api-python-client and google-auth are required")
    print("Install: pip install google-api-python-client google-auth")
    sys.exit(1)


def main():
    # Access CMP-injected context
    # cmp dict is injected by the task runner wrapper at the top of this file
    credential = cmp.get("credential") or {}

    token = credential.get("temp_access_token", "")
    project_id = params.get("GCP_PROJECT_ID") or credential.get("project_id", "")

    # Read inputs from params (merged form_data + step inputs)
    zone = params.get("GCP_ZONE") or params.get("zone", "us-central1-a")
    instance_name = params.get("INSTANCE_NAME") or params.get("instance_name", "")
    machine_type = params.get("MACHINE_TYPE") or params.get("machine_type", "e2-medium")
    boot_image = params.get("BOOT_IMAGE") or params.get("boot_image", "debian-cloud/debian-12")
    disk_size_gb = int(params.get("DISK_SIZE_GB") or params.get("disk_size_gb", "20"))
    network = params.get("NETWORK") or params.get("network", "default")
    subnetwork = params.get("SUBNETWORK") or params.get("subnetwork", "default")
    assign_public_ip = str(params.get("ASSIGN_PUBLIC_IP", params.get("assign_public_ip", "true"))).lower() in ("true", "1", "yes")

    # CMP provides a ready-to-use startup script via cmp["user_data"].
    # The format is automatically correct for the cloud provider:
    #   GCP         → plain #!/bin/bash script (GCP guest agent runs startup-script directly)
    #   AWS / Azure → #cloud-config YAML (both run cloud-init)
    # Task authors always use cmp["user_data"] — no provider-specific key needed.
    user_data = cmp.get("user_data", "")
    if user_data:
        print(f"[CMP] user_data provided ({len(user_data)} bytes) — will be set as startup-script")
    else:
        print("[CMP] WARNING: cmp['user_data'] is empty. Check admin Settings → Provisioning tab.")
        # Fallback: build a minimal startup script from cmp["agent"] if available
        agent = cmp.get("agent", {})
        if agent.get("token"):
            print("[CMP] Falling back to cmp['agent'] for startup-script")
            tenant_id = cmp.get("execution", {}).get("tenant_id", "default")
            user_data = (
                "#!/bin/bash\n"
                "# CMP Agent install — fallback (cmp['user_data'] was empty)\n"
                "sleep 10\n"
                "# GCP: use numeric instance ID — matches how CMP stores GCP resources (uses instance.id not instance.name)\n"
                'CMP_RESOURCE_ID=$(curl -s --connect-timeout 5 --retry 3 -H "Metadata-Flavor: Google" \\\n'
                '  "http://metadata.google.internal/computeMetadata/v1/instance/id" 2>/dev/null || true)\n'
                f'if [ -z "$CMP_RESOURCE_ID" ]; then CMP_RESOURCE_ID="{instance_name}"; fi\n'
                f'curl -sSL "{agent["install_url"]}" | bash -s -- \\\n'
                f'  --endpoint "{agent["endpoint"]}" \\\n'
                f'  --token "{agent["token"]}" \\\n'
                '  --resource-id "$CMP_RESOURCE_ID" \\\n'
                f'  --tenant-id "{tenant_id}"\n'
            )

    if not token:
        print("ERROR: No temp_access_token in credential context. GCP credential may not have generated a temp token.")
        print(f"  Available credential keys: {list(credential.keys())}")
        sys.exit(1)
    if not project_id:
        print("ERROR: No project_id available in credential context or params.")
        sys.exit(1)
    if not instance_name:
        print("ERROR: instance_name not provided in form data.")
        sys.exit(1)

    print(f"[GCP] Provisioning VM '{instance_name}' in {project_id}/{zone}")
    print(f"[GCP] Machine type: {machine_type}, Image: {boot_image}, Disk: {disk_size_gb}GB")
    if user_data:
        print("[GCP] CMP startup-script will be applied on first boot (SSH keys + agent)")

    # Authenticate with short-lived token
    creds = google.oauth2.credentials.Credentials(token=token)
    compute = discovery.build("compute", "v1", credentials=creds)

    # Build instance configuration
    image_parts = boot_image.split("/")
    if len(image_parts) == 2:
        source_image = f"projects/{image_parts[0]}/global/images/family/{image_parts[1]}"
    else:
        source_image = f"projects/debian-cloud/global/images/family/{boot_image}"

    # Detect Windows image — GCP Windows images use a different metadata key
    is_windows_image = "windows" in boot_image.lower()

    # Build metadata items
    metadata_items = [
        {"key": "enable-oslogin", "value": "TRUE"},  # Linux only — safe to set, ignored on Windows
    ]

    if is_windows_image:
        # GCP Windows: windows-startup-script-ps1 is executed by the GCE Windows agent
        win_data = cmp.get("user_data_windows", "")
        if win_data:
            metadata_items.append({"key": "windows-startup-script-ps1", "value": win_data})
            print(f"[CMP] user_data_windows provided ({len(win_data)} bytes) — set as windows-startup-script-ps1")
        else:
            print("[CMP] WARNING: cmp['user_data_windows'] is empty for Windows image.")
    else:
        # GCP Linux: startup-script is executed directly as a shell script
        # by the Google Cloud guest agent on every boot.
        if user_data:
            metadata_items.append({"key": "startup-script", "value": user_data})

    instance_body = {
        "name": instance_name,
        "machineType": f"zones/{zone}/machineTypes/{machine_type}",
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": source_image,
                    "diskSizeGb": str(disk_size_gb),
                    "diskType": f"zones/{zone}/diskTypes/pd-balanced",
                },
            }
        ],
        "networkInterfaces": [
            {
                "network": f"global/networks/{network}",
            }
        ],
        "metadata": {
            "items": metadata_items,
        },
        "labels": {
            "managed-by": "cmp",
            "provisioned-via": "native-task",
        },
    }

    # Add subnetwork if not default
    if subnetwork and subnetwork != "default":
        region = zone.rsplit("-", 1)[0]
        instance_body["networkInterfaces"][0]["subnetwork"] = f"regions/{region}/subnetworks/{subnetwork}"

    # Add external IP if requested
    if assign_public_ip:
        instance_body["networkInterfaces"][0]["accessConfigs"] = [
            {"type": "ONE_TO_ONE_NAT", "name": "External NAT", "networkTier": "PREMIUM"}
        ]

    try:
        print(f"[GCP] Sending instances.insert request...")
        operation = compute.instances().insert(
            project=project_id, zone=zone, body=instance_body
        ).execute()

        op_name = operation["name"]
        print(f"[GCP] Operation started: {op_name}")

        # Poll for completion
        max_wait = 120  # seconds
        start = time.time()
        while time.time() - start < max_wait:
            result = compute.zoneOperations().get(
                project=project_id, zone=zone, operation=op_name
            ).execute()

            status = result.get("status")
            print(f"[GCP] Operation status: {status}")

            if status == "DONE":
                if "error" in result:
                    errors = result["error"].get("errors", [])
                    error_msg = "; ".join(e.get("message", "") for e in errors)
                    print(f"ERROR: VM creation failed: {error_msg}")
                    sys.exit(1)
                break
            time.sleep(5)
        else:
            print(f"WARNING: Operation timed out after {max_wait}s. VM may still be provisioning.")

        # Fetch instance details
        instance = compute.instances().get(
            project=project_id, zone=zone, instance=instance_name
        ).execute()

        internal_ip = instance["networkInterfaces"][0].get("networkIP", "N/A")
        external_ip = "N/A"
        access_configs = instance["networkInterfaces"][0].get("accessConfigs", [])
        if access_configs:
            external_ip = access_configs[0].get("natIP", "N/A")

        # Output results as JSON (captured by CMP orchestrator)
        output = {
            "status": "success",
            "instance_id": instance.get("id"),
            "instance_name": instance_name,
            "zone": zone,
            "machine_type": machine_type,
            "internal_ip": internal_ip,
            "external_ip": external_ip,
            "self_link": instance.get("selfLink"),
            "status_vm": instance.get("status"),
        }
        print(json.dumps(output))

    except HttpError as e:
        error_details = json.loads(e.content.decode())
        msg = error_details.get("error", {}).get("message", str(e))
        print(f"ERROR: GCP API error: {msg}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error: {e}")
        sys.exit(1)


main()
'''

# ─────────────────────────────────────────────────────────────────────────────
# Form Schema (shared between both catalog items)
# ─────────────────────────────────────────────────────────────────────────────

GCP_VM_FORM_FIELDS = [
    {
        "field_id": "instance_name",
        "label": "Instance Name",
        "type": "string",
        "required": True,
        "placeholder": "my-gcp-vm-01",
        "description": "Name for the VM instance (lowercase, hyphens allowed, max 63 chars)",
        "validation": {
            "pattern": "^[a-z]([-a-z0-9]*[a-z0-9])?$",
            "maxLength": 63,
        },
    },
    {
        "field_id": "machine_type",
        "label": "Machine Type",
        "type": "select",
        "required": True,
        "default": "e2-medium",
        "options": [
            {"label": "e2-micro (0.25 vCPU, 1 GB) — Free tier eligible", "value": "e2-micro"},
            {"label": "e2-small (0.5 vCPU, 2 GB)", "value": "e2-small"},
            {"label": "e2-medium (1 vCPU, 4 GB)", "value": "e2-medium"},
            {"label": "e2-standard-2 (2 vCPU, 8 GB)", "value": "e2-standard-2"},
            {"label": "e2-standard-4 (4 vCPU, 16 GB)", "value": "e2-standard-4"},
            {"label": "e2-standard-8 (8 vCPU, 32 GB)", "value": "e2-standard-8"},
            {"label": "n2-standard-2 (2 vCPU, 8 GB)", "value": "n2-standard-2"},
            {"label": "n2-standard-4 (4 vCPU, 16 GB)", "value": "n2-standard-4"},
            {"label": "n2-standard-8 (8 vCPU, 32 GB)", "value": "n2-standard-8"},
            {"label": "c2-standard-4 (4 vCPU, 16 GB) — Compute optimized", "value": "c2-standard-4"},
        ],
        "description": "GCP machine type determining CPU and memory allocation",
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
            {"label": "us-east4 (Virginia)", "value": "us-east4"},
            {"label": "us-west1 (Oregon)", "value": "us-west1"},
            {"label": "europe-west1 (Belgium)", "value": "europe-west1"},
            {"label": "europe-west2 (London)", "value": "europe-west2"},
            {"label": "europe-west3 (Frankfurt)", "value": "europe-west3"},
            {"label": "asia-south1 (Mumbai)", "value": "asia-south1"},
            {"label": "asia-east1 (Taiwan)", "value": "asia-east1"},
            {"label": "asia-northeast1 (Tokyo)", "value": "asia-northeast1"},
            {"label": "australia-southeast1 (Sydney)", "value": "australia-southeast1"},
        ],
        "description": "GCP region where the VM will be deployed",
    },
    {
        "field_id": "zone",
        "label": "Zone",
        "type": "select",
        "required": True,
        "default": "us-central1-a",
        "options": [
            {"label": "us-central1-a", "value": "us-central1-a"},
            {"label": "us-central1-b", "value": "us-central1-b"},
            {"label": "us-central1-c", "value": "us-central1-c"},
            {"label": "us-east1-b", "value": "us-east1-b"},
            {"label": "us-east1-c", "value": "us-east1-c"},
            {"label": "us-east4-a", "value": "us-east4-a"},
            {"label": "us-west1-a", "value": "us-west1-a"},
            {"label": "us-west1-b", "value": "us-west1-b"},
            {"label": "europe-west1-b", "value": "europe-west1-b"},
            {"label": "europe-west2-a", "value": "europe-west2-a"},
            {"label": "europe-west3-a", "value": "europe-west3-a"},
            {"label": "asia-south1-a", "value": "asia-south1-a"},
            {"label": "asia-east1-a", "value": "asia-east1-a"},
            {"label": "asia-northeast1-a", "value": "asia-northeast1-a"},
            {"label": "australia-southeast1-a", "value": "australia-southeast1-a"},
        ],
        "description": "Availability zone within the selected region",
    },
    {
        "field_id": "boot_image",
        "label": "Boot Image (OS)",
        "type": "select",
        "required": True,
        "default": "debian-cloud/debian-12",
        "options": [
            {"label": "Debian 12 (Bookworm)", "value": "debian-cloud/debian-12"},
            {"label": "Debian 11 (Bullseye)", "value": "debian-cloud/debian-11"},
            {"label": "Ubuntu 22.04 LTS", "value": "ubuntu-os-cloud/ubuntu-2204-lts"},
            {"label": "Ubuntu 24.04 LTS", "value": "ubuntu-os-cloud/ubuntu-2404-lts-amd64"},
            {"label": "CentOS Stream 9", "value": "centos-cloud/centos-stream-9"},
            {"label": "Rocky Linux 9", "value": "rocky-linux-cloud/rocky-linux-9"},
            {"label": "RHEL 9", "value": "rhel-cloud/rhel-9"},
            {"label": "Windows Server 2022", "value": "windows-cloud/windows-2022"},
            {"label": "Container-Optimized OS (COS)", "value": "cos-cloud/cos-stable"},
        ],
        "description": "Operating system image for the boot disk",
    },
    {
        "field_id": "disk_size_gb",
        "label": "Boot Disk Size (GB)",
        "type": "number",
        "required": True,
        "default": 20,
        "validation": {"min": 10, "max": 2048},
        "description": "Boot disk size in gigabytes (min 10, max 2048)",
    },
    {
        "field_id": "network",
        "label": "VPC Network",
        "type": "string",
        "required": True,
        "default": "default",
        "placeholder": "default",
        "description": "VPC network name (use 'default' for the default VPC)",
    },
    {
        "field_id": "subnetwork",
        "label": "Subnetwork",
        "type": "string",
        "required": False,
        "default": "default",
        "placeholder": "default",
        "description": "Subnetwork within the VPC (leave as 'default' to auto-select)",
    },
    {
        "field_id": "assign_public_ip",
        "label": "Assign Public IP",
        "type": "boolean",
        "required": False,
        "default": True,
        "description": "Assign an external IP for internet access",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# API Client
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
# Seed Functions
# ─────────────────────────────────────────────────────────────────────────────


def create_terraform_template(client: CMPClient) -> str:
    """Create or update the GCP Compute Engine Terraform template."""
    print("\n[1/7] Terraform template: GCP Compute Instance...")
    name = "GCP Compute Instance"
    payload = {
        "name": name,
        "description": (
            "Provisions a GCP Compute Engine VM with configurable machine type, "
            "boot image, zone, network, and public IP. Uses google provider ~> 5.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": GCP_COMPUTE_HCL},
        "input_variables": [
            {"name": "project_id", "type": "string", "description": "GCP Project ID", "required": True},
            {"name": "region", "type": "string", "description": "GCP region", "default": "us-central1", "required": True},
            {"name": "zone", "type": "string", "description": "GCP zone", "default": "us-central1-a", "required": True},
            {"name": "instance_name", "type": "string", "description": "VM instance name", "required": True},
            {"name": "machine_type", "type": "string", "description": "Machine type", "default": "e2-medium", "required": True},
            {"name": "boot_image", "type": "string", "description": "Boot disk image", "default": "debian-cloud/debian-12", "required": True},
            {"name": "disk_size_gb", "type": "number", "description": "Boot disk size (GB)", "default": 20, "required": True},
            {"name": "network", "type": "string", "description": "VPC network", "default": "default", "required": True},
            {"name": "subnetwork", "type": "string", "description": "Subnetwork", "default": "default"},
            {"name": "assign_public_ip", "type": "bool", "description": "Assign external IP", "default": True},
            {"name": "labels", "type": "map", "description": "Labels to apply", "default": {}},
            {"name": "startup_script", "type": "string", "description": "Plain bash startup-script for Linux (SSH keys + CMP agent). Injected automatically via cmp[\"user_data\"].", "default": ""},
            {"name": "windows_startup_script", "type": "string", "description": "PowerShell startup script for Windows (CMP agent). Injected automatically via cmp[\"user_data_windows\"].", "default": ""},
        ],
        "output_definitions": [
            {"name": "instance_id", "description": "Instance ID"},
            {"name": "instance_name", "description": "Instance name"},
            {"name": "internal_ip", "description": "Internal IP address"},
            {"name": "external_ip", "description": "External IP address"},
            {"name": "self_link", "description": "Self link URL"},
            {"name": "zone", "description": "Zone"},
        ],
        "required_providers": {"google": "~> 5.0"},
        "supported_providers": ["gcp"],
        "tags": ["gcp", "compute", "vm", "day1", "seed"],
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


def create_native_task(client: CMPClient) -> str:
    """Create or update the native Python GCP VM provisioning task."""
    print("\n[2/7] Native Python task: GCP VM Provision (Native SDK)...")
    name = "GCP VM Provision (Native SDK)"
    payload = {
        "name": name,
        "description": (
            "Provisions a GCP Compute Engine VM using the Google Cloud Python SDK "
            "with a short-lived OAuth2 access token. Never accesses the raw service account key."
        ),
        "language": "python",
        "code": NATIVE_TASK_CODE,
        "requirements": "google-api-python-client>=2.100.0\ngoogle-auth>=2.20.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_name": {"type": "string", "description": "VM name"},
                "machine_type": {"type": "string", "description": "Machine type"},
                "zone": {"type": "string", "description": "Zone"},
                "boot_image": {"type": "string", "description": "Boot image"},
                "disk_size_gb": {"type": "integer", "description": "Disk size GB"},
                "network": {"type": "string", "description": "VPC network"},
                "subnetwork": {"type": "string", "description": "Subnetwork"},
                "assign_public_ip": {"type": "boolean", "description": "Assign public IP"},
            },
            "required": ["instance_name", "machine_type", "zone"],
        },
        "tags": ["gcp", "compute", "vm", "native", "sdk", "seed"],
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


def create_terraform_workflow(client: CMPClient, template_id: str) -> str:
    """Create or update a workflow that uses Terraform to provision the GCP VM."""
    print("\n[3/7] Terraform workflow...")
    name = "gcp-vm-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions a GCP Compute Engine VM using Terraform with the google provider.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — GCP VM",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "project_id": "{{credential.project_id}}",
                    "region": "{{form.region}}",
                    "zone": "{{form.zone}}",
                    "instance_name": "{{form.instance_name}}",
                    "machine_type": "{{form.machine_type}}",
                    "boot_image": "{{form.boot_image}}",
                    "disk_size_gb": "{{form.disk_size_gb}}",
                    "network": "{{form.network}}",
                    "subnetwork": "{{form.subnetwork}}",
                    "assign_public_ip": "{{form.assign_public_ip}}",
                    "startup_script": "{{cmp_user_data}}",
                    "windows_startup_script": "{{cmp_user_data_windows}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 900,
            }
        ],
        "tags": ["gcp", "compute", "terraform", "seed"],
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


def create_native_workflow(client: CMPClient, task_id: str) -> str:
    """Create or update a workflow that uses the native Python task to provision a GCP VM."""
    print("\n[4/7] Native SDK workflow...")
    name = "gcp-vm-native-provision"
    payload = {
        "name": name,
        "description": "Provisions a GCP Compute Engine VM using the native Python SDK with a short-lived temp token.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Provision GCP VM (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "instance_name": "{{form.instance_name}}",
                    "machine_type": "{{form.machine_type}}",
                    "zone": "{{form.zone}}",
                    "boot_image": "{{form.boot_image}}",
                    "disk_size_gb": "{{form.disk_size_gb}}",
                    "network": "{{form.network}}",
                    "subnetwork": "{{form.subnetwork}}",
                    "assign_public_ip": "{{form.assign_public_ip}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 300,
            }
        ],
        "tags": ["gcp", "compute", "native", "sdk", "seed"],
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
) -> str:
    """Create or update a published Day-1 catalog item for GCP VM provisioning."""
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
        "form_schema": {"fields": GCP_VM_FORM_FIELDS},
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
        description="Seed GCP VM provisioning catalogs (Terraform + Native) into CMP"
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
    print("  GCP VM Provisioning — CMP Seed Script")
    print("=" * 70)
    print(f"  Target: {args.url}")
    print(f"  Token:  {args.token[:20]}...")
    print("=" * 70)

    # ── Terraform-based provisioning ─────────────────────────────────────
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    template_id = create_terraform_template(client)
    tf_workflow_id = create_terraform_workflow(client, template_id)

    print("\n[5/7] Creating Terraform flow...")
    tf_flow_id = create_flow(
        client,
        tf_workflow_id,
        name="gcp-vm-terraform-flow",
        desc="Flow for GCP VM provisioning via Terraform",
        tags=["gcp", "compute", "terraform", "seed"],
    )

    print("\n[6/7] Creating Terraform catalog item...")
    tf_catalog_id = create_catalog(
        client,
        tf_flow_id,
        name="GCP Compute Engine VM (Terraform)",
        description=(
            "Provision a Google Cloud Compute Engine VM using Terraform. "
            "Select your GCP credential, configure VM specs (machine type, image, zone, network), "
            "and deploy infrastructure-as-code with full state management."
        ),
        tags=["gcp", "compute", "vm", "terraform", "infrastructure", "seed"],
    )

    # ── Native SDK-based provisioning ────────────────────────────────────
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + google-api-python-client)")
    print("─" * 70)

    task_id = create_native_task(client)
    native_workflow_id = create_native_workflow(client, task_id)

    print("\n[5/7] Creating native flow...")
    native_flow_id = create_flow(
        client,
        native_workflow_id,
        name="gcp-vm-native-flow",
        desc="Flow for GCP VM provisioning via native Python SDK with temp OAuth2 token",
        tags=["gcp", "compute", "native", "sdk", "seed"],
    )

    print("\n[7/7] Creating native catalog item...")
    native_catalog_id = create_catalog(
        client,
        native_flow_id,
        name="GCP Compute Engine VM (Native SDK)",
        description=(
            "Provision a Google Cloud Compute Engine VM using the native GCP Python SDK. "
            "Uses a short-lived OAuth2 access token (1hr) from the selected credential. "
            "The service account key is never exposed to the provisioning task."
        ),
        tags=["gcp", "compute", "vm", "native", "sdk", "seed"],
    )

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  DONE! Summary of created resources:")
    print("=" * 70)
    print(f"""
  TERRAFORM APPROACH:
    Template ID:  {template_id}
    Workflow ID:  {tf_workflow_id}
    Flow ID:      {tf_flow_id}
    Catalog ID:   {tf_catalog_id}

  NATIVE SDK APPROACH:
    Task ID:      {task_id}
    Workflow ID:  {native_workflow_id}
    Flow ID:      {native_flow_id}
    Catalog ID:   {native_catalog_id}

  HOW TO TEST:
    1. Navigate to the CMP Catalog page
    2. Select "GCP Compute Engine VM (Terraform)" or "(Native SDK)"
    3. Choose your onboarded GCP credential
    4. Fill in VM configuration and submit
    5. Monitor execution in the Executions page

  CREDENTIAL SECURITY:
    - Terraform: SA JSON → temp file → GOOGLE_APPLICATION_CREDENTIALS → deleted after execution
    - Native:    SA JSON → OAuth2 token (1hr) → passed as GCP_ACCESS_TOKEN env var
    - In BOTH cases, the raw service account key is never visible to the user or task code
""")


if __name__ == "__main__":
    main()
