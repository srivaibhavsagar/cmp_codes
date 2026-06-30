variable "region" {
  type = string
}

variable "ami_id" {
  type = string
}

variable "instance_name" {
  type = string
}

variable "instance_type" {
  type    = string
  default = "t3.micro"
}

variable "subnet_id" {
  type = string
}

variable "key_name" {
  type    = string
  default = null
}

variable "cmp_user_data" {
  description = "Pre-built cloud-init user_data from CMP (SSH keys + agent install). Auto-injected by CMP."
  type        = string
  default     = ""
}

variable "security_group_ids" {
  type    = list(string)
  default = []
}

variable "assign_public_ip" {
  description = "Assign public IP. Required for CMP agent to reach the platform."
  type    = bool
  default = true
}

variable "root_volume_size_gb" {
  type    = number
  default = 20
}

variable "root_volume_type" {
  type    = string
  default = "gp3"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# CMP Agent variables (auto-injected by CMP during execution)
variable "cmp_agent_install_url" {
  description = "URL to the CMP agent install script"
  type        = string
  default     = ""
}

variable "cmp_agent_endpoint" {
  description = "CMP agent API endpoint"
  type        = string
  default     = ""
}

variable "cmp_agent_token" {
  description = "One-time registration token for the CMP agent"
  type        = string
  default     = ""
  sensitive   = true
}

variable "cmp_agent_tenant_id" {
  description = "CMP tenant ID"
  type        = string
  default     = "default"
}

provider "aws" {
  region = var.region
}

locals {
  install_agent = var.cmp_agent_token != "" && var.cmp_agent_install_url != ""

  # Prefer cmp_user_data (pre-built cloud-init from CMP with SSH keys + agent).
  # Fall back to building agent-only user_data from individual cmp_agent_* variables.
  agent_user_data = local.install_agent ? join("\n", [
    "#!/bin/bash",
    "# Wait for network and metadata service",
    "sleep 10",
    "",
    "# Resolve actual instance ID from EC2 metadata (IMDSv2)",
    "IMDS_TOKEN=$(curl -s -X PUT \"http://169.254.169.254/latest/api/token\" -H \"X-aws-ec2-metadata-token-ttl-seconds: 60\")",
    "INSTANCE_ID=$(curl -s -H \"X-aws-ec2-metadata-token: $IMDS_TOKEN\" http://169.254.169.254/latest/meta-data/instance-id)",
    "",
    "# Install CMP monitoring agent",
    "curl -sSL ${var.cmp_agent_install_url} | bash -s -- --endpoint ${var.cmp_agent_endpoint} --token ${var.cmp_agent_token} --resource-id $INSTANCE_ID --tenant-id ${var.cmp_agent_tenant_id}",
  ]) : ""

  # Use cmp_user_data if provided (includes SSH keys + agent + custom commands),
  # otherwise fall back to agent-only user_data
  effective_user_data = var.cmp_user_data != "" ? var.cmp_user_data : local.agent_user_data
}

resource "aws_instance" "vm" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  key_name               = var.key_name
  vpc_security_group_ids = var.security_group_ids

  associate_public_ip_address = var.assign_public_ip

  user_data = local.effective_user_data != "" ? local.effective_user_data : null

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = var.root_volume_type
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"
  }

  tags = merge(var.tags, {
    Name      = var.instance_name
    ManagedBy = "CMP"
    CMPAgent  = local.install_agent ? "true" : "false"
  })
}

output "instance_id" {
  description = "EC2 Instance ID"
  value       = aws_instance.vm.id
}

output "instance_name" {
  description = "Instance Name"
  value       = var.instance_name
}

output "private_ip" {
  description = "Private IP"
  value       = aws_instance.vm.private_ip
}

output "public_ip" {
  description = "Public IP"
  value       = aws_instance.vm.public_ip
}

output "availability_zone" {
  description = "Availability Zone"
  value       = aws_instance.vm.availability_zone
}

output "instance_state" {
  description = "Instance State"
  value       = aws_instance.vm.instance_state
}
