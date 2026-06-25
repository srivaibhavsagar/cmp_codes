#!/usr/bin/env python3
"""
Azure Blob Storage & Azure SQL Database — Seed Script

Creates all CMP resources needed to provision:
  1. Azure Blob Storage Account (Terraform + Native SDK)
  2. Azure SQL Database (Terraform + Native SDK)

Both approaches use secure credentials from the selected
Azure cloud credential — the service principal secret is never exposed.

Usage:
    python seed_azure_storage_sql_catalog.py --url https://your-cmp.example.com --token <admin_jwt>

    # Or with environment variables:
    export CMP_URL=http://localhost:8001
    export CMP_TOKEN=eyJhbGciOiJIUzI1NiIs...
    python seed_azure_storage_sql_catalog.py
"""

import argparse
import json
import os
import sys
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Terraform HCL Templates
# ─────────────────────────────────────────────────────────────────────────────

AZURE_STORAGE_HCL = r'''
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

resource "azurerm_resource_group" "rg" {
  count    = var.create_resource_group ? 1 : 0
  name     = var.resource_group_name
  location = var.location

  tags = { ManagedBy = "cmp" }
}

locals {
  rg_name = var.create_resource_group ? azurerm_resource_group.rg[0].name : var.resource_group_name
}

resource "azurerm_storage_account" "storage" {
  name                     = var.storage_account_name
  resource_group_name      = local.rg_name
  location                 = var.location
  account_tier             = var.account_tier
  account_replication_type = var.replication_type
  account_kind             = var.account_kind
  min_tls_version          = "TLS1_2"
  allow_nested_items_to_be_public = !var.block_public_access

  blob_properties {
    versioning_enabled = var.versioning_enabled

    dynamic "delete_retention_policy" {
      for_each = var.soft_delete_days > 0 ? [1] : []
      content {
        days = var.soft_delete_days
      }
    }
  }

  tags = {
    Name      = var.storage_account_name
    ManagedBy = "cmp"
  }
}

resource "azurerm_storage_container" "container" {
  count                 = var.container_name != "" ? 1 : 0
  name                  = var.container_name
  storage_account_name  = azurerm_storage_account.storage.name
  container_access_type = "private"
}

output "storage_account_name" {
  value       = azurerm_storage_account.storage.name
  description = "Storage account name"
}

output "primary_blob_endpoint" {
  value       = azurerm_storage_account.storage.primary_blob_endpoint
  description = "Primary blob endpoint URL"
}

output "primary_access_key" {
  value       = azurerm_storage_account.storage.primary_access_key
  description = "Primary access key"
  sensitive   = true
}

output "resource_group" {
  value       = local.rg_name
  description = "Resource group name"
}

output "location" {
  value       = azurerm_storage_account.storage.location
  description = "Location"
}
'''

AZURE_SQL_HCL = r'''
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

resource "azurerm_resource_group" "rg" {
  count    = var.create_resource_group ? 1 : 0
  name     = var.resource_group_name
  location = var.location

  tags = { ManagedBy = "cmp" }
}

locals {
  rg_name = var.create_resource_group ? azurerm_resource_group.rg[0].name : var.resource_group_name
}

resource "azurerm_mssql_server" "server" {
  name                         = var.server_name
  resource_group_name          = local.rg_name
  location                     = var.location
  version                      = "12.0"
  administrator_login          = var.admin_username
  administrator_login_password = var.admin_password
  minimum_tls_version          = "1.2"

  tags = {
    Name      = var.server_name
    ManagedBy = "cmp"
  }
}

resource "azurerm_mssql_database" "db" {
  name         = var.database_name
  server_id    = azurerm_mssql_server.server.id
  collation    = "SQL_Latin1_General_CP1_CI_AS"
  max_size_gb  = var.max_size_gb
  sku_name     = var.sku_name
  zone_redundant = var.zone_redundant

  tags = {
    Name      = var.database_name
    ManagedBy = "cmp"
  }
}

resource "azurerm_mssql_firewall_rule" "allow_azure" {
  name             = "AllowAzureServices"
  server_id        = azurerm_mssql_server.server.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

output "server_name" {
  value       = azurerm_mssql_server.server.name
  description = "SQL Server name"
}

output "server_fqdn" {
  value       = azurerm_mssql_server.server.fully_qualified_domain_name
  description = "Fully qualified domain name"
}

output "database_name" {
  value       = azurerm_mssql_database.db.name
  description = "Database name"
}

output "database_id" {
  value       = azurerm_mssql_database.db.id
  description = "Database resource ID"
}

output "connection_string" {
  value       = "Server=tcp:${azurerm_mssql_server.server.fully_qualified_domain_name},1433;Initial Catalog=${azurerm_mssql_database.db.name};Persist Security Info=False;User ID=${var.admin_username};Password=${var.admin_password};MultipleActiveResultSets=False;Encrypt=True;TrustServerCertificate=False;Connection Timeout=30;"
  description = "ADO.NET connection string"
  sensitive   = true
}
'''

# ─────────────────────────────────────────────────────────────────────────────
# Native Python Task Code
# ─────────────────────────────────────────────────────────────────────────────

AZURE_STORAGE_NATIVE_TASK_CODE = r'''"""
Azure Blob Storage Account — Native Python Task

Creates an Azure Storage Account using the Azure SDK for Python
with service principal credentials from the CMP credential context.

CMP injects context as:
  cmp["credential"]["azure_client_id"]
  cmp["credential"]["azure_client_secret"]
  cmp["credential"]["azure_tenant_id"]
  cmp["credential"]["azure_subscription_id"]
  params["storage_account_name"] — form data
"""
import json
import sys

try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.storage import StorageManagementClient
    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError:
        from azure.mgmt.resource.resources import ResourceManagementClient
except ImportError:
    print("ERROR: azure-identity, azure-mgmt-storage, azure-mgmt-resource required")
    sys.exit(1)

credential = cmp.get("credential", {})
client_id = credential.get("azure_client_id", "")
client_secret = credential.get("azure_client_secret", "")
tenant_id = credential.get("azure_tenant_id", "")
subscription_id = params.get("subscription_id") or credential.get("azure_subscription_id", "")

storage_account_name = params.get("storage_account_name", "")
resource_group = params.get("resource_group_name", "")
location = params.get("location", "eastus")
account_tier = params.get("account_tier", "Standard")
replication_type = params.get("replication_type", "LRS")
account_kind = params.get("account_kind", "StorageV2")
container_name = params.get("container_name", "")
versioning_enabled = str(params.get("versioning_enabled", "false")).lower() in ("true", "1", "yes")

if not all([client_id, client_secret, tenant_id, subscription_id]):
    print("ERROR: Missing Azure credential fields.")
    sys.exit(1)

if not storage_account_name:
    print("ERROR: storage_account_name is required.")
    sys.exit(1)

if not resource_group:
    print("ERROR: resource_group_name is required.")
    sys.exit(1)

print(f"[Azure] Creating storage account '{storage_account_name}' in {location}")
print(f"[Azure] Tier: {account_tier}, Replication: {replication_type}, Kind: {account_kind}")

creds = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
resource_client = ResourceManagementClient(creds, subscription_id)
storage_client = StorageManagementClient(creds, subscription_id)

# Ensure resource group
print(f"[Azure] Ensuring resource group '{resource_group}'...")
resource_client.resource_groups.create_or_update(
    resource_group, {"location": location, "tags": {"ManagedBy": "cmp"}}
)

# Create storage account
print(f"[Azure] Creating storage account...")
poller = storage_client.storage_accounts.begin_create(
    resource_group,
    storage_account_name,
    {
        "location": location,
        "sku": {"name": f"{account_tier}_{replication_type}"},
        "kind": account_kind,
        "properties": {
            "minimum_tls_version": "TLS1_2",
            "allow_blob_public_access": False,
            "supportsHttpsTrafficOnly": True,
        },
        "tags": {"Name": storage_account_name, "ManagedBy": "cmp"},
    },
)
account = poller.result()
print(f"[Azure] Storage account created: {account.name}")

# Enable versioning via blob service properties
if versioning_enabled:
    print("[Azure] Enabling blob versioning...")
    storage_client.blob_services.set_service_properties(
        resource_group,
        storage_account_name,
        {
            "is_versioning_enabled": True,
        },
    )

# Create container if specified
if container_name:
    print(f"[Azure] Creating container '{container_name}'...")
    storage_client.blob_containers.create(
        resource_group,
        storage_account_name,
        container_name,
        {"public_access": "None"},
    )

# Get keys
keys = storage_client.storage_accounts.list_keys(resource_group, storage_account_name)
primary_key = keys.keys[0].value if keys.keys else "N/A"

output = {
    "status": "success",
    "storage_account_name": account.name,
    "primary_blob_endpoint": account.primary_endpoints.blob,
    "location": account.location,
    "resource_group": resource_group,
    "account_tier": account_tier,
    "replication_type": replication_type,
    "container": container_name or "none",
    "message": f"Storage account '{account.name}' created in {location}",
}
print(json.dumps(output))
'''

AZURE_SQL_NATIVE_TASK_CODE = r'''"""
Azure SQL Database — Native Python Task

Creates an Azure SQL Server and Database using the Azure SDK for Python
with service principal credentials from the CMP credential context.

CMP injects context as:
  cmp["credential"]["azure_client_id"]
  cmp["credential"]["azure_client_secret"]
  cmp["credential"]["azure_tenant_id"]
  cmp["credential"]["azure_subscription_id"]
  params["server_name"] — form data
"""
import json
import sys

try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.sql import SqlManagementClient
    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError:
        from azure.mgmt.resource.resources import ResourceManagementClient
except ImportError:
    print("ERROR: azure-identity, azure-mgmt-sql, azure-mgmt-resource required")
    sys.exit(1)

credential = cmp.get("credential", {})
client_id = credential.get("azure_client_id", "")
client_secret = credential.get("azure_client_secret", "")
tenant_id = credential.get("azure_tenant_id", "")
subscription_id = params.get("subscription_id") or credential.get("azure_subscription_id", "")

server_name = params.get("server_name", "")
database_name = params.get("database_name", "appdb")
resource_group = params.get("resource_group_name", "")
location = params.get("location", "eastus")
admin_username = params.get("admin_username", "sqladmin")
admin_password = params.get("admin_password", "")
sku_name = params.get("sku_name", "Basic")
max_size_gb = int(params.get("max_size_gb", "2"))

if not all([client_id, client_secret, tenant_id, subscription_id]):
    print("ERROR: Missing Azure credential fields.")
    sys.exit(1)

if not server_name:
    print("ERROR: server_name is required.")
    sys.exit(1)

if not resource_group:
    print("ERROR: resource_group_name is required.")
    sys.exit(1)

if not admin_password:
    print("ERROR: admin_password is required.")
    sys.exit(1)

print(f"[Azure] Creating SQL Server '{server_name}' in {location}")
print(f"[Azure] Database: {database_name}, SKU: {sku_name}, Max size: {max_size_gb}GB")

creds = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
resource_client = ResourceManagementClient(creds, subscription_id)
sql_client = SqlManagementClient(creds, subscription_id)

# Ensure resource group
print(f"[Azure] Ensuring resource group '{resource_group}'...")
resource_client.resource_groups.create_or_update(
    resource_group, {"location": location, "tags": {"ManagedBy": "cmp"}}
)

# Create SQL Server
print(f"[Azure] Creating SQL Server '{server_name}'...")
server_poller = sql_client.servers.begin_create_or_update(
    resource_group,
    server_name,
    {
        "location": location,
        "properties": {
            "administrator_login": admin_username,
            "administrator_login_password": admin_password,
            "version": "12.0",
            "minimal_tls_version": "1.2",
        },
        "tags": {"Name": server_name, "ManagedBy": "cmp"},
    },
)
server = server_poller.result()
print(f"[Azure] SQL Server created: {server.fully_qualified_domain_name}")

# Create firewall rule to allow Azure services
print("[Azure] Adding firewall rule for Azure services...")
sql_client.firewall_rules.create_or_update(
    resource_group, server_name, "AllowAzureServices",
    {"start_ip_address": "0.0.0.0", "end_ip_address": "0.0.0.0"}
)

# Create Database
print(f"[Azure] Creating database '{database_name}'...")
db_poller = sql_client.databases.begin_create_or_update(
    resource_group,
    server_name,
    database_name,
    {
        "location": location,
        "sku": {"name": sku_name},
        "properties": {
            "collation": "SQL_Latin1_General_CP1_CI_AS",
            "max_size_bytes": max_size_gb * 1024 * 1024 * 1024,
        },
        "tags": {"Name": database_name, "ManagedBy": "cmp"},
    },
)
db = db_poller.result()
print(f"[Azure] Database created: {db.name}")

fqdn = server.fully_qualified_domain_name
output = {
    "status": "success",
    "server_name": server_name,
    "server_fqdn": fqdn,
    "database_name": database_name,
    "admin_username": admin_username,
    "sku_name": sku_name,
    "max_size_gb": max_size_gb,
    "location": location,
    "resource_group": resource_group,
    "connection_string": f"Server=tcp:{fqdn},1433;Initial Catalog={database_name};User ID={admin_username};Password=***;Encrypt=True;",
    "message": f"Azure SQL '{server_name}/{database_name}' created in {location}",
}
print(json.dumps(output))
'''

# ─────────────────────────────────────────────────────────────────────────────
# Form Field Definitions
# ─────────────────────────────────────────────────────────────────────────────

AZURE_STORAGE_FORM_FIELDS = [
    {
        "field_id": "storage_account_name",
        "label": "Storage Account Name",
        "type": "string",
        "required": True,
        "placeholder": "myappdata01",
        "description": "Globally unique name (lowercase letters and numbers only, 3-24 chars)",
        "validation": {
            "pattern": "^[a-z0-9]{3,24}$",
            "message": "3-24 chars: lowercase letters and numbers only",
        },
    },
    {
        "field_id": "resource_group_name",
        "label": "Resource Group",
        "type": "string",
        "required": True,
        "placeholder": "my-app-rg",
        "description": "Azure resource group (will be created if it doesn't exist)",
    },
    {
        "field_id": "location",
        "label": "Region",
        "type": "select",
        "required": True,
        "default": "eastus",
        "options": [
            {"label": "East US", "value": "eastus"},
            {"label": "East US 2", "value": "eastus2"},
            {"label": "West US 2", "value": "westus2"},
            {"label": "West Europe", "value": "westeurope"},
            {"label": "North Europe", "value": "northeurope"},
            {"label": "UK South", "value": "uksouth"},
            {"label": "Southeast Asia", "value": "southeastasia"},
            {"label": "Australia East", "value": "australiaeast"},
            {"label": "Central India", "value": "centralindia"},
        ],
        "description": "Azure region for the storage account",
    },
    {
        "field_id": "account_tier",
        "label": "Performance Tier",
        "type": "select",
        "required": True,
        "default": "Standard",
        "options": [
            {"label": "Standard (HDD-backed, cost effective)", "value": "Standard"},
            {"label": "Premium (SSD-backed, low latency)", "value": "Premium"},
        ],
        "description": "Performance tier of the storage account",
    },
    {
        "field_id": "replication_type",
        "label": "Replication",
        "type": "select",
        "required": True,
        "default": "LRS",
        "options": [
            {"label": "LRS — Locally Redundant (3 copies, single region)", "value": "LRS"},
            {"label": "ZRS — Zone Redundant (3 zones, single region)", "value": "ZRS"},
            {"label": "GRS — Geo Redundant (6 copies, two regions)", "value": "GRS"},
            {"label": "RAGRS — Read-Access Geo Redundant", "value": "RAGRS"},
        ],
        "description": "Data redundancy strategy",
    },
    {
        "field_id": "account_kind",
        "label": "Account Kind",
        "type": "select",
        "required": True,
        "default": "StorageV2",
        "options": [
            {"label": "StorageV2 (General Purpose v2 — recommended)", "value": "StorageV2"},
            {"label": "BlobStorage (Blob-only, legacy)", "value": "BlobStorage"},
            {"label": "BlockBlobStorage (Premium block blobs)", "value": "BlockBlobStorage"},
        ],
        "description": "Storage account kind",
    },
    {
        "field_id": "container_name",
        "label": "Initial Container Name",
        "type": "string",
        "required": False,
        "placeholder": "data",
        "description": "Optional: create an initial blob container",
    },
    {
        "field_id": "versioning_enabled",
        "label": "Blob Versioning",
        "type": "select",
        "required": True,
        "default": "false",
        "options": [
            {"label": "Disabled", "value": "false"},
            {"label": "Enabled", "value": "true"},
        ],
        "description": "Keep previous versions of blobs when overwritten or deleted",
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
        "description": "Disallow public access to blobs",
    },
]

AZURE_SQL_FORM_FIELDS = [
    {
        "field_id": "server_name",
        "label": "SQL Server Name",
        "type": "string",
        "required": True,
        "placeholder": "my-app-sql-server",
        "description": "Globally unique Azure SQL Server name (lowercase, hyphens, 1-63 chars)",
        "validation": {
            "pattern": "^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$",
            "message": "Lowercase letters, numbers, hyphens. Must start/end with letter or number.",
        },
    },
    {
        "field_id": "database_name",
        "label": "Database Name",
        "type": "string",
        "required": True,
        "default": "appdb",
        "placeholder": "appdb",
        "description": "Name of the SQL database to create",
    },
    {
        "field_id": "resource_group_name",
        "label": "Resource Group",
        "type": "string",
        "required": True,
        "placeholder": "my-app-rg",
        "description": "Azure resource group (will be created if it doesn't exist)",
    },
    {
        "field_id": "location",
        "label": "Region",
        "type": "select",
        "required": True,
        "default": "eastus",
        "options": [
            {"label": "East US", "value": "eastus"},
            {"label": "East US 2", "value": "eastus2"},
            {"label": "West US 2", "value": "westus2"},
            {"label": "West Europe", "value": "westeurope"},
            {"label": "North Europe", "value": "northeurope"},
            {"label": "UK South", "value": "uksouth"},
            {"label": "Southeast Asia", "value": "southeastasia"},
            {"label": "Australia East", "value": "australiaeast"},
            {"label": "Central India", "value": "centralindia"},
        ],
        "description": "Azure region for the SQL Server",
    },
    {
        "field_id": "sku_name",
        "label": "Pricing Tier (SKU)",
        "type": "select",
        "required": True,
        "default": "Basic",
        "options": [
            {"label": "Basic (5 DTU, 2 GB) — Dev/Test", "value": "Basic"},
            {"label": "S0 (10 DTU, 250 GB) — Standard", "value": "S0"},
            {"label": "S1 (20 DTU, 250 GB) — Standard", "value": "S1"},
            {"label": "S2 (50 DTU, 250 GB) — Standard", "value": "S2"},
            {"label": "S3 (100 DTU, 250 GB) — Standard", "value": "S3"},
            {"label": "P1 (125 DTU, 500 GB) — Premium", "value": "P1"},
            {"label": "P2 (250 DTU, 500 GB) — Premium", "value": "P2"},
            {"label": "GP_S_Gen5_1 (Serverless Gen5, 1 vCore)", "value": "GP_S_Gen5_1"},
            {"label": "GP_S_Gen5_2 (Serverless Gen5, 2 vCores)", "value": "GP_S_Gen5_2"},
            {"label": "GP_Gen5_2 (Provisioned Gen5, 2 vCores)", "value": "GP_Gen5_2"},
        ],
        "description": "Pricing tier determining performance and cost",
    },
    {
        "field_id": "max_size_gb",
        "label": "Max Database Size (GB)",
        "type": "select",
        "required": True,
        "default": "2",
        "options": [
            {"label": "2 GB", "value": "2"},
            {"label": "5 GB", "value": "5"},
            {"label": "10 GB", "value": "10"},
            {"label": "20 GB", "value": "20"},
            {"label": "50 GB", "value": "50"},
            {"label": "100 GB", "value": "100"},
            {"label": "250 GB", "value": "250"},
        ],
        "description": "Maximum database size",
    },
    {
        "field_id": "admin_username",
        "label": "Admin Username",
        "type": "string",
        "required": True,
        "default": "sqladmin",
        "placeholder": "sqladmin",
        "description": "SQL Server administrator username",
    },
    {
        "field_id": "admin_password",
        "label": "Admin Password",
        "type": "password",
        "required": True,
        "placeholder": "••••••••",
        "description": "Admin password (min 8 chars, requires uppercase, lowercase, number, special char)",
        "validation": {
            "min_length": 8,
            "message": "Password must be at least 8 characters with mixed case, numbers, and special chars",
        },
    },
    {
        "field_id": "zone_redundant",
        "label": "Zone Redundancy",
        "type": "select",
        "required": True,
        "default": "false",
        "options": [
            {"label": "Disabled (single zone)", "value": "false"},
            {"label": "Enabled (zone redundant — Premium/Business Critical only)", "value": "true"},
        ],
        "description": "Enable zone redundancy for high availability (Premium tier only)",
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
        items = self.get(path, params={"name": name})
        if isinstance(items, list):
            for item in items:
                if item.get("name") == name:
                    return item
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Seed Functions — Azure Storage
# ─────────────────────────────────────────────────────────────────────────────


def create_storage_terraform_template(client: CMPClient) -> str:
    print("\n[STOR 1/6] Terraform template: Azure Storage Account...")
    name = "Azure Storage Account"
    payload = {
        "name": name,
        "description": (
            "Provisions an Azure Storage Account with configurable tier, replication, "
            "versioning, and optional container. Uses azurerm provider ~> 3.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": AZURE_STORAGE_HCL},
        "input_variables": [
            {"name": "subscription_id", "type": "string", "description": "Azure subscription ID", "required": True},
            {"name": "resource_group_name", "type": "string", "description": "Resource group name", "required": True},
            {"name": "create_resource_group", "type": "bool", "description": "Create RG if needed", "default": True},
            {"name": "location", "type": "string", "description": "Azure region", "default": "eastus", "required": True},
            {"name": "storage_account_name", "type": "string", "description": "Storage account name", "required": True},
            {"name": "account_tier", "type": "string", "description": "Performance tier", "default": "Standard"},
            {"name": "replication_type", "type": "string", "description": "Replication type", "default": "LRS"},
            {"name": "account_kind", "type": "string", "description": "Account kind", "default": "StorageV2"},
            {"name": "versioning_enabled", "type": "bool", "description": "Enable blob versioning", "default": False},
            {"name": "block_public_access", "type": "bool", "description": "Block public access", "default": True},
            {"name": "soft_delete_days", "type": "number", "description": "Soft delete retention days (0=disabled)", "default": 0},
            {"name": "container_name", "type": "string", "description": "Optional initial container name", "default": ""},
        ],
        "output_definitions": [
            {"name": "storage_account_name", "description": "Storage account name"},
            {"name": "primary_blob_endpoint", "description": "Primary blob endpoint"},
            {"name": "primary_access_key", "description": "Primary access key"},
            {"name": "resource_group", "description": "Resource group"},
            {"name": "location", "description": "Location"},
        ],
        "required_providers": {"azurerm": "~> 3.0"},
        "supported_providers": ["azure"],
        "tags": ["azure", "storage", "blob", "day1", "seed"],
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


def create_storage_native_task(client: CMPClient) -> str:
    print("\n[STOR 2/6] Native Python task: Azure Storage Account (Native SDK)...")
    name = "Azure Storage Account (Native SDK)"
    payload = {
        "name": name,
        "description": "Creates an Azure Storage Account using the Azure SDK with service principal credentials.",
        "language": "python",
        "code": AZURE_STORAGE_NATIVE_TASK_CODE,
        "requirements": "azure-identity>=1.14.0\nazure-mgmt-storage>=21.0.0\nazure-mgmt-resource>=23.0.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "storage_account_name": {"type": "string", "description": "Storage account name"},
                "resource_group_name": {"type": "string", "description": "Resource group"},
                "location": {"type": "string", "description": "Azure region"},
                "account_tier": {"type": "string", "description": "Performance tier"},
                "replication_type": {"type": "string", "description": "Replication type"},
                "account_kind": {"type": "string", "description": "Account kind"},
                "container_name": {"type": "string", "description": "Initial container"},
                "versioning_enabled": {"type": "string", "description": "Enable versioning"},
            },
            "required": ["storage_account_name", "resource_group_name"],
        },
        "tags": ["azure", "storage", "blob", "native", "sdk", "seed"],
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


def create_storage_terraform_workflow(client: CMPClient, template_id: str) -> str:
    print("\n[STOR 3/6] Terraform workflow...")
    name = "azure-storage-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions an Azure Storage Account using Terraform.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — Azure Storage",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "subscription_id": "{{credential.azure_subscription_id}}",
                    "resource_group_name": "{{form.resource_group_name}}",
                    "location": "{{form.location}}",
                    "storage_account_name": "{{form.storage_account_name}}",
                    "account_tier": "{{form.account_tier}}",
                    "replication_type": "{{form.replication_type}}",
                    "account_kind": "{{form.account_kind}}",
                    "versioning_enabled": "{{form.versioning_enabled}}",
                    "block_public_access": "{{form.block_public_access}}",
                    "container_name": "{{form.container_name}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 300,
            }
        ],
        "tags": ["azure", "storage", "blob", "terraform", "seed"],
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


def create_storage_native_workflow(client: CMPClient, task_id: str) -> str:
    print("\n[STOR 4/6] Native SDK workflow...")
    name = "azure-storage-native-provision"
    payload = {
        "name": name,
        "description": "Creates an Azure Storage Account using the Azure SDK.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Create Azure Storage (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "storage_account_name": "{{form.storage_account_name}}",
                    "resource_group_name": "{{form.resource_group_name}}",
                    "location": "{{form.location}}",
                    "account_tier": "{{form.account_tier}}",
                    "replication_type": "{{form.replication_type}}",
                    "account_kind": "{{form.account_kind}}",
                    "container_name": "{{form.container_name}}",
                    "versioning_enabled": "{{form.versioning_enabled}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 300,
            }
        ],
        "tags": ["azure", "storage", "blob", "native", "sdk", "seed"],
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
# Seed Functions — Azure SQL
# ─────────────────────────────────────────────────────────────────────────────


def create_sql_terraform_template(client: CMPClient) -> str:
    print("\n[SQL 1/6] Terraform template: Azure SQL Database...")
    name = "Azure SQL Database"
    payload = {
        "name": name,
        "description": (
            "Provisions an Azure SQL Server and Database with configurable SKU, "
            "size, and firewall rules. Uses azurerm provider ~> 3.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": AZURE_SQL_HCL},
        "input_variables": [
            {"name": "subscription_id", "type": "string", "description": "Azure subscription ID", "required": True},
            {"name": "resource_group_name", "type": "string", "description": "Resource group name", "required": True},
            {"name": "create_resource_group", "type": "bool", "description": "Create RG if needed", "default": True},
            {"name": "location", "type": "string", "description": "Azure region", "default": "eastus", "required": True},
            {"name": "server_name", "type": "string", "description": "SQL Server name", "required": True},
            {"name": "database_name", "type": "string", "description": "Database name", "default": "appdb", "required": True},
            {"name": "admin_username", "type": "string", "description": "Admin username", "default": "sqladmin", "required": True},
            {"name": "admin_password", "type": "string", "description": "Admin password", "required": True, "sensitive": True},
            {"name": "sku_name", "type": "string", "description": "SKU/pricing tier", "default": "Basic", "required": True},
            {"name": "max_size_gb", "type": "number", "description": "Max database size (GB)", "default": 2, "required": True},
            {"name": "zone_redundant", "type": "bool", "description": "Zone redundancy", "default": False},
        ],
        "output_definitions": [
            {"name": "server_name", "description": "SQL Server name"},
            {"name": "server_fqdn", "description": "Fully qualified domain name"},
            {"name": "database_name", "description": "Database name"},
            {"name": "database_id", "description": "Database resource ID"},
            {"name": "connection_string", "description": "ADO.NET connection string"},
        ],
        "required_providers": {"azurerm": "~> 3.0"},
        "supported_providers": ["azure"],
        "tags": ["azure", "sql", "database", "day1", "seed"],
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


def create_sql_native_task(client: CMPClient) -> str:
    print("\n[SQL 2/6] Native Python task: Azure SQL Database (Native SDK)...")
    name = "Azure SQL Database (Native SDK)"
    payload = {
        "name": name,
        "description": "Creates an Azure SQL Server and Database using the Azure SDK with SP credentials.",
        "language": "python",
        "code": AZURE_SQL_NATIVE_TASK_CODE,
        "requirements": "azure-identity>=1.14.0\nazure-mgmt-sql>=4.0.0\nazure-mgmt-resource>=23.0.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "server_name": {"type": "string", "description": "SQL Server name"},
                "database_name": {"type": "string", "description": "Database name"},
                "resource_group_name": {"type": "string", "description": "Resource group"},
                "location": {"type": "string", "description": "Azure region"},
                "admin_username": {"type": "string", "description": "Admin username"},
                "admin_password": {"type": "string", "description": "Admin password"},
                "sku_name": {"type": "string", "description": "SKU"},
                "max_size_gb": {"type": "string", "description": "Max size GB"},
            },
            "required": ["server_name", "admin_password"],
        },
        "tags": ["azure", "sql", "database", "native", "sdk", "seed"],
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


def create_sql_terraform_workflow(client: CMPClient, template_id: str) -> str:
    print("\n[SQL 3/6] Terraform workflow...")
    name = "azure-sql-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions an Azure SQL Server and Database using Terraform.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — Azure SQL",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "subscription_id": "{{credential.azure_subscription_id}}",
                    "resource_group_name": "{{form.resource_group_name}}",
                    "location": "{{form.location}}",
                    "server_name": "{{form.server_name}}",
                    "database_name": "{{form.database_name}}",
                    "admin_username": "{{form.admin_username}}",
                    "admin_password": "{{form.admin_password}}",
                    "sku_name": "{{form.sku_name}}",
                    "max_size_gb": "{{form.max_size_gb}}",
                    "zone_redundant": "{{form.zone_redundant}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 600,
            }
        ],
        "tags": ["azure", "sql", "database", "terraform", "seed"],
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


def create_sql_native_workflow(client: CMPClient, task_id: str) -> str:
    print("\n[SQL 4/6] Native SDK workflow...")
    name = "azure-sql-native-provision"
    payload = {
        "name": name,
        "description": "Creates an Azure SQL Server and Database using the Azure SDK.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Create Azure SQL (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "server_name": "{{form.server_name}}",
                    "database_name": "{{form.database_name}}",
                    "resource_group_name": "{{form.resource_group_name}}",
                    "location": "{{form.location}}",
                    "admin_username": "{{form.admin_username}}",
                    "admin_password": "{{form.admin_password}}",
                    "sku_name": "{{form.sku_name}}",
                    "max_size_gb": "{{form.max_size_gb}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 600,
            }
        ],
        "tags": ["azure", "sql", "database", "native", "sdk", "seed"],
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
        "workflows": [{"workflow_id": workflow_id, "order": 1, "depends_on": [], "input_mapping": {}}],
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
        "cloud_provider": "azure",
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
        description="Seed Azure Storage Account & Azure SQL Database catalogs into CMP"
    )
    parser.add_argument("--url", default=os.environ.get("CMP_URL", "http://localhost:8001"))
    parser.add_argument("--token", default=os.environ.get("CMP_TOKEN", ""))
    args = parser.parse_args()

    if not args.token:
        print("ERROR: No token provided. Use --token or set CMP_TOKEN env var.")
        sys.exit(1)

    client = CMPClient(args.url, args.token)

    print("=" * 70)
    print("  Azure Storage & SQL Database — CMP Seed Script")
    print("=" * 70)
    print(f"  Target: {args.url}")
    print(f"  Token:  {args.token[:20]}...")
    print("=" * 70)

    # ══════════════════════════════════════════════════════════════════════
    # AZURE STORAGE ACCOUNT
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "═" * 70)
    print("  AZURE STORAGE ACCOUNT")
    print("═" * 70)

    # Terraform
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    stor_template_id = create_storage_terraform_template(client)
    stor_tf_workflow_id = create_storage_terraform_workflow(client, stor_template_id)

    print("\n[STOR 5/6] Creating Terraform flow...")
    stor_tf_flow_id = create_flow(client, stor_tf_workflow_id, "azure-storage-terraform-flow",
                                  "Flow for Azure Storage Account provisioning via Terraform",
                                  ["azure", "storage", "blob", "terraform", "seed"])

    print("\n[STOR 6/6] Creating Terraform catalog item...")
    stor_tf_catalog_id = create_catalog(client, stor_tf_flow_id,
        "Azure Storage Account (Terraform)",
        "Provision an Azure Storage Account using Terraform. Configure tier, replication, "
        "versioning, and optional container. Full IaC with state management.",
        ["azure", "storage", "blob", "terraform", "seed"],
        AZURE_STORAGE_FORM_FIELDS)

    # Native
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + azure-sdk)")
    print("─" * 70)

    stor_task_id = create_storage_native_task(client)
    stor_native_workflow_id = create_storage_native_workflow(client, stor_task_id)

    print("\n[STOR 5/6] Creating native flow...")
    stor_native_flow_id = create_flow(client, stor_native_workflow_id, "azure-storage-native-flow",
                                      "Flow for Azure Storage Account provisioning via Azure SDK",
                                      ["azure", "storage", "blob", "native", "sdk", "seed"])

    print("\n[STOR 6/6] Creating native catalog item...")
    stor_native_catalog_id = create_catalog(client, stor_native_flow_id,
        "Azure Storage Account (Native SDK)",
        "Create an Azure Storage Account using the Azure SDK for Python. Uses service principal "
        "credentials. Configures tier, replication, versioning, and optional container.",
        ["azure", "storage", "blob", "native", "sdk", "seed"],
        AZURE_STORAGE_FORM_FIELDS)

    # ══════════════════════════════════════════════════════════════════════
    # AZURE SQL DATABASE
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "═" * 70)
    print("  AZURE SQL DATABASE")
    print("═" * 70)

    # Terraform
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    sql_template_id = create_sql_terraform_template(client)
    sql_tf_workflow_id = create_sql_terraform_workflow(client, sql_template_id)

    print("\n[SQL 5/6] Creating Terraform flow...")
    sql_tf_flow_id = create_flow(client, sql_tf_workflow_id, "azure-sql-terraform-flow",
                                 "Flow for Azure SQL Database provisioning via Terraform",
                                 ["azure", "sql", "database", "terraform", "seed"])

    print("\n[SQL 6/6] Creating Terraform catalog item...")
    sql_tf_catalog_id = create_catalog(client, sql_tf_flow_id,
        "Azure SQL Database (Terraform)",
        "Provision an Azure SQL Server and Database using Terraform. Configure SKU, "
        "size, admin credentials, and firewall rules. Full IaC with state management.",
        ["azure", "sql", "database", "terraform", "seed"],
        AZURE_SQL_FORM_FIELDS)

    # Native
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + azure-sdk)")
    print("─" * 70)

    sql_task_id = create_sql_native_task(client)
    sql_native_workflow_id = create_sql_native_workflow(client, sql_task_id)

    print("\n[SQL 5/6] Creating native flow...")
    sql_native_flow_id = create_flow(client, sql_native_workflow_id, "azure-sql-native-flow",
                                     "Flow for Azure SQL Database provisioning via Azure SDK",
                                     ["azure", "sql", "database", "native", "sdk", "seed"])

    print("\n[SQL 6/6] Creating native catalog item...")
    sql_native_catalog_id = create_catalog(client, sql_native_flow_id,
        "Azure SQL Database (Native SDK)",
        "Create an Azure SQL Server and Database using the Azure SDK for Python. Creates server, "
        "database, and firewall rules. Uses service principal credentials.",
        ["azure", "sql", "database", "native", "sdk", "seed"],
        AZURE_SQL_FORM_FIELDS)

    # ══════════════════════════════════════════════════════════════════════
    # Summary
    # ══════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  DONE! Summary of created resources:")
    print("=" * 70)
    print(f"""
  AZURE STORAGE ACCOUNT:
  ──────────────────────
    Terraform:  Template={stor_template_id} | Workflow={stor_tf_workflow_id} | Flow={stor_tf_flow_id} | Catalog={stor_tf_catalog_id}
    Native SDK: Task={stor_task_id} | Workflow={stor_native_workflow_id} | Flow={stor_native_flow_id} | Catalog={stor_native_catalog_id}

  AZURE SQL DATABASE:
  ───────────────────
    Terraform:  Template={sql_template_id} | Workflow={sql_tf_workflow_id} | Flow={sql_tf_flow_id} | Catalog={sql_tf_catalog_id}
    Native SDK: Task={sql_task_id} | Workflow={sql_native_workflow_id} | Flow={sql_native_flow_id} | Catalog={sql_native_catalog_id}

  CREDENTIAL SECURITY:
    - Terraform: SP credentials → env vars (ARM_CLIENT_ID, etc.) → deleted after execution
    - Native:    SP credentials → ClientSecretCredential → never stored on disk
""")


if __name__ == "__main__":
    main()
