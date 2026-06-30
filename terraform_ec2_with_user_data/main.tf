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

variable "security_group_ids" {
  type    = list(string)
  default = []
}

variable "assign_public_ip" {
  description = "Assign public IP. Required for CMP agent to reach the platform."
  type        = bool
  default     = true
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

# CMP auto-injected — contains SSH keys + agent install + custom commands
variable "cmp_user_data" {
  description = "Pre-built cloud-init user_data from CMP (auto-injected by orchestrator). Includes SSH keys, agent install, and custom commands configured in admin settings."
  type        = string
  default     = ""
}

provider "aws" {
  region = var.region
}

resource "aws_instance" "vm" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  key_name               = var.key_name
  vpc_security_group_ids = var.security_group_ids

  associate_public_ip_address = var.assign_public_ip

  user_data = var.cmp_user_data != "" ? var.cmp_user_data : null

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
