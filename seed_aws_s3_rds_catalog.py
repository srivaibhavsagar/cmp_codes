#!/usr/bin/env python3
"""
AWS S3 Bucket & RDS Instance — Seed Script

Creates all CMP resources needed to provision:
  1. AWS S3 Bucket (Terraform + Native SDK)
  2. AWS RDS Instance (Terraform + Native SDK)

Both approaches use secure credentials from the selected
AWS cloud credential — the original access keys are never exposed.

Usage:
    python seed_aws_s3_rds_catalog.py --url https://your-cmp.example.com --token <admin_jwt>

    # Or with environment variables:
    export CMP_URL=http://localhost:8001
    export CMP_TOKEN=eyJhbGciOiJIUzI1NiIs...
    python seed_aws_s3_rds_catalog.py
"""

import argparse
import json
import os
import sys
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Terraform HCL Templates
# ─────────────────────────────────────────────────────────────────────────────

S3_BUCKET_HCL = r'''
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

resource "aws_s3_bucket" "bucket" {
  bucket = var.bucket_name

  tags = merge(var.tags, {
    Name      = var.bucket_name
    ManagedBy = "cmp"
  })
}

resource "aws_s3_bucket_versioning" "versioning" {
  bucket = aws_s3_bucket.bucket.id

  versioning_configuration {
    status = var.versioning_enabled ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "encryption" {
  bucket = aws_s3_bucket.bucket.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = var.encryption_algorithm
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "public_access" {
  bucket = aws_s3_bucket.bucket.id

  block_public_acls       = var.block_public_access
  block_public_policy     = var.block_public_access
  ignore_public_acls      = var.block_public_access
  restrict_public_buckets = var.block_public_access
}

resource "aws_s3_bucket_lifecycle_configuration" "lifecycle" {
  count  = var.lifecycle_expiration_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.bucket.id

  rule {
    id     = "auto-expire"
    status = "Enabled"

    expiration {
      days = var.lifecycle_expiration_days
    }
  }
}

output "bucket_name" {
  value       = aws_s3_bucket.bucket.id
  description = "The name of the bucket"
}

output "bucket_arn" {
  value       = aws_s3_bucket.bucket.arn
  description = "The ARN of the bucket"
}

output "bucket_region" {
  value       = aws_s3_bucket.bucket.region
  description = "The region of the bucket"
}

output "bucket_domain_name" {
  value       = aws_s3_bucket.bucket.bucket_domain_name
  description = "The domain name of the bucket"
}
'''

RDS_INSTANCE_HCL = r'''
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

resource "aws_db_instance" "rds" {
  identifier     = var.instance_identifier
  engine         = var.engine
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = var.storage_type
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.master_username
  password = var.master_password

  multi_az            = var.multi_az
  publicly_accessible = var.publicly_accessible

  backup_retention_period = var.backup_retention_days
  backup_window           = var.backup_window
  maintenance_window      = var.maintenance_window

  deletion_protection = var.deletion_protection
  skip_final_snapshot = var.skip_final_snapshot

  tags = merge(var.tags, {
    Name      = var.instance_identifier
    ManagedBy = "cmp"
  })
}

output "instance_identifier" {
  value       = aws_db_instance.rds.identifier
  description = "RDS instance identifier"
}

output "endpoint" {
  value       = aws_db_instance.rds.endpoint
  description = "Connection endpoint"
}

output "address" {
  value       = aws_db_instance.rds.address
  description = "Hostname of the RDS instance"
}

output "port" {
  value       = aws_db_instance.rds.port
  description = "Database port"
}

output "engine" {
  value       = aws_db_instance.rds.engine
  description = "Database engine"
}

output "arn" {
  value       = aws_db_instance.rds.arn
  description = "ARN of the RDS instance"
}

output "availability_zone" {
  value       = aws_db_instance.rds.availability_zone
  description = "Availability zone"
}
'''

# ─────────────────────────────────────────────────────────────────────────────
# Native Python Task Code
# ─────────────────────────────────────────────────────────────────────────────

S3_NATIVE_TASK_CODE = r'''"""
AWS S3 Bucket — Native Python Task

Creates an AWS S3 bucket using boto3 with temporary credentials
from the CMP credential context.

CMP injects context as:
  cmp["credential"]["aws_access_key_id"]
  cmp["credential"]["aws_secret_access_key"]
  cmp["credential"]["aws_session_token"]
  params["bucket_name"] — form data
"""
import json
import sys

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 is required. Install: pip install boto3")
    sys.exit(1)

credential = cmp.get("credential", {})
aws_access_key = credential.get("aws_access_key_id", "")
aws_secret_key = credential.get("aws_secret_access_key", "")
aws_session_token = credential.get("aws_session_token", "")

bucket_name = params.get("bucket_name", "")
region = params.get("region", "us-east-1")
versioning_enabled = str(params.get("versioning_enabled", "false")).lower() in ("true", "1", "yes")
encryption_algorithm = params.get("encryption_algorithm", "AES256")
block_public_access = str(params.get("block_public_access", "true")).lower() in ("true", "1", "yes")

if not aws_access_key or not aws_secret_key:
    print("ERROR: No AWS credentials in credential context.")
    sys.exit(1)

if not bucket_name:
    print("ERROR: bucket_name is required.")
    sys.exit(1)

print(f"[AWS] Creating S3 bucket '{bucket_name}' in {region}")

session_kwargs = {
    "aws_access_key_id": aws_access_key,
    "aws_secret_access_key": aws_secret_key,
    "region_name": region,
}
if aws_session_token:
    session_kwargs["aws_session_token"] = aws_session_token

session = boto3.Session(**session_kwargs)
s3 = session.client("s3")

try:
    # Create bucket
    create_kwargs = {"Bucket": bucket_name}
    if region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}

    s3.create_bucket(**create_kwargs)
    print(f"[AWS] Bucket '{bucket_name}' created.")

    # Enable encryption
    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": encryption_algorithm,
                    },
                    "BucketKeyEnabled": True,
                }
            ]
        },
    )
    print(f"[AWS] Encryption configured: {encryption_algorithm}")

    # Block public access
    if block_public_access:
        s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print("[AWS] Public access blocked.")

    # Versioning
    if versioning_enabled:
        s3.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )
        print("[AWS] Versioning enabled.")

    # Tagging
    s3.put_bucket_tagging(
        Bucket=bucket_name,
        Tagging={
            "TagSet": [
                {"Key": "ManagedBy", "Value": "cmp"},
                {"Key": "ProvisionedVia", "Value": "native-task"},
            ]
        },
    )

    output = {
        "status": "success",
        "bucket_name": bucket_name,
        "bucket_arn": f"arn:aws:s3:::{bucket_name}",
        "region": region,
        "versioning": "Enabled" if versioning_enabled else "Suspended",
        "encryption": encryption_algorithm,
        "public_access_blocked": block_public_access,
        "message": f"S3 bucket '{bucket_name}' created in {region}",
    }
    print(json.dumps(output))

except ClientError as e:
    msg = e.response["Error"]["Message"]
    print(f"ERROR: AWS S3 API error: {msg}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: Unexpected error: {e}")
    sys.exit(1)
'''

RDS_NATIVE_TASK_CODE = r'''"""
AWS RDS Instance — Native Python Task

Creates an AWS RDS database instance using boto3 with temporary credentials.

CMP injects context as:
  cmp["credential"]["aws_access_key_id"]
  cmp["credential"]["aws_secret_access_key"]
  cmp["credential"]["aws_session_token"]
  params["instance_identifier"] — form data
"""
import json
import sys
import time

try:
    import boto3
    from botocore.exceptions import ClientError, WaiterError
except ImportError:
    print("ERROR: boto3 is required. Install: pip install boto3")
    sys.exit(1)

credential = cmp.get("credential", {})
aws_access_key = credential.get("aws_access_key_id", "")
aws_secret_key = credential.get("aws_secret_access_key", "")
aws_session_token = credential.get("aws_session_token", "")

instance_identifier = params.get("instance_identifier", "")
region = params.get("region", "us-east-1")
engine = params.get("engine", "postgres")
engine_version = params.get("engine_version", "15.4")
instance_class = params.get("instance_class", "db.t3.micro")
allocated_storage = int(params.get("allocated_storage_gb", "20"))
storage_type = params.get("storage_type", "gp3")
db_name = params.get("db_name", "appdb")
master_username = params.get("master_username", "admin")
master_password = params.get("master_password", "")
multi_az = str(params.get("multi_az", "false")).lower() in ("true", "1", "yes")
publicly_accessible = str(params.get("publicly_accessible", "false")).lower() in ("true", "1", "yes")
backup_retention_days = int(params.get("backup_retention_days", "7"))

if not aws_access_key or not aws_secret_key:
    print("ERROR: No AWS credentials in credential context.")
    sys.exit(1)

if not instance_identifier:
    print("ERROR: instance_identifier is required.")
    sys.exit(1)

if not master_password:
    print("ERROR: master_password is required.")
    sys.exit(1)

print(f"[AWS] Creating RDS instance '{instance_identifier}' in {region}")
print(f"[AWS] Engine: {engine} {engine_version}, Class: {instance_class}")
print(f"[AWS] Storage: {allocated_storage}GB {storage_type}, Multi-AZ: {multi_az}")

session_kwargs = {
    "aws_access_key_id": aws_access_key,
    "aws_secret_access_key": aws_secret_key,
    "region_name": region,
}
if aws_session_token:
    session_kwargs["aws_session_token"] = aws_session_token

session = boto3.Session(**session_kwargs)
rds = session.client("rds")

try:
    print("[AWS] Sending create_db_instance request...")
    response = rds.create_db_instance(
        DBInstanceIdentifier=instance_identifier,
        Engine=engine,
        EngineVersion=engine_version,
        DBInstanceClass=instance_class,
        AllocatedStorage=allocated_storage,
        StorageType=storage_type,
        StorageEncrypted=True,
        DBName=db_name,
        MasterUsername=master_username,
        MasterUserPassword=master_password,
        MultiAZ=multi_az,
        PubliclyAccessible=publicly_accessible,
        BackupRetentionPeriod=backup_retention_days,
        DeletionProtection=True,
        Tags=[
            {"Key": "Name", "Value": instance_identifier},
            {"Key": "ManagedBy", "Value": "cmp"},
            {"Key": "ProvisionedVia", "Value": "native-task"},
        ],
    )

    db_instance = response["DBInstance"]
    print(f"[AWS] RDS instance creation initiated: {db_instance['DBInstanceIdentifier']}")
    print(f"[AWS] Status: {db_instance['DBInstanceStatus']}")

    # Wait for available (up to 15 min)
    print("[AWS] Waiting for instance to become available...")
    waiter = rds.get_waiter("db_instance_available")
    try:
        waiter.wait(
            DBInstanceIdentifier=instance_identifier,
            WaiterConfig={"Delay": 30, "MaxAttempts": 30},
        )
    except WaiterError:
        print("[AWS] WARNING: Timed out waiting. Instance may still be provisioning.")

    # Fetch final details
    desc = rds.describe_db_instances(DBInstanceIdentifier=instance_identifier)
    instance = desc["DBInstances"][0]

    endpoint = instance.get("Endpoint", {})
    output = {
        "status": "success",
        "instance_identifier": instance_identifier,
        "engine": instance.get("Engine"),
        "engine_version": instance.get("EngineVersion"),
        "instance_class": instance.get("DBInstanceClass"),
        "endpoint": endpoint.get("Address", ""),
        "port": endpoint.get("Port", 0),
        "db_name": db_name,
        "master_username": master_username,
        "multi_az": instance.get("MultiAZ"),
        "availability_zone": instance.get("AvailabilityZone", ""),
        "arn": instance.get("DBInstanceArn", ""),
        "instance_status": instance.get("DBInstanceStatus"),
        "region": region,
        "message": f"RDS instance '{instance_identifier}' created in {region}",
    }
    print(json.dumps(output))

except ClientError as e:
    msg = e.response["Error"]["Message"]
    print(f"ERROR: AWS RDS API error: {msg}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: Unexpected error: {e}")
    sys.exit(1)
'''

# ─────────────────────────────────────────────────────────────────────────────
# Form Field Definitions
# ─────────────────────────────────────────────────────────────────────────────

S3_BUCKET_FORM_FIELDS = [
    {
        "field_id": "bucket_name",
        "label": "Bucket Name",
        "type": "string",
        "required": True,
        "placeholder": "my-app-data-bucket",
        "description": "Globally unique bucket name (lowercase, numbers, hyphens, 3-63 chars)",
        "validation": {
            "pattern": "^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
            "message": "3-63 chars: lowercase letters, numbers, hyphens, dots. Must start/end with letter or number.",
        },
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
        "description": "AWS region for the S3 bucket",
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
        "field_id": "encryption_algorithm",
        "label": "Encryption",
        "type": "select",
        "required": True,
        "default": "AES256",
        "options": [
            {"label": "AES-256 (SSE-S3) — Amazon managed keys", "value": "AES256"},
            {"label": "aws:kms (SSE-KMS) — AWS KMS managed keys", "value": "aws:kms"},
        ],
        "description": "Server-side encryption algorithm",
    },
    {
        "field_id": "block_public_access",
        "label": "Block Public Access",
        "type": "select",
        "required": True,
        "default": "true",
        "options": [
            {"label": "Enabled (recommended)", "value": "true"},
            {"label": "Disabled", "value": "false"},
        ],
        "description": "Block all public access to the bucket",
    },
    {
        "field_id": "lifecycle_expiration_days",
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

RDS_INSTANCE_FORM_FIELDS = [
    {
        "field_id": "instance_identifier",
        "label": "Instance Identifier",
        "type": "string",
        "required": True,
        "placeholder": "my-app-db-01",
        "description": "Unique identifier for the RDS instance (lowercase, hyphens allowed)",
        "validation": {
            "pattern": "^[a-z][a-z0-9-]{0,62}$",
            "message": "Must start with a letter, lowercase + numbers + hyphens, max 63 chars",
        },
    },
    {
        "field_id": "engine",
        "label": "Database Engine",
        "type": "select",
        "required": True,
        "default": "postgres",
        "options": [
            {"label": "PostgreSQL", "value": "postgres"},
            {"label": "MySQL", "value": "mysql"},
            {"label": "MariaDB", "value": "mariadb"},
            {"label": "SQL Server Express", "value": "sqlserver-ex"},
            {"label": "SQL Server Standard", "value": "sqlserver-se"},
        ],
        "description": "Database engine type",
    },
    {
        "field_id": "engine_version",
        "label": "Engine Version",
        "type": "select",
        "required": True,
        "default": "15.4",
        "options": [
            {"label": "PostgreSQL 15.4", "value": "15.4"},
            {"label": "PostgreSQL 14.9", "value": "14.9"},
            {"label": "PostgreSQL 13.12", "value": "13.12"},
            {"label": "MySQL 8.0.35", "value": "8.0.35"},
            {"label": "MySQL 5.7.44", "value": "5.7.44"},
            {"label": "MariaDB 10.11.6", "value": "10.11.6"},
            {"label": "SQL Server 2019 (15.00)", "value": "15.00"},
        ],
        "description": "Database engine version",
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
            {"label": "us-west-2 (Oregon)", "value": "us-west-2"},
            {"label": "eu-west-1 (Ireland)", "value": "eu-west-1"},
            {"label": "eu-central-1 (Frankfurt)", "value": "eu-central-1"},
            {"label": "ap-south-1 (Mumbai)", "value": "ap-south-1"},
            {"label": "ap-southeast-1 (Singapore)", "value": "ap-southeast-1"},
            {"label": "ap-northeast-1 (Tokyo)", "value": "ap-northeast-1"},
        ],
        "description": "AWS region for the RDS instance",
    },
    {
        "field_id": "instance_class",
        "label": "Instance Class",
        "type": "select",
        "required": True,
        "default": "db.t3.micro",
        "options": [
            {"label": "db.t3.micro (2 vCPU, 1 GB) — Free tier", "value": "db.t3.micro"},
            {"label": "db.t3.small (2 vCPU, 2 GB)", "value": "db.t3.small"},
            {"label": "db.t3.medium (2 vCPU, 4 GB)", "value": "db.t3.medium"},
            {"label": "db.m5.large (2 vCPU, 8 GB)", "value": "db.m5.large"},
            {"label": "db.m5.xlarge (4 vCPU, 16 GB)", "value": "db.m5.xlarge"},
            {"label": "db.m5.2xlarge (8 vCPU, 32 GB)", "value": "db.m5.2xlarge"},
            {"label": "db.r5.large (2 vCPU, 16 GB) — Memory optimized", "value": "db.r5.large"},
            {"label": "db.r5.xlarge (4 vCPU, 32 GB) — Memory optimized", "value": "db.r5.xlarge"},
        ],
        "description": "RDS instance class determining CPU and memory",
    },
    {
        "field_id": "allocated_storage_gb",
        "label": "Storage (GB)",
        "type": "select",
        "required": True,
        "default": "20",
        "options": [
            {"label": "20 GB", "value": "20"},
            {"label": "50 GB", "value": "50"},
            {"label": "100 GB", "value": "100"},
            {"label": "250 GB", "value": "250"},
            {"label": "500 GB", "value": "500"},
            {"label": "1000 GB (1 TB)", "value": "1000"},
        ],
        "description": "Allocated storage capacity",
    },
    {
        "field_id": "storage_type",
        "label": "Storage Type",
        "type": "select",
        "required": True,
        "default": "gp3",
        "options": [
            {"label": "gp3 — General Purpose SSD (recommended)", "value": "gp3"},
            {"label": "gp2 — General Purpose SSD (previous gen)", "value": "gp2"},
            {"label": "io1 — Provisioned IOPS SSD", "value": "io1"},
        ],
        "description": "EBS storage type for the database",
    },
    {
        "field_id": "multi_az",
        "label": "Multi-AZ Deployment",
        "type": "select",
        "required": True,
        "default": "false",
        "options": [
            {"label": "Single-AZ (lower cost)", "value": "false"},
            {"label": "Multi-AZ (high availability)", "value": "true"},
        ],
        "description": "Multi-AZ for automatic failover (recommended for production)",
    },
    {
        "field_id": "publicly_accessible",
        "label": "Publicly Accessible",
        "type": "select",
        "required": True,
        "default": "false",
        "options": [
            {"label": "No (private only)", "value": "false"},
            {"label": "Yes (accessible via public IP)", "value": "true"},
        ],
        "description": "Allow connections from outside the VPC",
    },
    {
        "field_id": "backup_retention_days",
        "label": "Backup Retention (days)",
        "type": "select",
        "required": True,
        "default": "7",
        "options": [
            {"label": "0 (no backups)", "value": "0"},
            {"label": "1 day", "value": "1"},
            {"label": "7 days (recommended)", "value": "7"},
            {"label": "14 days", "value": "14"},
            {"label": "30 days", "value": "30"},
            {"label": "35 days (maximum)", "value": "35"},
        ],
        "description": "Number of days to retain automated backups",
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
        "field_id": "master_username",
        "label": "Master Username",
        "type": "string",
        "required": True,
        "default": "admin",
        "placeholder": "admin",
        "description": "Master username for the database",
    },
    {
        "field_id": "master_password",
        "label": "Master Password",
        "type": "password",
        "required": True,
        "placeholder": "••••••••",
        "description": "Master password (min 8 chars, stored encrypted)",
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

    def find_by_name(self, path: str, name: str):
        """List resources and find one matching the exact name."""
        items = self.get(path, params={"name": name})
        if isinstance(items, list):
            for item in items:
                if item.get("name") == name:
                    return item
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Seed Functions — S3
# ─────────────────────────────────────────────────────────────────────────────


def create_s3_terraform_template(client: CMPClient) -> str:
    print("\n[S3 1/6] Terraform template: AWS S3 Bucket...")
    name = "AWS S3 Bucket"
    payload = {
        "name": name,
        "description": (
            "Provisions an AWS S3 bucket with configurable versioning, encryption, "
            "public access blocking, and lifecycle rules. Uses aws provider ~> 5.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": S3_BUCKET_HCL},
        "input_variables": [
            {"name": "region", "type": "string", "description": "AWS region", "default": "us-east-1", "required": True},
            {"name": "bucket_name", "type": "string", "description": "Globally unique bucket name", "required": True},
            {"name": "versioning_enabled", "type": "bool", "description": "Enable versioning", "default": False},
            {"name": "encryption_algorithm", "type": "string", "description": "SSE algorithm", "default": "AES256"},
            {"name": "block_public_access", "type": "bool", "description": "Block public access", "default": True},
            {"name": "lifecycle_expiration_days", "type": "number", "description": "Auto-delete after N days (0=disabled)", "default": 0},
            {"name": "tags", "type": "map", "description": "Additional tags", "default": {}},
        ],
        "output_definitions": [
            {"name": "bucket_name", "description": "Bucket name"},
            {"name": "bucket_arn", "description": "Bucket ARN"},
            {"name": "bucket_region", "description": "Bucket region"},
            {"name": "bucket_domain_name", "description": "Bucket domain name"},
        ],
        "required_providers": {"aws": "~> 5.0"},
        "supported_providers": ["aws"],
        "tags": ["aws", "s3", "storage", "bucket", "day1", "seed"],
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


def create_s3_native_task(client: CMPClient) -> str:
    print("\n[S3 2/6] Native Python task: AWS S3 Bucket (Native SDK)...")
    name = "AWS S3 Bucket (Native SDK)"
    payload = {
        "name": name,
        "description": "Creates an AWS S3 bucket using boto3 with temporary credentials.",
        "language": "python",
        "code": S3_NATIVE_TASK_CODE,
        "requirements": "boto3>=1.28.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "bucket_name": {"type": "string", "description": "Bucket name"},
                "region": {"type": "string", "description": "AWS region"},
                "versioning_enabled": {"type": "string", "description": "Enable versioning"},
                "encryption_algorithm": {"type": "string", "description": "Encryption algorithm"},
                "block_public_access": {"type": "string", "description": "Block public access"},
            },
            "required": ["bucket_name"],
        },
        "tags": ["aws", "s3", "storage", "bucket", "native", "sdk", "seed"],
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


def create_s3_terraform_workflow(client: CMPClient, template_id: str) -> str:
    print("\n[S3 3/6] Terraform workflow...")
    name = "aws-s3-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions an AWS S3 bucket using Terraform.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — S3 Bucket",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "region": "{{form.region}}",
                    "bucket_name": "{{form.bucket_name}}",
                    "versioning_enabled": "{{form.versioning_enabled}}",
                    "encryption_algorithm": "{{form.encryption_algorithm}}",
                    "block_public_access": "{{form.block_public_access}}",
                    "lifecycle_expiration_days": "{{form.lifecycle_expiration_days}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 300,
            }
        ],
        "tags": ["aws", "s3", "storage", "terraform", "seed"],
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


def create_s3_native_workflow(client: CMPClient, task_id: str) -> str:
    print("\n[S3 4/6] Native SDK workflow...")
    name = "aws-s3-native-provision"
    payload = {
        "name": name,
        "description": "Creates an AWS S3 bucket using boto3 with temp credentials.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Create S3 Bucket (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "bucket_name": "{{form.bucket_name}}",
                    "region": "{{form.region}}",
                    "versioning_enabled": "{{form.versioning_enabled}}",
                    "encryption_algorithm": "{{form.encryption_algorithm}}",
                    "block_public_access": "{{form.block_public_access}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 120,
            }
        ],
        "tags": ["aws", "s3", "storage", "native", "sdk", "seed"],
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
# Seed Functions — RDS
# ─────────────────────────────────────────────────────────────────────────────


def create_rds_terraform_template(client: CMPClient) -> str:
    print("\n[RDS 1/6] Terraform template: AWS RDS Instance...")
    name = "AWS RDS Instance"
    payload = {
        "name": name,
        "description": (
            "Provisions an AWS RDS database instance with configurable engine, "
            "instance class, storage, Multi-AZ, backups, and initial database/user. "
            "Uses aws provider ~> 5.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": RDS_INSTANCE_HCL},
        "input_variables": [
            {"name": "region", "type": "string", "description": "AWS region", "default": "us-east-1", "required": True},
            {"name": "instance_identifier", "type": "string", "description": "RDS instance identifier", "required": True},
            {"name": "engine", "type": "string", "description": "Database engine", "default": "postgres", "required": True},
            {"name": "engine_version", "type": "string", "description": "Engine version", "default": "15.4", "required": True},
            {"name": "instance_class", "type": "string", "description": "Instance class", "default": "db.t3.micro", "required": True},
            {"name": "allocated_storage_gb", "type": "number", "description": "Allocated storage (GB)", "default": 20, "required": True},
            {"name": "max_allocated_storage_gb", "type": "number", "description": "Max storage for autoscaling (GB)", "default": 100},
            {"name": "storage_type", "type": "string", "description": "Storage type", "default": "gp3", "required": True},
            {"name": "db_name", "type": "string", "description": "Initial database name", "default": "appdb", "required": True},
            {"name": "master_username", "type": "string", "description": "Master username", "default": "admin", "required": True},
            {"name": "master_password", "type": "string", "description": "Master password", "required": True, "sensitive": True},
            {"name": "multi_az", "type": "bool", "description": "Multi-AZ deployment", "default": False},
            {"name": "publicly_accessible", "type": "bool", "description": "Publicly accessible", "default": False},
            {"name": "backup_retention_days", "type": "number", "description": "Backup retention (days)", "default": 7},
            {"name": "backup_window", "type": "string", "description": "Backup window", "default": "03:00-04:00"},
            {"name": "maintenance_window", "type": "string", "description": "Maintenance window", "default": "Mon:04:00-Mon:05:00"},
            {"name": "deletion_protection", "type": "bool", "description": "Deletion protection", "default": True},
            {"name": "skip_final_snapshot", "type": "bool", "description": "Skip final snapshot on delete", "default": False},
            {"name": "tags", "type": "map", "description": "Additional tags", "default": {}},
        ],
        "output_definitions": [
            {"name": "instance_identifier", "description": "RDS instance identifier"},
            {"name": "endpoint", "description": "Connection endpoint"},
            {"name": "address", "description": "Hostname"},
            {"name": "port", "description": "Port"},
            {"name": "engine", "description": "Database engine"},
            {"name": "arn", "description": "Instance ARN"},
            {"name": "availability_zone", "description": "Availability zone"},
        ],
        "required_providers": {"aws": "~> 5.0"},
        "supported_providers": ["aws"],
        "tags": ["aws", "rds", "database", "day1", "seed"],
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


def create_rds_native_task(client: CMPClient) -> str:
    print("\n[RDS 2/6] Native Python task: AWS RDS Instance (Native SDK)...")
    name = "AWS RDS Instance (Native SDK)"
    payload = {
        "name": name,
        "description": "Creates an AWS RDS instance using boto3 with temporary credentials.",
        "language": "python",
        "code": RDS_NATIVE_TASK_CODE,
        "requirements": "boto3>=1.28.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance_identifier": {"type": "string", "description": "RDS instance identifier"},
                "engine": {"type": "string", "description": "Database engine"},
                "engine_version": {"type": "string", "description": "Engine version"},
                "instance_class": {"type": "string", "description": "Instance class"},
                "allocated_storage_gb": {"type": "string", "description": "Storage GB"},
                "storage_type": {"type": "string", "description": "Storage type"},
                "region": {"type": "string", "description": "AWS region"},
                "db_name": {"type": "string", "description": "Database name"},
                "master_username": {"type": "string", "description": "Master username"},
                "master_password": {"type": "string", "description": "Master password"},
                "multi_az": {"type": "string", "description": "Multi-AZ"},
                "publicly_accessible": {"type": "string", "description": "Public access"},
                "backup_retention_days": {"type": "string", "description": "Backup retention"},
            },
            "required": ["instance_identifier", "master_password"],
        },
        "tags": ["aws", "rds", "database", "native", "sdk", "seed"],
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


def create_rds_terraform_workflow(client: CMPClient, template_id: str) -> str:
    print("\n[RDS 3/6] Terraform workflow...")
    name = "aws-rds-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions an AWS RDS instance using Terraform.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — RDS Instance",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "region": "{{form.region}}",
                    "instance_identifier": "{{form.instance_identifier}}",
                    "engine": "{{form.engine}}",
                    "engine_version": "{{form.engine_version}}",
                    "instance_class": "{{form.instance_class}}",
                    "allocated_storage_gb": "{{form.allocated_storage_gb}}",
                    "storage_type": "{{form.storage_type}}",
                    "db_name": "{{form.db_name}}",
                    "master_username": "{{form.master_username}}",
                    "master_password": "{{form.master_password}}",
                    "multi_az": "{{form.multi_az}}",
                    "publicly_accessible": "{{form.publicly_accessible}}",
                    "backup_retention_days": "{{form.backup_retention_days}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 1200,
            }
        ],
        "tags": ["aws", "rds", "database", "terraform", "seed"],
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


def create_rds_native_workflow(client: CMPClient, task_id: str) -> str:
    print("\n[RDS 4/6] Native SDK workflow...")
    name = "aws-rds-native-provision"
    payload = {
        "name": name,
        "description": "Creates an AWS RDS instance using boto3 with temp credentials.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Create RDS Instance (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "instance_identifier": "{{form.instance_identifier}}",
                    "engine": "{{form.engine}}",
                    "engine_version": "{{form.engine_version}}",
                    "instance_class": "{{form.instance_class}}",
                    "allocated_storage_gb": "{{form.allocated_storage_gb}}",
                    "storage_type": "{{form.storage_type}}",
                    "region": "{{form.region}}",
                    "db_name": "{{form.db_name}}",
                    "master_username": "{{form.master_username}}",
                    "master_password": "{{form.master_password}}",
                    "multi_az": "{{form.multi_az}}",
                    "publicly_accessible": "{{form.publicly_accessible}}",
                    "backup_retention_days": "{{form.backup_retention_days}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 900,
            }
        ],
        "tags": ["aws", "rds", "database", "native", "sdk", "seed"],
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
    payload = {
        "name": name,
        "description": desc,
        "workflows": [
            {"workflow_id": workflow_id, "order": 1, "depends_on": [], "input_mapping": {}}
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


def create_catalog(client: CMPClient, flow_id: str, name: str, description: str, tags: list, form_fields: list) -> str:
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
        description="Seed AWS S3 Bucket & RDS Instance catalogs into CMP"
    )
    parser.add_argument("--url", default=os.environ.get("CMP_URL", "http://localhost:8001"))
    parser.add_argument("--token", default=os.environ.get("CMP_TOKEN", ""))
    args = parser.parse_args()

    if not args.token:
        print("ERROR: No token provided. Use --token or set CMP_TOKEN env var.")
        sys.exit(1)

    client = CMPClient(args.url, args.token)

    print("=" * 70)
    print("  AWS S3 & RDS — CMP Seed Script")
    print("=" * 70)
    print(f"  Target: {args.url}")
    print(f"  Token:  {args.token[:20]}...")
    print("=" * 70)

    # ══════════════════════════════════════════════════════════════════════
    # S3 BUCKET
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "═" * 70)
    print("  AWS S3 BUCKET")
    print("═" * 70)

    # Terraform
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    s3_template_id = create_s3_terraform_template(client)
    s3_tf_workflow_id = create_s3_terraform_workflow(client, s3_template_id)

    print("\n[S3 5/6] Creating Terraform flow...")
    s3_tf_flow_id = create_flow(client, s3_tf_workflow_id, "aws-s3-terraform-flow",
                                "Flow for AWS S3 Bucket provisioning via Terraform",
                                ["aws", "s3", "storage", "terraform", "seed"])

    print("\n[S3 6/6] Creating Terraform catalog item...")
    s3_tf_catalog_id = create_catalog(client, s3_tf_flow_id,
        "AWS S3 Bucket (Terraform)",
        "Provision an AWS S3 bucket using Terraform. Configure versioning, encryption, "
        "public access blocking, and lifecycle rules. Full IaC with state management.",
        ["aws", "s3", "storage", "bucket", "terraform", "seed"],
        S3_BUCKET_FORM_FIELDS)

    # Native
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + boto3)")
    print("─" * 70)

    s3_task_id = create_s3_native_task(client)
    s3_native_workflow_id = create_s3_native_workflow(client, s3_task_id)

    print("\n[S3 5/6] Creating native flow...")
    s3_native_flow_id = create_flow(client, s3_native_workflow_id, "aws-s3-native-flow",
                                    "Flow for AWS S3 Bucket provisioning via boto3",
                                    ["aws", "s3", "storage", "native", "sdk", "seed"])

    print("\n[S3 6/6] Creating native catalog item...")
    s3_native_catalog_id = create_catalog(client, s3_native_flow_id,
        "AWS S3 Bucket (Native SDK)",
        "Create an AWS S3 bucket using boto3. Uses temporary credentials from the "
        "selected AWS credential. Configures encryption, versioning, and access controls.",
        ["aws", "s3", "storage", "bucket", "native", "sdk", "seed"],
        S3_BUCKET_FORM_FIELDS)

    # ══════════════════════════════════════════════════════════════════════
    # RDS INSTANCE
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "═" * 70)
    print("  AWS RDS INSTANCE")
    print("═" * 70)

    # Terraform
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    rds_template_id = create_rds_terraform_template(client)
    rds_tf_workflow_id = create_rds_terraform_workflow(client, rds_template_id)

    print("\n[RDS 5/6] Creating Terraform flow...")
    rds_tf_flow_id = create_flow(client, rds_tf_workflow_id, "aws-rds-terraform-flow",
                                 "Flow for AWS RDS Instance provisioning via Terraform",
                                 ["aws", "rds", "database", "terraform", "seed"])

    print("\n[RDS 6/6] Creating Terraform catalog item...")
    rds_tf_catalog_id = create_catalog(client, rds_tf_flow_id,
        "AWS RDS Instance (Terraform)",
        "Provision an AWS RDS database instance using Terraform. Supports PostgreSQL, "
        "MySQL, MariaDB, and SQL Server with configurable class, storage, Multi-AZ, and backups.",
        ["aws", "rds", "database", "terraform", "seed"],
        RDS_INSTANCE_FORM_FIELDS)

    # Native
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + boto3)")
    print("─" * 70)

    rds_task_id = create_rds_native_task(client)
    rds_native_workflow_id = create_rds_native_workflow(client, rds_task_id)

    print("\n[RDS 5/6] Creating native flow...")
    rds_native_flow_id = create_flow(client, rds_native_workflow_id, "aws-rds-native-flow",
                                     "Flow for AWS RDS Instance provisioning via boto3",
                                     ["aws", "rds", "database", "native", "sdk", "seed"])

    print("\n[RDS 6/6] Creating native catalog item...")
    rds_native_catalog_id = create_catalog(client, rds_native_flow_id,
        "AWS RDS Instance (Native SDK)",
        "Create an AWS RDS database instance using boto3. Supports PostgreSQL, MySQL, "
        "MariaDB, and SQL Server. Uses temporary credentials with automatic waiter for availability.",
        ["aws", "rds", "database", "native", "sdk", "seed"],
        RDS_INSTANCE_FORM_FIELDS)

    # ══════════════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  DONE! Summary of created resources:")
    print("=" * 70)
    print(f"""
  AWS S3 BUCKET:
  ──────────────
    Terraform:  Template={s3_template_id} | Workflow={s3_tf_workflow_id} | Flow={s3_tf_flow_id} | Catalog={s3_tf_catalog_id}
    Native SDK: Task={s3_task_id} | Workflow={s3_native_workflow_id} | Flow={s3_native_flow_id} | Catalog={s3_native_catalog_id}

  AWS RDS INSTANCE:
  ─────────────────
    Terraform:  Template={rds_template_id} | Workflow={rds_tf_workflow_id} | Flow={rds_tf_flow_id} | Catalog={rds_tf_catalog_id}
    Native SDK: Task={rds_task_id} | Workflow={rds_native_workflow_id} | Flow={rds_native_flow_id} | Catalog={rds_native_catalog_id}

  CREDENTIAL SECURITY:
    - Terraform: AWS creds → env vars → deleted after execution
    - Native:    AWS creds → boto3 session with temp token → never stored on disk
""")


if __name__ == "__main__":
    main()
