"""
Azure VM Provisioning — Native Python Task (with cmp["user_data"])

Provisions an Azure Linux VM with VNet, Subnet, NIC, and optional Public IP.
Uses cmp["user_data"] for SSH keys + CMP agent installation.

CMP injects context as:
    cmp["credential"]["azure_client_id"]
    cmp["credential"]["azure_client_secret"]
    cmp["credential"]["azure_tenant_id"]
    cmp["credential"]["azure_subscription_id"]
    cmp["user_data"]                            — ready-to-use cloud-init string
    params["vm_name"]                           — form data / step inputs
"""
import json
import sys
import base64

from azure.identity import ClientSecretCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.compute.models import (
    VirtualMachine,
    HardwareProfile,
    StorageProfile,
    ImageReference,
    OSDisk,
    DiskCreateOptionTypes,
    OSProfile,
    LinuxConfiguration,
    SshConfiguration,
    SshPublicKey,
    NetworkProfile,
    NetworkInterfaceReference,
)

# -------------------------------------------------------------------
# Azure Credentials from CMP
# -------------------------------------------------------------------
credential = cmp.get("credential", {})
client_id = credential.get("azure_client_id")
client_secret = credential.get("azure_client_secret")
tenant_id = credential.get("azure_tenant_id")
subscription_id = credential.get("azure_subscription_id")

if not all([client_id, client_secret, tenant_id, subscription_id]):
    print("ERROR: Missing Azure credentials in CMP")
    sys.exit(1)

# -------------------------------------------------------------------
# User Inputs
# -------------------------------------------------------------------
vm_name = params.get("vm_name")
resource_group = params.get("resource_group_name")
location = params.get("location", "uksouth")
vm_size = params.get("vm_size", "Standard_B2s")
admin_username = params.get("admin_username", "azureuser")
disk_size_gb = int(params.get("disk_size_gb", "30"))
assign_public_ip = str(params.get("assign_public_ip", "true")).lower() in ("true", "1", "yes")

if not vm_name:
    print("ERROR: vm_name is required")
    sys.exit(1)
if not resource_group:
    print("ERROR: resource_group_name is required")
    sys.exit(1)

# -------------------------------------------------------------------
# CMP user_data (cloud-init with SSH keys + agent install)
# -------------------------------------------------------------------
user_data = cmp.get("user_data", "")
if user_data:
    print(f"[CMP] user_data provided ({len(user_data)} bytes) — SSH keys + agent will be injected via cloud-init")
else:
    print("[CMP] WARNING: cmp['user_data'] is empty")

# Extract SSH public key from user_data for Azure os_profile
# Azure requires at least one SSH key in os_profile for password-less auth.
# We parse it from the cloud-init ssh_authorized_keys section.
ssh_keys_for_os_profile = []
if user_data:
    for line in user_data.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- ssh-") or stripped.startswith("- ecdsa-"):
            key_data = stripped.lstrip("- ").strip()
            ssh_keys_for_os_profile.append(key_data)

# Fallback: if no SSH key in user_data, check params
if not ssh_keys_for_os_profile:
    param_key = params.get("ssh_public_key", "")
    if param_key:
        ssh_keys_for_os_profile.append(param_key)

if not ssh_keys_for_os_profile:
    print("ERROR: No SSH public key available. Configure in Admin Settings → Provisioning or pass ssh_public_key param.")
    sys.exit(1)

# -------------------------------------------------------------------
# Azure Clients
# -------------------------------------------------------------------
creds = ClientSecretCredential(
    tenant_id=tenant_id,
    client_id=client_id,
    client_secret=client_secret,
)
resource_client = ResourceManagementClient(creds, subscription_id)
network_client = NetworkManagementClient(creds, subscription_id)
compute_client = ComputeManagementClient(creds, subscription_id)

# -------------------------------------------------------------------
# Resource Group
# -------------------------------------------------------------------
print(f"[Azure] Creating/updating Resource Group '{resource_group}' in {location}")
resource_client.resource_groups.create_or_update(
    resource_group, {"location": location}
)

# -------------------------------------------------------------------
# VNet
# -------------------------------------------------------------------
vnet_name = f"{vm_name}-vnet"
print(f"[Azure] Creating VNet '{vnet_name}'")
vnet = network_client.virtual_networks.begin_create_or_update(
    resource_group,
    vnet_name,
    {"location": location, "address_space": {"address_prefixes": ["10.0.0.0/16"]}},
).result()

# -------------------------------------------------------------------
# Subnet
# -------------------------------------------------------------------
subnet_name = f"{vm_name}-subnet"
print(f"[Azure] Creating Subnet '{subnet_name}'")
subnet = network_client.subnets.begin_create_or_update(
    resource_group, vnet_name, subnet_name, {"address_prefix": "10.0.1.0/24"}
).result()

# -------------------------------------------------------------------
# Public IP (optional)
# -------------------------------------------------------------------
public_ip_id = None
if assign_public_ip:
    pip_name = f"{vm_name}-pip"
    print(f"[Azure] Creating Public IP '{pip_name}'")
    pip = network_client.public_ip_addresses.begin_create_or_update(
        resource_group,
        pip_name,
        {
            "location": location,
            "sku": {"name": "Standard"},
            "public_ip_allocation_method": "Static",
        },
    ).result()
    public_ip_id = pip.id

# -------------------------------------------------------------------
# NIC
# -------------------------------------------------------------------
nic_name = f"{vm_name}-nic"
ip_config = {
    "name": "ipconfig1",
    "subnet": {"id": subnet.id},
}
if public_ip_id:
    ip_config["public_ip_address"] = {"id": public_ip_id}

print(f"[Azure] Creating NIC '{nic_name}'")
nic = network_client.network_interfaces.begin_create_or_update(
    resource_group,
    nic_name,
    {"location": location, "ip_configurations": [ip_config]},
).result()

# -------------------------------------------------------------------
# VM
# -------------------------------------------------------------------
print(f"[Azure] Creating VM '{vm_name}' (size: {vm_size})")

vm_parameters = VirtualMachine(
    location=location,
    hardware_profile=HardwareProfile(vm_size=vm_size),
    storage_profile=StorageProfile(
        image_reference=ImageReference(
            publisher="Canonical",
            offer="0001-com-ubuntu-server-jammy",
            sku="22_04-lts-gen2",
            version="latest",
        ),
        os_disk=OSDisk(
            create_option=DiskCreateOptionTypes.FROM_IMAGE,
            disk_size_gb=disk_size_gb,
        ),
    ),
    os_profile=OSProfile(
        computer_name=vm_name,
        admin_username=admin_username,
        linux_configuration=LinuxConfiguration(
            disable_password_authentication=True,
            ssh=SshConfiguration(
                public_keys=[
                    SshPublicKey(
                        path=f"/home/{admin_username}/.ssh/authorized_keys",
                        key_data=key,
                    )
                    for key in ssh_keys_for_os_profile
                ]
            ),
        ),
        # Pass full cloud-init as custom_data (base64-encoded)
        custom_data=base64.b64encode(user_data.encode()).decode() if user_data else None,
    ),
    network_profile=NetworkProfile(
        network_interfaces=[NetworkInterfaceReference(id=nic.id, primary=True)]
    ),
    tags={"Name": vm_name, "ManagedBy": "CMP"},
)

vm = compute_client.virtual_machines.begin_create_or_update(
    resource_group, vm_name, vm_parameters
).result()

# -------------------------------------------------------------------
# Output
# -------------------------------------------------------------------
nic_details = network_client.network_interfaces.get(resource_group, nic_name)
private_ip = nic_details.ip_configurations[0].private_ip_address

# Get public IP if assigned
public_ip = "N/A"
if assign_public_ip:
    pip_details = network_client.public_ip_addresses.get(resource_group, pip_name)
    public_ip = pip_details.ip_address or "Allocating..."

print(f"[Azure] VM created successfully!")
output = {
    "status": "success",
    "vm_name": vm_name,
    "vm_id": vm.id,
    "private_ip": private_ip,
    "public_ip": public_ip,
    "location": location,
    "resource_group": resource_group,
    "vm_size": vm_size,
}
print(json.dumps(output))
