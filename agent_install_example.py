"""
Example: EC2 Provisioning Task with CMP Agent Installation
===========================================================

This sample task demonstrates how to provision an EC2 instance and
automatically install the CMP monitoring agent using the cmp["agent"]
context that is injected into every task execution.

After provisioning, the agent will:
- Register itself with CMP
- Report CPU, memory, disk, network metrics every 60 seconds
- Display metrics on the Resource Detail page → "System Metrics" tab

Usage in CMP:
- Create a Task with this code (language: python)
- Link it in a Workflow → Flow → Catalog
- When a user requests the catalog, the VM is created with the agent pre-installed
"""

import json
import boto3

# CMP context is automatically injected
# cmp["params"]      – form data submitted by the user
# cmp["credential"]  – AWS temp credentials
# cmp["agent"]       – agent registration info (token, endpoint, install_url)

# Get AWS credentials from CMP context
aws_creds = {
    "aws_access_key_id": cmp["credential"]["aws_access_key_id"],
    "aws_secret_access_key": cmp["credential"]["aws_secret_access_key"],
    "aws_session_token": cmp["credential"]["aws_session_token"],
}
region = cmp["credential"]["region"] or params.get("region", "us-east-1")

# Get form inputs
instance_type = params.get("instance_type", "t3.micro")
ami_id = params.get("ami_id", "ami-0c02fb55956c7d316")  # Amazon Linux 2023
key_name = params.get("key_name", "")
subnet_id = params.get("subnet_id", "")
security_group_ids = params.get("security_group_ids", [])
instance_name = params.get("instance_name", f"cmp-instance-{cmp['execution']['execution_id'][:8]}")

# Build user_data script that installs the CMP agent
# The cmp["agent"] context provides everything needed:
#   cmp["agent"]["token"]       – one-time registration token (valid 1 hour)
#   cmp["agent"]["endpoint"]    – CMP agent API base URL
#   cmp["agent"]["install_url"] – URL to the install script
#   cmp["agent"]["resource_id"] – resource identifier for this execution
agent_info = cmp.get("agent", {})

user_data_script = f"""#!/bin/bash
# === User's custom setup ===
yum update -y
yum install -y httpd
systemctl start httpd
systemctl enable httpd

# === CMP Agent Installation ===
# This installs a lightweight monitoring agent that reports system metrics
# (CPU, memory, disk, network) to CMP every 60 seconds.
curl -sSL {agent_info.get('install_url', '')} | bash -s -- \\
  --endpoint {agent_info.get('endpoint', '')} \\
  --token {agent_info.get('token', '')} \\
  --resource-id {{INSTANCE_ID}} \\
  --tenant-id {cmp['execution'].get('tenant_id', 'default')}
"""

# Create EC2 instance
ec2 = boto3.client("ec2", region_name=region, **aws_creds)

run_kwargs = {
    "ImageId": ami_id,
    "InstanceType": instance_type,
    "MinCount": 1,
    "MaxCount": 1,
    "TagSpecifications": [
        {
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": instance_name},
                {"Key": "CreatedBy", "Value": "CMP"},
                {"Key": "ExecutionId", "Value": cmp["execution"]["execution_id"]},
            ],
        }
    ],
}

if key_name:
    run_kwargs["KeyName"] = key_name
if subnet_id:
    run_kwargs["SubnetId"] = subnet_id
if security_group_ids:
    run_kwargs["SecurityGroupIds"] = security_group_ids if isinstance(security_group_ids, list) else [security_group_ids]

# Launch the instance first to get instance_id, then update user_data with it
response = ec2.run_instances(**run_kwargs)
instance = response["Instances"][0]
instance_id = instance["InstanceId"]

# Now set the user_data with the actual instance_id
# (We use a two-step approach because we need the instance_id in the agent config)
final_user_data = user_data_script.replace("{INSTANCE_ID}", instance_id)

# For a real implementation, you'd either:
# 1. Use instance_id as the resource_id directly (already done via cmp["agent"]["resource_id"])
# 2. Or pass instance_id via EC2 instance tags and have the agent read it

# Wait for instance to be running
waiter = ec2.get_waiter("instance_running")
waiter.wait(InstanceIds=[instance_id])

# Get final instance details
describe_response = ec2.describe_instances(InstanceIds=[instance_id])
final_instance = describe_response["Reservations"][0]["Instances"][0]

# Output the result (CMP reads this as JSON from stdout)
output = {
    "instance_id": instance_id,
    "resource_id": instance_id,
    "resource_name": instance_name,
    "resource_type": "ec2",
    "public_ip": final_instance.get("PublicIpAddress"),
    "private_ip": final_instance.get("PrivateIpAddress"),
    "status": final_instance["State"]["Name"],
    "region": region,
    "instance_type": instance_type,
    "agent_installed": bool(agent_info.get("token")),
}

print(json.dumps(output))
