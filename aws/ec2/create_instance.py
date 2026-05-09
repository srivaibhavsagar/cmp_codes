import boto3, json

cred = cmp.get("credential") or {}
if not cred or cred.get("provider") != "aws":
    raise ValueError(
        "AWS credential is required. Select an AWS credential when submitting this catalog."
    )

# Credentials are reference-only in the CMP payload (credential_id, provider, region).
# Configure AWS credentials via environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
# or attach an IAM instance profile to the backend container.
region = params.get("region") or cred.get("region") or "us-east-1"
instance_name = params.get("instance_name", "cmp-instance")
instance_type = params.get("instance_type", "t3.micro")
ami_id = params.get("ami_id") or "ami-0c55b159cbfafe1f0"

print(cred["temp_access_key_id"],cred["temp_access_key_id"],cred["temp_session_token"])
session = boto3.Session(

    aws_access_key_id=cred["temp_access_key_id"],

    aws_secret_access_key=cred["temp_access_key_id"],

    aws_session_token=cred["temp_session_token"],

    region_name=region
)

ec2 = session.client("ec2")

resp = ec2.run_instances(
    ImageId=ami_id,
    InstanceType=instance_type,
    MinCount=1,
    MaxCount=1,
    TagSpecifications=[{
        "ResourceType": "instance",
        "Tags": [
            {"Key": "Name", "Value": instance_name},
            {"Key": "ManagedBy", "Value": "CMP"},
            {"Key": "Group", "Value": params.get("group")},
        ],
    }],
)

instance = resp["Instances"][0]
instance_id = instance["InstanceId"]
public_ip = instance.get("PublicIpAddress", "")

print(json.dumps({
    "instance_id": instance_id,
    "instance_name": instance_name,
    "instance_type": instance_type,
    "region": region,
    "ami_id": ami_id,
    "public_ip": public_ip,
    "state": instance["State"]["Name"],
    "message": f"EC2 instance {instance_id} ({instance_name}) launched successfully",
}))
