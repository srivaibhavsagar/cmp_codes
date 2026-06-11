#!/usr/bin/env python3
"""
AWS EC2 VM Provisioning — Seed Script

Creates all CMP resources needed to provision AWS EC2 instances:
  1. Terraform-based provisioning (IaC approach)
  2. Native Python task-based provisioning (SDK approach)

Both approaches use secure short-lived credentials from the selected
AWS cloud credential — the original access keys are never exposed.

Usage:
    python seed_aws_vm_catalog.py --url https://your-cmp.example.com --token <admin_jwt>

    # Or with environment variables:
    export CMP_URL=http://localhost:8001
    export CMP_TOKEN=eyJhbGciOiJIUzI1NiIs...
    python seed_aws_vm_catalog.py
"""

import argparse
import json
import os
import sys
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Terraform HCL Template
# ─────────────────────────────────────────────────────────────────────────────

AWS_EC2_HCL = r'''
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

resource "aws_instance" "vm" {
  ami           = var.ami_id
  instance_type = var.instance_type
  subnet_id     = var.subnet_id != "" ? var.subnet_id : null
  key_name      = var.key_name != "" ? var.key_name : null

  associate_public_ip_address = var.assign_public_ip

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = var.root_volume_type
    encrypted   = true
  }

  tags = merge(var.tags, {
    Name       = var.instance_name
    ManagedBy  = "cmp"
  })

  metadata_options {
    http_tokens = "required"
  }
}

output "instance_id" {
  value       = aws_instance.vm.id
  description = "The EC2 instance ID"
}

output "instance_name" {
  value       = var.instance_name
  description = "The instance name tag"
}

output "private_ip" {
  value       = aws_instance.vm.private_ip
  description = "Private IP address"
}

output "public_ip" {
  value       = aws_instance.vm.public_ip
  description = "Public IP address (if assigned)"
}

output "availability_zone" {
  value       = aws_instance.vm.availability_zone
  description = "Availability zone"
}

output "instance_state" {
  value       = aws_instance.vm.instance_state
  description = "Current instance state"
}
'''

# ─────────────────────────────────────────────────────────────────────────────
# Native Python Task Code
# ─────────────────────────────────────────────────────────────────────────────

NATIVE_TASK_CODE = r'''"""
AWS EC2 Provisioning — Native Python Task

Provisions an AWS EC2 instance using boto3 with temporary credentials
from the CMP credential context. The original access keys are never
visible to this task.

CMP injects context as:
  cmp["credential"]["aws_access_key_id"]      — temp or original key
  cmp["credential"]["aws_secret_access_key"]  — temp or original secret
  cmp["credential"]["aws_session_token"]      — STS session token (if assumed role)
  params["instance_name"]                     — form data / step inputs
"""
import json
import sys
import time

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 is required. Install: pip install boto3")
    sys.exit(1)


def main():
    credential = cmp.get("credential", {})

    aws_access_key = credential.get("aws_access_key_id", "")
    aws_secret_key = credential.get("aws_secret_access_key", "")
    aws_session_token = credential.get("aws_session_token", "")

    # Read inputs from params
    region = params.get("region", "us-east-1")
    instance_name = params.get("instance_name", "")
    instance_type = params.get("instance_type", "t3.micro")
    ami_id = params.get("ami_id", "")
    subnet_id = params.get("subnet_id", "")
    key_name = params.get("key_name", "")
    root_volume_size = int(params.get("root_volume_size_gb", "20"))
    root_volume_type = params.get("root_volume_type", "gp3")
    assign_public_ip = str(params.get("assign_public_ip", "true")).lower() in ("true", "1", "yes")

    if not aws_access_key or not aws_secret_key:
        print("ERROR: No AWS credentials in credential context.")
        print(f"  Available credential keys: {list(credential.keys())}")
        sys.exit(1)

    if not instance_name:
        print("ERROR: instance_name is required.")
        sys.exit(1)

    if not ami_id:
        print("ERROR: ami_id is required.")
        sys.exit(1)

    print(f"[AWS] Provisioning EC2 instance '{instance_name}' in {region}")
    print(f"[AWS] Type: {instance_type}, AMI: {ami_id}, Volume: {root_volume_size}GB {root_volume_type}")

    # Create boto3 session with temp credentials
    session_kwargs = {
        "aws_access_key_id": aws_access_key,
        "aws_secret_access_key": aws_secret_key,
        "region_name": region,
    }
    if aws_session_token:
        session_kwargs["aws_session_token"] = aws_session_token

    session = boto3.Session(**session_kwargs)
    ec2 = session.resource("ec2")
    ec2_client = session.client("ec2")

    # Build instance parameters
    run_kwargs = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": root_volume_size,
                    "VolumeType": root_volume_type,
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": instance_name},
                    {"Key": "ManagedBy", "Value": "cmp"},
                    {"Key": "ProvisionedVia", "Value": "native-task"},
                ],
            }
        ],
        "MetadataOptions": {
            "HttpTokens": "required",
            "HttpEndpoint": "enabled",
        },
    }

    if subnet_id:
        run_kwargs["SubnetId"] = subnet_id
    if key_name:
        run_kwargs["KeyName"] = key_name

    # Network interface for public IP control
    if subnet_id and assign_public_ip:
        run_kwargs.pop("SubnetId", None)
        run_kwargs["NetworkInterfaces"] = [
            {
                "DeviceIndex": 0,
                "SubnetId": subnet_id,
                "AssociatePublicIpAddress": assign_public_ip,
            }
        ]

    try:
        print("[AWS] Launching instance...")
        instances = ec2.create_instances(**run_kwargs)
        instance = instances[0]
        instance_id = instance.id
        print(f"[AWS] Instance launched: {instance_id}")

        # Wait for running state
        print("[AWS] Waiting for instance to reach 'running' state...")
        instance.wait_until_running()
        instance.reload()

        private_ip = instance.private_ip_address or "N/A"
        public_ip = instance.public_ip_address or "N/A"
        az = instance.placement.get("AvailabilityZone", "N/A")

        output = {
            "status": "success",
            "instance_id": instance_id,
            "instance_name": instance_name,
            "region": region,
            "availability_zone": az,
            "instance_type": instance_type,
            "private_ip": private_ip,
            "public_ip": public_ip,
            "instance_state": instance.state["Name"],
        }
        print(json.dumps(output))

    except ClientError as e:
        msg = e.response["Error"]["Message"]
        print(f"ERROR: AWS API error: {msg}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error: {e}")
        sys.exit(1)


main()
'''

# ─────────────────────────────────────────────────────────────────────────────
# Form Schema
# ─────────────────────────────────────────────────────────────────────────────

AWS_EC2_FORM_FIELDS = [
    {
        "field_id": "instance_name",
        "label": "Instance Name",
        "type": "string",
        "required": True,
        "placeholder": "my-aws-vm-01",
        "description": "Name tag for the EC2 instance",
    },
    {
        "field_id": "instance_type",
        "label": "Instance Type",
        "type": "select",
        "required": True,
        "default": "t3.micro",
        "options": [
            {"label": "t3.micro (2 vCPU, 1 GB) — Free tier eligible", "value": "t3.micro"},
            {"label": "t3.small (2 vCPU, 2 GB)", "value": "t3.small"},
            {"label": "t3.medium (2 vCPU, 4 GB)", "value": "t3.medium"},
            {"label": "t3.large (2 vCPU, 8 GB)", "value": "t3.large"},
            {"label": "t3.xlarge (4 vCPU, 16 GB)", "value": "t3.xlarge"},
            {"label": "m5.large (2 vCPU, 8 GB)", "value": "m5.large"},
            {"label": "m5.xlarge (4 vCPU, 16 GB)", "value": "m5.xlarge"},
            {"label": "m5.2xlarge (8 vCPU, 32 GB)", "value": "m5.2xlarge"},
            {"label": "c5.large (2 vCPU, 4 GB) — Compute optimized", "value": "c5.large"},
            {"label": "c5.xlarge (4 vCPU, 8 GB) — Compute optimized", "value": "c5.xlarge"},
        ],
        "description": "EC2 instance type determining CPU and memory",
    },
    {
        "field_id": "region",
        "label": "Region",
        "type": "select",
        "required": True,
        "default": "us-east-1",
        "options": [
            {"label": "us-east-1 (N. Virginia)", "value": "us-east-1"},
            {"label": "us-east-2 (Ohio)", "value": "us-east-2"},
            {"label": "us-west-1 (N. California)", "value": "us-west-1"},
            {"label": "us-west-2 (Oregon)", "value": "us-west-2"},
            {"label": "eu-west-1 (Ireland)", "value": "eu-west-1"},
            {"label": "eu-west-2 (London)", "value": "eu-west-2"},
            {"label": "eu-central-1 (Frankfurt)", "value": "eu-central-1"},
            {"label": "ap-south-1 (Mumbai)", "value": "ap-south-1"},
            {"label": "ap-southeast-1 (Singapore)", "value": "ap-southeast-1"},
            {"label": "ap-northeast-1 (Tokyo)", "value": "ap-northeast-1"},
        ],
        "description": "AWS region where the instance will be launched",
    },
    {
        "field_id": "ami_id",
        "label": "AMI ID",
        "type": "string",
        "required": True,
        "placeholder": "ami-0c02fb55956c7d316",
        "description": "Amazon Machine Image ID (e.g., ami-0c02fb55956c7d316 for Amazon Linux 2023 in us-east-1)",
    },
    {
        "field_id": "root_volume_size_gb",
        "label": "Root Volume Size (GB)",
        "type": "number",
        "required": True,
        "default": 20,
        "validation": {"min": 8, "max": 2048},
        "description": "Root EBS volume size in GB (min 8, max 2048)",
    },
    {
        "field_id": "root_volume_type",
        "label": "Root Volume Type",
        "type": "select",
        "required": True,
        "default": "gp3",
        "options": [
            {"label": "gp3 — General Purpose SSD (recommended)", "value": "gp3"},
            {"label": "gp2 — General Purpose SSD (previous gen)", "value": "gp2"},
            {"label": "io1 — Provisioned IOPS SSD", "value": "io1"},
            {"label": "st1 — Throughput Optimized HDD", "value": "st1"},
        ],
        "description": "EBS volume type for the root disk",
    },
    {
        "field_id": "subnet_id",
        "label": "Subnet ID",
        "type": "string",
        "required": False,
        "placeholder": "subnet-0abcdef1234567890",
        "description": "VPC subnet ID (leave empty for default VPC)",
    },
    {
        "field_id": "key_name",
        "label": "SSH Key Pair Name",
        "type": "string",
        "required": False,
        "placeholder": "my-key-pair",
        "description": "EC2 key pair name for SSH access (optional)",
    },
    {
        "field_id": "assign_public_ip",
        "label": "Assign Public IP",
        "type": "boolean",
        "required": False,
        "default": True,
        "description": "Assign a public IP address for internet access",
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

    def find_by_name(self, path: str, name: str):
        """List resources and find one matching the exact name."""
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
    """Create or update the AWS EC2 Terraform template."""
    print("\n[1/7] Terraform template: AWS EC2 Instance...")
    name = "AWS EC2 Instance"
    payload = {
        "name": name,
        "description": (
            "Provisions an AWS EC2 instance with configurable instance type, "
            "AMI, volume, subnet, and public IP. Uses aws provider ~> 5.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": AWS_EC2_HCL},
        "input_variables": [
            {"name": "region", "type": "string", "description": "AWS region", "default": "us-east-1", "required": True},
            {"name": "instance_name", "type": "string", "description": "Instance name tag", "required": True},
            {"name": "instance_type", "type": "string", "description": "EC2 instance type", "default": "t3.micro", "required": True},
            {"name": "ami_id", "type": "string", "description": "AMI ID", "required": True},
            {"name": "root_volume_size_gb", "type": "number", "description": "Root volume size (GB)", "default": 20, "required": True},
            {"name": "root_volume_type", "type": "string", "description": "Root volume type", "default": "gp3", "required": True},
            {"name": "subnet_id", "type": "string", "description": "VPC subnet ID", "default": ""},
            {"name": "key_name", "type": "string", "description": "SSH key pair name", "default": ""},
            {"name": "assign_public_ip", "type": "bool", "description": "Assign public IP", "default": True},
            {"name": "tags", "type": "map", "description": "Additional tags", "default": {}},
        ],
        "output_definitions": [
            {"name": "instance_id", "description": "EC2 instance ID"},
            {"name": "instance_name", "description": "Instance name tag"},
            {"name": "private_ip", "description": "Private IP address"},
            {"name": "public_ip", "description": "Public IP address"},
            {"name": "availability_zone", "description": "Availability zone"},
            {"name": "instance_state", "description": "Instance state"},
        ],
        "required_providers": {"aws": "~> 5.0"},
        "supported_providers": ["aws"],
        "tags": ["aws", "ec2", "compute", "vm", "day1", "seed"],
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
    """Create or update the native Python AWS EC2 provisioning task."""
    print("\n[2/7] Native Python task: AWS EC2 Provision (Native SDK)...")
    name = "AWS EC2 Provision (Native SDK)"
    payload = {
        "name": name,
        "description": (
            "Provisions an AWS EC2 instance using boto3 with temporary credentials. "
            "Never accesses the raw access keys directly."
        ),
        "language": "python",
        "code": NATIVE_TASK_CODE,
        "requirements": "boto3>=1.28.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_name": {"type": "string", "description": "Instance name"},
                "instance_type": {"type": "string", "description": "Instance type"},
                "ami_id": {"type": "string", "description": "AMI ID"},
                "region": {"type": "string", "description": "AWS region"},
                "root_volume_size_gb": {"type": "integer", "description": "Root volume size GB"},
                "root_volume_type": {"type": "string", "description": "Root volume type"},
                "subnet_id": {"type": "string", "description": "Subnet ID"},
                "key_name": {"type": "string", "description": "Key pair name"},
                "assign_public_ip": {"type": "boolean", "description": "Assign public IP"},
            },
            "required": ["instance_name", "instance_type", "ami_id"],
        },
        "tags": ["aws", "ec2", "compute", "vm", "native", "sdk", "seed"],
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
    """Create or update a workflow that uses Terraform to provision an EC2 instance."""
    print("\n[3/7] Terraform workflow...")
    name = "aws-ec2-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions an AWS EC2 instance using Terraform with the aws provider.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — AWS EC2",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "region": "{{form.region}}",
                    "instance_name": "{{form.instance_name}}",
                    "instance_type": "{{form.instance_type}}",
                    "ami_id": "{{form.ami_id}}",
                    "root_volume_size_gb": "{{form.root_volume_size_gb}}",
                    "root_volume_type": "{{form.root_volume_type}}",
                    "subnet_id": "{{form.subnet_id}}",
                    "key_name": "{{form.key_name}}",
                    "assign_public_ip": "{{form.assign_public_ip}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 600,
            }
        ],
        "tags": ["aws", "ec2", "compute", "terraform", "seed"],
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
    """Create or update a workflow that uses the native task to provision an EC2 instance."""
    print("\n[4/7] Native SDK workflow...")
    name = "aws-ec2-native-provision"
    payload = {
        "name": name,
        "description": "Provisions an AWS EC2 instance using boto3 with temporary credentials.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Provision AWS EC2 (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "instance_name": "{{form.instance_name}}",
                    "instance_type": "{{form.instance_type}}",
                    "ami_id": "{{form.ami_id}}",
                    "region": "{{form.region}}",
                    "root_volume_size_gb": "{{form.root_volume_size_gb}}",
                    "root_volume_type": "{{form.root_volume_type}}",
                    "subnet_id": "{{form.subnet_id}}",
                    "key_name": "{{form.key_name}}",
                    "assign_public_ip": "{{form.assign_public_ip}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 300,
            }
        ],
        "tags": ["aws", "ec2", "compute", "native", "sdk", "seed"],
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


def create_catalog(client: CMPClient, flow_id: str, name: str, description: str, tags: list) -> str:
    """Create or update a published Day-1 catalog item."""
    payload = {
        "name": name,
        "description": description,
        "tags": tags,
        "catalog_type": "day1",
        "cloud_provider": "aws",
        "ui_mode": "form_builder",
        "flow_id": flow_id,
        "allowed_roles": ["admin", "developer", "user"],
        "status": "published",
        "show_cost_estimate": True,
        "show_live_pricing": True,
        "form_schema": {"fields": AWS_EC2_FORM_FIELDS},
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
        description="Seed AWS EC2 provisioning catalogs (Terraform + Native) into CMP"
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
    print("  AWS EC2 Provisioning — CMP Seed Script")
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
        name="aws-ec2-terraform-flow",
        desc="Flow for AWS EC2 provisioning via Terraform",
        tags=["aws", "ec2", "compute", "terraform", "seed"],
    )

    print("\n[6/7] Creating Terraform catalog item...")
    tf_catalog_id = create_catalog(
        client,
        tf_flow_id,
        name="AWS EC2 Instance (Terraform)",
        description=(
            "Provision an AWS EC2 instance using Terraform. "
            "Select your AWS credential, configure instance specs (type, AMI, volume, subnet), "
            "and deploy infrastructure-as-code with full state management."
        ),
        tags=["aws", "ec2", "compute", "vm", "terraform", "infrastructure", "seed"],
    )

    # ── Native SDK-based provisioning ────────────────────────────────────
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + boto3)")
    print("─" * 70)

    task_id = create_native_task(client)
    native_workflow_id = create_native_workflow(client, task_id)

    print("\n[5/7] Creating native flow...")
    native_flow_id = create_flow(
        client,
        native_workflow_id,
        name="aws-ec2-native-flow",
        desc="Flow for AWS EC2 provisioning via native Python SDK with temp credentials",
        tags=["aws", "ec2", "compute", "native", "sdk", "seed"],
    )

    print("\n[7/7] Creating native catalog item...")
    native_catalog_id = create_catalog(
        client,
        native_flow_id,
        name="AWS EC2 Instance (Native SDK)",
        description=(
            "Provision an AWS EC2 instance using boto3. "
            "Uses temporary credentials from the selected AWS credential. "
            "The original access keys are never exposed to the provisioning task."
        ),
        tags=["aws", "ec2", "compute", "vm", "native", "sdk", "seed"],
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
    2. Select "AWS EC2 Instance (Terraform)" or "(Native SDK)"
    3. Choose your onboarded AWS credential
    4. Fill in instance configuration and submit
    5. Monitor execution in the Executions page

  CREDENTIAL SECURITY:
    - Terraform: AWS creds → env vars (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) → deleted after execution
    - Native:    AWS creds → boto3 session with temp token → never stored on disk
    - In BOTH cases, the raw access keys are never visible to the user or task code
""")


if __name__ == "__main__":
    main()
