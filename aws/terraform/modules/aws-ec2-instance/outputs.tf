# modules/aws-ec2-instance/outputs.tf

output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.this.id
}

output "private_ip" {
  description = "Private IP address"
  value       = aws_instance.this.private_ip
}

output "public_ip" {
  description = "Public IP address (if assigned)"
  value       = aws_instance.this.public_ip
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.instance.id
}
