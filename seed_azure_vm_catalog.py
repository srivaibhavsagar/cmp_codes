#!/usr/bin/env python3
"""
Azure VM Provisioning — Seed Script

Creates all CMP resources needed to provision Azure Virtual Machines:
  1. Terraform-based provisioning (IaC approach)
  2. Native Python task-based provisioning (SDK approach)

Both approaches use secure credentials from the selected
Azure cloud credential — the service principal secret is never exposed.

Usage:
    python seed_azure_vm_catalog.py --url https://your-cmp.example.com --token <admin_jwt>

    # Or with environment variables:
    export CMP_URL=http://localhost:8001
    export CMP_TOKEN=eyJhbGciOiJIUzI1NiIs...
    python seed_azure_vm_catalog.py
"""

import argparse
import json
import os
import sys
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Terraform HCL Template
# ─────────────────────────────────────────────────────────────────────────────

AZURE_VM_HCL = r'''
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

  tags = {
    ManagedBy = "cmp"
  }
}

locals {
  rg_name = var.create_resource_group ? azurerm_resource_group.rg[0].name : var.resource_group_name
}

resource "azurerm_virtual_network" "vnet" {
  count               = var.create_vnet ? 1 : 0
  name                = "${var.vm_name}-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = var.location
  resource_group_name = local.rg_name
}

resource "azurerm_subnet" "subnet" {
  count                = var.create_vnet ? 1 : 0
  name                 = "${var.vm_name}-subnet"
  resource_group_name  = local.rg_name
  virtual_network_name = azurerm_virtual_network.vnet[0].name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_public_ip" "pip" {
  count               = var.assign_public_ip ? 1 : 0
  name                = "${var.vm_name}-pip"
  location            = var.location
  resource_group_name = local.rg_name
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_network_interface" "nic" {
  name                = "${var.vm_name}-nic"
  location            = var.location
  resource_group_name = local.rg_name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.create_vnet ? azurerm_subnet.subnet[0].id : var.subnet_id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = var.assign_public_ip ? azurerm_public_ip.pip[0].id : null
  }
}

resource "azurerm_linux_virtual_machine" "vm" {
  name                  = var.vm_name
  resource_group_name   = local.rg_name
  location              = var.location
  size                  = var.vm_size
  admin_username        = var.admin_username
  network_interface_ids = [azurerm_network_interface.nic.id]

  admin_ssh_key {
    username   = var.admin_username
    public_key = var.ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = var.os_disk_type
    disk_size_gb         = var.os_disk_size_gb
  }

  source_image_reference {
    publisher = var.image_publisher
    offer     = var.image_offer
    sku       = var.image_sku
    version   = "latest"
  }

  tags = {
    Name      = var.vm_name
    ManagedBy = "cmp"
  }
}

output "vm_id" {
  value       = azurerm_linux_virtual_machine.vm.id
  description = "The VM resource ID"
}

output "vm_name" {
  value       = azurerm_linux_virtual_machine.vm.name
  description = "The VM name"
}

output "private_ip" {
  value       = azurerm_network_interface.nic.private_ip_address
  description = "Private IP address"
}

output "public_ip" {
  value       = var.assign_public_ip ? azurerm_public_ip.pip[0].ip_address : "none"
  description = "Public IP address"
}

output "location" {
  value       = azurerm_linux_virtual_machine.vm.location
  description = "Azure region"
}
'''

# ─────────────────────────────────────────────────────────────────────────────
# Native Python Task Code
# ─────────────────────────────────────────────────────────────────────────────

NATIVE_TASK_CODE = r'''"""
Azure VM Provisioning — Native Python Task

Provisions an Azure Virtual Machine using the Azure SDK for Python
with service principal credentials from the CMP credential context.

CMP injects context as:
  cmp["credential"]["azure_client_id"]
  cmp["credential"]["azure_client_secret"]
  cmp["credential"]["azure_tenant_id"]
  cmp["credential"]["azure_subscription_id"]
  params["vm_name"] — form data
"""
import json
import sys
import time

try:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.network import NetworkManagementClient
    try:
        from azure.mgmt.resource import ResourceManagementClient
    except ImportError:
        from azure.mgmt.resource.resources import ResourceManagementClient
except ImportError:
    print("ERROR: azure-identity, azure-mgmt-compute, azure-mgmt-network, azure-mgmt-resource required")
    print("Install: pip install azure-identity azure-mgmt-compute azure-mgmt-network azure-mgmt-resource")
    sys.exit(1)


def main():
    credential = cmp.get("credential", {})

    client_id = credential.get("azure_client_id", "")
    client_secret = credential.get("azure_client_secret", "")
    tenant_id = credential.get("azure_tenant_id", "")
    subscription_id = params.get("subscription_id") or credential.get("azure_subscription_id", "")

    vm_name = params.get("vm_name", "")
    location = params.get("location", "eastus")
    resource_group = params.get("resource_group_name", "")
    vm_size = params.get("vm_size", "Standard_B1s")
    admin_username = params.get("admin_username", "azureuser")
    ssh_public_key = params.get("ssh_public_key", "")
    image_publisher = params.get("image_publisher", "Canonical")
    image_offer = params.get("image_offer", "0001-com-ubuntu-server-jammy")
    image_sku = params.get("image_sku", "22_04-lts-gen2")
    os_disk_size_gb = int(params.get("os_disk_size_gb", "30"))
    os_disk_type = params.get("os_disk_type", "StandardSSD_LRS")
    assign_public_ip = str(params.get("assign_public_ip", "true")).lower() in ("true", "1", "yes")

    if not all([client_id, client_secret, tenant_id, subscription_id]):
        print("ERROR: Missing Azure credential fields.")
        print(f"  Available credential keys: {list(credential.keys())}")
        sys.exit(1)

    if not vm_name:
        print("ERROR: vm_name is required.")
        sys.exit(1)

    if not resource_group:
        print("ERROR: resource_group_name is required.")
        sys.exit(1)

    if not ssh_public_key:
        print("ERROR: ssh_public_key is required for Linux VMs.")
        sys.exit(1)

    print(f"[Azure] Provisioning VM '{vm_name}' in {location}")
    print(f"[Azure] Size: {vm_size}, Image: {image_publisher}/{image_offer}/{image_sku}")

    creds = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)

    resource_client = ResourceManagementClient(creds, subscription_id)
    network_client = NetworkManagementClient(creds, subscription_id)
    compute_client = ComputeManagementClient(creds, subscription_id)

    # Ensure resource group exists
    print(f"[Azure] Ensuring resource group '{resource_group}' exists...")
    resource_client.resource_groups.create_or_update(
        resource_group, {"location": location, "tags": {"ManagedBy": "cmp"}}
    )

    # Create VNet and Subnet
    vnet_name = f"{vm_name}-vnet"
    subnet_name = f"{vm_name}-subnet"
    print(f"[Azure] Creating VNet '{vnet_name}'...")
    vnet_poller = network_client.virtual_networks.begin_create_or_update(
        resource_group, vnet_name,
        {"location": location, "address_space": {"address_prefixes": ["10.0.0.0/16"]}}
    )
    vnet_poller.result()

    print(f"[Azure] Creating subnet '{subnet_name}'...")
    subnet_poller = network_client.subnets.begin_create_or_update(
        resource_group, vnet_name, subnet_name,
        {"address_prefix": "10.0.1.0/24"}
    )
    subnet = subnet_poller.result()

    # Public IP
    public_ip_address = None
    if assign_public_ip:
        pip_name = f"{vm_name}-pip"
        print(f"[Azure] Creating public IP '{pip_name}'...")
        pip_poller = network_client.public_ip_addresses.begin_create_or_update(
            resource_group, pip_name,
            {"location": location, "sku": {"name": "Standard"}, "public_ip_allocation_method": "Static"}
        )
        pip = pip_poller.result()
        public_ip_address = pip.ip_address

    # NIC
    nic_name = f"{vm_name}-nic"
    print(f"[Azure] Creating NIC '{nic_name}'...")
    ip_config = {
        "name": "internal",
        "subnet": {"id": subnet.id},
        "private_ip_address_allocation": "Dynamic",
    }
    if assign_public_ip:
        ip_config["public_ip_address"] = {"id": pip.id}

    nic_poller = network_client.network_interfaces.begin_create_or_update(
        resource_group, nic_name,
        {"location": location, "ip_configurations": [ip_config]}
    )
    nic = nic_poller.result()

    # Create VM
    print(f"[Azure] Creating VM '{vm_name}'...")
    vm_params = {
        "location": location,
        "hardwareProfile": {"vmSize": vm_size},
        "storageProfile": {
            "imageReference": {
                "publisher": image_publisher,
                "offer": image_offer,
                "sku": image_sku,
                "version": "latest",
            },
            "osDisk": {
                "createOption": "FromImage",
                "managedDisk": {"storageAccountType": os_disk_type},
                "diskSizeGB": os_disk_size_gb,
            },
        },
        "osProfile": {
            "computerName": vm_name,
            "adminUsername": admin_username,
            "linuxConfiguration": {
                "disablePasswordAuthentication": True,
                "ssh": {
                    "publicKeys": [
                        {
                            "path": f"/home/{admin_username}/.ssh/authorized_keys",
                            "keyData": ssh_public_key,
                        }
                    ]
                },
            },
        },
        "networkProfile": {
            "networkInterfaces": [{"id": nic.id}]
        },
        "tags": {"Name": vm_name, "ManagedBy": "cmp", "ProvisionedVia": "native-task"},
    }

    vm_poller = compute_client.virtual_machines.begin_create_or_update(resource_group, vm_name, vm_params)
    vm = vm_poller.result()

    # Get private IP
    nic_info = network_client.network_interfaces.get(resource_group, nic_name)
    private_ip = nic_info.ip_configurations[0].private_ip_address

    output = {
        "status": "success",
        "vm_id": vm.id,
        "vm_name": vm_name,
        "location": location,
        "vm_size": vm_size,
        "private_ip": private_ip or "N/A",
        "public_ip": public_ip_address or "N/A",
        "resource_group": resource_group,
        "provisioning_state": vm.provisioning_state,
    }
    print(json.dumps(output))


main()
'''

# ─────────────────────────────────────────────────────────────────────────────
# Form Schema
# ─────────────────────────────────────────────────────────────────────────────

AZURE_VM_FORM_FIELDS = [
    {
        "field_id": "vm_name",
        "label": "VM Name",
        "type": "string",
        "required": True,
        "placeholder": "my-azure-vm-01",
        "description": "Name for the Azure VM (alphanumeric and hyphens, max 64 chars)",
        "validation": {"pattern": "^[a-zA-Z][a-zA-Z0-9-]{0,63}$"},
    },
    {
        "field_id": "resource_group_name",
        "label": "Resource Group",
        "type": "string",
        "required": True,
        "placeholder": "my-app-rg",
        "description": "Azure resource group name (will be created if it doesn't exist)",
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
            {"label": "West US 3", "value": "westus3"},
            {"label": "Central US", "value": "centralus"},
            {"label": "North Europe", "value": "northeurope"},
            {"label": "West Europe", "value": "westeurope"},
            {"label": "UK South", "value": "uksouth"},
            {"label": "Southeast Asia", "value": "southeastasia"},
            {"label": "East Asia", "value": "eastasia"},
            {"label": "Australia East", "value": "australiaeast"},
            {"label": "Central India", "value": "centralindia"},
        ],
        "description": "Azure region for the VM deployment",
    },
    {
        "field_id": "vm_size",
        "label": "VM Size",
        "type": "select",
        "required": True,
        "default": "Standard_B1s",
        "options": [
            {"label": "Standard_B1s (1 vCPU, 1 GB) — Burstable", "value": "Standard_B1s"},
            {"label": "Standard_B2s (2 vCPU, 4 GB) — Burstable", "value": "Standard_B2s"},
            {"label": "Standard_B2ms (2 vCPU, 8 GB) — Burstable", "value": "Standard_B2ms"},
            {"label": "Standard_D2s_v5 (2 vCPU, 8 GB)", "value": "Standard_D2s_v5"},
            {"label": "Standard_D4s_v5 (4 vCPU, 16 GB)", "value": "Standard_D4s_v5"},
            {"label": "Standard_D8s_v5 (8 vCPU, 32 GB)", "value": "Standard_D8s_v5"},
            {"label": "Standard_E2s_v5 (2 vCPU, 16 GB) — Memory optimized", "value": "Standard_E2s_v5"},
            {"label": "Standard_F2s_v2 (2 vCPU, 4 GB) — Compute optimized", "value": "Standard_F2s_v2"},
        ],
        "description": "Azure VM size determining CPU and memory",
    },
    {
        "field_id": "image_publisher",
        "label": "Image Publisher",
        "type": "select",
        "required": True,
        "default": "Canonical",
        "options": [
            {"label": "Canonical (Ubuntu)", "value": "Canonical"},
            {"label": "RedHat (RHEL)", "value": "RedHat"},
            {"label": "Debian", "value": "Debian"},
            {"label": "OpenLogic (CentOS)", "value": "OpenLogic"},
            {"label": "SUSE", "value": "SUSE"},
        ],
        "description": "OS image publisher",
    },
    {
        "field_id": "image_offer",
        "label": "Image Offer",
        "type": "select",
        "required": True,
        "default": "0001-com-ubuntu-server-jammy",
        "options": [
            {"label": "Ubuntu Server 22.04 LTS", "value": "0001-com-ubuntu-server-jammy"},
            {"label": "Ubuntu Server 24.04 LTS", "value": "0001-com-ubuntu-server-noble"},
            {"label": "RHEL 9", "value": "RHEL"},
            {"label": "Debian 12", "value": "debian-12"},
            {"label": "CentOS 7", "value": "CentOS"},
        ],
        "description": "OS image offer",
    },
    {
        "field_id": "image_sku",
        "label": "Image SKU",
        "type": "select",
        "required": True,
        "default": "22_04-lts-gen2",
        "options": [
            {"label": "Ubuntu 22.04 LTS Gen2", "value": "22_04-lts-gen2"},
            {"label": "Ubuntu 24.04 LTS Gen2", "value": "24_04-lts-gen2"},
            {"label": "RHEL 9 Gen2", "value": "9_3"},
            {"label": "Debian 12 Gen2", "value": "12-gen2"},
            {"label": "CentOS 7.9", "value": "7_9"},
        ],
        "description": "OS image SKU version",
    },
    {
        "field_id": "os_disk_size_gb",
        "label": "OS Disk Size (GB)",
        "type": "number",
        "required": True,
        "default": 30,
        "validation": {"min": 30, "max": 2048},
        "description": "OS disk size in GB (min 30)",
    },
    {
        "field_id": "os_disk_type",
        "label": "OS Disk Type",
        "type": "select",
        "required": True,
        "default": "StandardSSD_LRS",
        "options": [
            {"label": "Standard SSD (recommended)", "value": "StandardSSD_LRS"},
            {"label": "Premium SSD", "value": "Premium_LRS"},
            {"label": "Standard HDD", "value": "Standard_LRS"},
        ],
        "description": "Managed disk type for the OS disk",
    },
    {
        "field_id": "admin_username",
        "label": "Admin Username",
        "type": "string",
        "required": True,
        "default": "azureuser",
        "placeholder": "azureuser",
        "description": "SSH admin username",
    },
    {
        "field_id": "ssh_public_key",
        "label": "SSH Public Key",
        "type": "textarea",
        "required": True,
        "placeholder": "ssh-rsa AAAA...",
        "description": "SSH public key for authentication (paste your ~/.ssh/id_rsa.pub content)",
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
    print("\n[1/7] Terraform template: Azure Virtual Machine...")
    name = "Azure Virtual Machine"
    payload = {
        "name": name,
        "description": (
            "Provisions an Azure Linux VM with configurable size, image, disk, "
            "VNet, and public IP. Uses azurerm provider ~> 3.0."
        ),
        "source_type": "inline",
        "source_config": {"hcl_content": AZURE_VM_HCL},
        "input_variables": [
            {"name": "subscription_id", "type": "string", "description": "Azure subscription ID", "required": True},
            {"name": "resource_group_name", "type": "string", "description": "Resource group name", "required": True},
            {"name": "create_resource_group", "type": "bool", "description": "Create resource group if not exists", "default": True},
            {"name": "location", "type": "string", "description": "Azure region", "default": "eastus", "required": True},
            {"name": "vm_name", "type": "string", "description": "VM name", "required": True},
            {"name": "vm_size", "type": "string", "description": "VM size", "default": "Standard_B1s", "required": True},
            {"name": "image_publisher", "type": "string", "description": "Image publisher", "default": "Canonical", "required": True},
            {"name": "image_offer", "type": "string", "description": "Image offer", "default": "0001-com-ubuntu-server-jammy", "required": True},
            {"name": "image_sku", "type": "string", "description": "Image SKU", "default": "22_04-lts-gen2", "required": True},
            {"name": "os_disk_size_gb", "type": "number", "description": "OS disk size (GB)", "default": 30},
            {"name": "os_disk_type", "type": "string", "description": "OS disk type", "default": "StandardSSD_LRS"},
            {"name": "admin_username", "type": "string", "description": "Admin username", "default": "azureuser", "required": True},
            {"name": "ssh_public_key", "type": "string", "description": "SSH public key", "required": True},
            {"name": "create_vnet", "type": "bool", "description": "Create new VNet", "default": True},
            {"name": "subnet_id", "type": "string", "description": "Existing subnet ID (if not creating VNet)", "default": ""},
            {"name": "assign_public_ip", "type": "bool", "description": "Assign public IP", "default": True},
        ],
        "output_definitions": [
            {"name": "vm_id", "description": "VM resource ID"},
            {"name": "vm_name", "description": "VM name"},
            {"name": "private_ip", "description": "Private IP address"},
            {"name": "public_ip", "description": "Public IP address"},
            {"name": "location", "description": "Azure region"},
        ],
        "required_providers": {"azurerm": "~> 3.0"},
        "supported_providers": ["azure"],
        "tags": ["azure", "vm", "compute", "day1", "seed"],
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
    print("\n[2/7] Native Python task: Azure VM Provision (Native SDK)...")
    name = "Azure VM Provision (Native SDK)"
    payload = {
        "name": name,
        "description": (
            "Provisions an Azure Linux VM using the Azure SDK for Python "
            "with service principal credentials. Creates RG, VNet, subnet, NIC, and VM."
        ),
        "language": "python",
        "code": NATIVE_TASK_CODE,
        "requirements": "azure-identity>=1.14.0\nazure-mgmt-compute>=30.0.0\nazure-mgmt-network>=25.0.0\nazure-mgmt-resource>=23.0.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "vm_name": {"type": "string", "description": "VM name"},
                "resource_group_name": {"type": "string", "description": "Resource group"},
                "location": {"type": "string", "description": "Azure region"},
                "vm_size": {"type": "string", "description": "VM size"},
                "image_publisher": {"type": "string", "description": "Image publisher"},
                "image_offer": {"type": "string", "description": "Image offer"},
                "image_sku": {"type": "string", "description": "Image SKU"},
                "os_disk_size_gb": {"type": "integer", "description": "OS disk size GB"},
                "os_disk_type": {"type": "string", "description": "OS disk type"},
                "admin_username": {"type": "string", "description": "Admin username"},
                "ssh_public_key": {"type": "string", "description": "SSH public key"},
                "assign_public_ip": {"type": "boolean", "description": "Assign public IP"},
            },
            "required": ["vm_name", "resource_group_name", "ssh_public_key"],
        },
        "tags": ["azure", "vm", "compute", "native", "sdk", "seed"],
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
    print("\n[3/7] Terraform workflow...")
    name = "azure-vm-terraform-provision"
    payload = {
        "name": name,
        "description": "Provisions an Azure VM using Terraform with the azurerm provider.",
        "steps": [
            {
                "step_id": "terraform_apply",
                "name": "Terraform Apply — Azure VM",
                "action": "terraform",
                "template_id": template_id,
                "inputs": {
                    "subscription_id": "{{credential.azure_subscription_id}}",
                    "resource_group_name": "{{form.resource_group_name}}",
                    "location": "{{form.location}}",
                    "vm_name": "{{form.vm_name}}",
                    "vm_size": "{{form.vm_size}}",
                    "image_publisher": "{{form.image_publisher}}",
                    "image_offer": "{{form.image_offer}}",
                    "image_sku": "{{form.image_sku}}",
                    "os_disk_size_gb": "{{form.os_disk_size_gb}}",
                    "os_disk_type": "{{form.os_disk_type}}",
                    "admin_username": "{{form.admin_username}}",
                    "ssh_public_key": "{{form.ssh_public_key}}",
                    "assign_public_ip": "{{form.assign_public_ip}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 900,
            }
        ],
        "tags": ["azure", "vm", "compute", "terraform", "seed"],
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
    print("\n[4/7] Native SDK workflow...")
    name = "azure-vm-native-provision"
    payload = {
        "name": name,
        "description": "Provisions an Azure VM using the Azure SDK for Python.",
        "steps": [
            {
                "step_id": "native_provision",
                "name": "Provision Azure VM (Native SDK)",
                "action": "run_task",
                "task_id": task_id,
                "inputs": {
                    "vm_name": "{{form.vm_name}}",
                    "resource_group_name": "{{form.resource_group_name}}",
                    "location": "{{form.location}}",
                    "vm_size": "{{form.vm_size}}",
                    "image_publisher": "{{form.image_publisher}}",
                    "image_offer": "{{form.image_offer}}",
                    "image_sku": "{{form.image_sku}}",
                    "os_disk_size_gb": "{{form.os_disk_size_gb}}",
                    "os_disk_type": "{{form.os_disk_type}}",
                    "admin_username": "{{form.admin_username}}",
                    "ssh_public_key": "{{form.ssh_public_key}}",
                    "assign_public_ip": "{{form.assign_public_ip}}",
                },
                "depends_on": [],
                "on_failure": "stop",
                "timeout_seconds": 600,
            }
        ],
        "tags": ["azure", "vm", "compute", "native", "sdk", "seed"],
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


def create_catalog(client: CMPClient, flow_id: str, name: str, description: str, tags: list) -> str:
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
        "form_schema": {"fields": AZURE_VM_FORM_FIELDS},
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
    parser = argparse.ArgumentParser(description="Seed Azure VM provisioning catalogs into CMP")
    parser.add_argument("--url", default=os.environ.get("CMP_URL", "http://localhost:8001"))
    parser.add_argument("--token", default=os.environ.get("CMP_TOKEN", ""))
    args = parser.parse_args()

    if not args.token:
        print("ERROR: No token provided. Use --token or set CMP_TOKEN env var.")
        sys.exit(1)

    client = CMPClient(args.url, args.token)

    print("=" * 70)
    print("  Azure VM Provisioning — CMP Seed Script")
    print("=" * 70)
    print(f"  Target: {args.url}")
    print(f"  Token:  {args.token[:20]}...")
    print("=" * 70)

    # Terraform
    print("\n" + "─" * 70)
    print("  TERRAFORM-BASED PROVISIONING (IaC)")
    print("─" * 70)

    template_id = create_terraform_template(client)
    tf_workflow_id = create_terraform_workflow(client, template_id)

    print("\n[5/7] Creating Terraform flow...")
    tf_flow_id = create_flow(client, tf_workflow_id, "azure-vm-terraform-flow",
                             "Flow for Azure VM provisioning via Terraform",
                             ["azure", "vm", "compute", "terraform", "seed"])

    print("\n[6/7] Creating Terraform catalog item...")
    tf_catalog_id = create_catalog(client, tf_flow_id,
        "Azure Virtual Machine (Terraform)",
        "Provision an Azure Linux VM using Terraform. Configure VM size, image, disk, "
        "and networking. Creates resource group, VNet, subnet, NIC, and VM.",
        ["azure", "vm", "compute", "terraform", "infrastructure", "seed"])

    # Native
    print("\n" + "─" * 70)
    print("  NATIVE SDK PROVISIONING (Python + azure-sdk)")
    print("─" * 70)

    task_id = create_native_task(client)
    native_workflow_id = create_native_workflow(client, task_id)

    print("\n[5/7] Creating native flow...")
    native_flow_id = create_flow(client, native_workflow_id, "azure-vm-native-flow",
                                 "Flow for Azure VM provisioning via Azure SDK",
                                 ["azure", "vm", "compute", "native", "sdk", "seed"])

    print("\n[7/7] Creating native catalog item...")
    native_catalog_id = create_catalog(client, native_flow_id,
        "Azure Virtual Machine (Native SDK)",
        "Provision an Azure Linux VM using the Azure SDK for Python. Uses service principal "
        "credentials from the selected Azure credential. Creates all networking resources automatically.",
        ["azure", "vm", "compute", "native", "sdk", "seed"])

    # Summary
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

  CREDENTIAL SECURITY:
    - Terraform: SP credentials → env vars (ARM_CLIENT_ID, etc.) → deleted after execution
    - Native:    SP credentials → ClientSecretCredential → never stored on disk
""")


if __name__ == "__main__":
    main()
