variable "volumes" {
  type = list(object({
    size        = number
    type        = string
    device_name = string
  }))
  description = "Additional EBS volumes to attach"
  default     = []
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type"
  default     = "t3.micro"
}
variable "sequence" {
  type        = string
  description = "EC2 instance Name sequence"
  default     = "9999"
}
variable "ami" {
  type        = string
  description = "AMI ID"
  default = ""
}

variable "key_name" {
  type        = string
  description = "SSH key pair name"
  default = ""
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_ebs_volume" "extra" {
  for_each          = { for idx, vol in var.volumes : idx => vol }
  availability_zone = aws_instance.main.availability_zone
  size              = each.value.size
  type              = each.value.type

  tags = {
    Name      = "cmp-volume-${each.key}"
    ManagedBy = "CMP"
  }
}

resource "aws_volume_attachment" "extra" {
  for_each    = { for idx, vol in var.volumes : idx => vol }
  device_name = each.value.device_name
  volume_id   = aws_ebs_volume.extra[each.key].id
  instance_id = aws_instance.main.id
}

resource "aws_instance" "main" {
  ami           = var.ami
  instance_type = var.instance_type
  key_name      = var.key_name

  tags = {
    Name      = "cmp-managed-${sequence}"
    ManagedBy = "CMP"
    Createdfrom = "terraform"
  }
}

output "instance_id" {
  value = aws_instance.main.id
}

output "public_ip" {
  value = aws_instance.main.public_ip
}
output "resource_name" {
  value = aws_instance.main.tags.Name
}
