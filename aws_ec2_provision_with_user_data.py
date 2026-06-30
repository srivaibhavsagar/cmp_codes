"""
AWS EC2 Provisioning — Native Python Task (Simplified with cmp["user_data"])

Same as aws_ec2_provision_with_agent.py but uses the pre-built cmp["user_data"]
which CMP assembles centrally. It includes:
  - SSH public keys (configured by admin in Settings)
  - CMP Agent installation
  - Any future platform bootstrap

The task author doesn't need to know about agent tokens or SSH keys.
They just pass cmp["user_data"] to the VM.

CMP injects context as:
    cmp["credential"]["aws_access_key_id"]
    cmp["credential"]["aws_secret_access_key"]
    cmp["credential"]["aws_session_token"]
    cmp["user_data"]                            — ready-to-use cloud-init string
    params["instance_name"]                     — form data / step inputs
"""
import json
import sys

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
    root_volume_size = int(params.get("root_volume_size_gb", "20"))
    root_volume_type = params.get("root_volume_type", "gp3")
    assign_public_ip = str(params.get("assign_public_ip", "true")).lower() in ("true", "1", "yes")

    # CMP provides ready-to-use user_data (SSH keys + agent install + future bootstrap)
    user_data = cmp.get("user_data", "")
    if user_data:
        print(f"[CMP] user_data provided ({len(user_data)} bytes)")
    else:
        print("[CMP] WARNING: cmp['user_data'] is empty. Check admin Settings → Provisioning tab.")
        # Fallback: try to build from cmp["agent"] if available
        agent = cmp.get("agent", {})
        if agent.get("token"):
            print("[CMP] Falling back to cmp['agent'] for user_data")
            tenant_id = cmp.get("execution", {}).get("tenant_id", "default")
            user_data = f"""#!/bin/bash
sleep 10
# Resolve actual instance ID from EC2 metadata (IMDSv2)
IMDS_TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null)
CMP_RESOURCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
if [ -z "$CMP_RESOURCE_ID" ]; then CMP_RESOURCE_ID="{params.get('instance_name', 'unknown')}"; fi
curl -sSL {agent['install_url']} | bash -s -- --endpoint {agent['endpoint']} --token {agent['token']} --resource-id $CMP_RESOURCE_ID --tenant-id {tenant_id}
"""

    if not aws_access_key or not aws_secret_key:
        print("ERROR: No AWS credentials in credential context.")
        sys.exit(1)

    if not instance_name or not ami_id:
        print("ERROR: instance_name and ami_id are required.")
        sys.exit(1)

    print(f"[AWS] Provisioning EC2 instance '{instance_name}' in {region}")
    print(f"[AWS] Type: {instance_type}, AMI: {ami_id}, Volume: {root_volume_size}GB {root_volume_type}")
    if user_data:
        print("[AWS] CMP user_data will be applied (SSH keys + agent)")

    session = boto3.Session(
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        aws_session_token=aws_session_token or None,
        region_name=region,
    )
    ec2 = session.resource("ec2")

    run_kwargs = {
        "ImageId": ami_id,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "BlockDeviceMappings": [{
            "DeviceName": "/dev/xvda",
            "Ebs": {
                "VolumeSize": root_volume_size,
                "VolumeType": root_volume_type,
                "Encrypted": True,
                "DeleteOnTermination": True,
            },
        }],
        "TagSpecifications": [{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": instance_name},
                {"Key": "ManagedBy", "Value": "cmp"},
                {"Key": "ProvisionedVia", "Value": "native-task"},
            ],
        }],
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled"},
    }

    if user_data:
        run_kwargs["UserData"] = user_data

    if subnet_id:
        run_kwargs["SubnetId"] = subnet_id

    if subnet_id and assign_public_ip:
        run_kwargs.pop("SubnetId", None)
        run_kwargs["NetworkInterfaces"] = [{
            "DeviceIndex": 0,
            "SubnetId": subnet_id,
            "AssociatePublicIpAddress": assign_public_ip,
        }]

    try:
        print("[AWS] Launching instance...")
        instances = ec2.create_instances(**run_kwargs)
        instance = instances[0]
        instance_id = instance.id
        print(f"[AWS] Instance launched: {instance_id}")

        instance.wait_until_running()
        instance.reload()

        output = {
            "status": "success",
            "instance_id": instance_id,
            "instance_name": instance_name,
            "region": region,
            "availability_zone": instance.placement.get("AvailabilityZone", "N/A"),
            "instance_type": instance_type,
            "private_ip": instance.private_ip_address or "N/A",
            "public_ip": instance.public_ip_address or "N/A",
            "instance_state": instance.state["Name"],
        }
        print(json.dumps(output))

    except ClientError as e:
        print(f"ERROR: AWS API error: {e.response['Error']['Message']}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected error: {e}")
        sys.exit(1)


main()
