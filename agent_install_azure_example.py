"""
Example: Azure VM Provisioning Task with CMP Agent Installation
================================================================

This sample task demonstrates provisioning an Azure VM and
automatically installing the CMP monitoring agent via custom_data.

The agent reports CPU, memory, disk, and network metrics to CMP
and they appear on the Resource Detail → System Metrics tab.
"""

import json
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

# CMP context
cred_info = cmp["credential"]
agent_info = cmp.get("agent", {})
execution_id = cmp["execution"]["execution_id"]
tenant_id = cmp["execution"].get("tenant_id", "default")

# Azure credentials from CMP
credential = ClientSecretCredential(
    tenant_id=cred_info["azure_tenant_id"],
    client_id=cred_info["azure_client_id"],
    client_secret=cred_info["azure_client_secret"],
)
subscription_id = cred_info["azure_subscription_id"]

# Form inputs
vm_name = params.get("vm_name", f"cmp-vm-{execution_id[:8]}")
resource_group = params.get("resource_group", "cmp-resources")
location = params.get("location", cred_info.get("region", "eastus"))
vm_size = params.get("vm_size", "Standard_B2s")
admin_username = params.get("admin_username", "azureuser")
admin_password = params.get("admin_password", "")
ssh_key = params.get("ssh_public_key", "")
image_publisher = params.get("image_publisher", "Canonical")
image_offer = params.get("image_offer", "0001-com-ubuntu-server-jammy")
image_sku = params.get("image_sku", "22_04-lts-gen2")

# Build cloud-init script with CMP agent installation
cloud_init = f"""#!/bin/bash
# === User setup ===
apt-get update -y
apt-get install -y nginx

# === CMP Agent Installation ===
curl -sSL {agent_info.get('install_url', '')} | bash -s -- \\
  --endpoint {agent_info.get('endpoint', '')} \\
  --token {agent_info.get('token', '')} \\
  --resource-id {vm_name} \\
  --tenant-id {tenant_id}
"""

import base64
custom_data = base64.b64encode(cloud_init.encode()).decode()

# Create the VM
compute_client = ComputeManagementClient(credential, subscription_id)

vm_parameters = {
    "location": location,
    "tags": {
        "CreatedBy": "CMP",
        "ExecutionId": execution_id,
    },
    "hardware_profile": {"vm_size": vm_size},
    "storage_profile": {
        "image_reference": {
            "publisher": image_publisher,
            "offer": image_offer,
            "sku": image_sku,
            "version": "latest",
        },
        "os_disk": {
            "create_option": "FromImage",
            "managed_disk": {"storage_account_type": "Standard_LRS"},
        },
    },
    "os_profile": {
        "computer_name": vm_name,
        "admin_username": admin_username,
        "custom_data": custom_data,
    },
    "network_profile": {
        "network_interfaces": [
            {"id": params.get("nic_id", ""), "primary": True}
        ]
    },
}

# Add auth
if ssh_key:
    vm_parameters["os_profile"]["linux_configuration"] = {
        "disable_password_authentication": True,
        "ssh": {
            "public_keys": [
                {
                    "path": f"/home/{admin_username}/.ssh/authorized_keys",
                    "key_data": ssh_key,
                }
            ]
        },
    }
elif admin_password:
    vm_parameters["os_profile"]["admin_password"] = admin_password

# Create VM
poller = compute_client.virtual_machines.begin_create_or_update(
    resource_group, vm_name, vm_parameters
)
vm_result = poller.result()

# Output result
output = {
    "instance_id": vm_result.vm_id or vm_name,
    "resource_id": vm_name,
    "resource_name": vm_name,
    "resource_type": "vm",
    "location": location,
    "vm_size": vm_size,
    "provisioning_state": vm_result.provisioning_state,
    "agent_installed": bool(agent_info.get("token")),
}

print(json.dumps(output))
