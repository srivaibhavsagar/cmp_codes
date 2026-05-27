module "web_server" {
  source = "./modules/aws-ec2-instance"

  name          = "cmp-web-server"
  ami_id        = "ami-0c02fb55956c7d316"  # Amazon Linux 2023
  instance_type = "t3.small"
  vpc_id        = "vpc-abc123"
  subnet_id     = "subnet-def456"
  key_name      = "my-key"

  allowed_ssh_cidrs = ["10.0.0.0/8"]
  additional_ports  = [80, 443]

  tags = {
    Environment = "dev"
    ManagedBy   = "cmp"
    Tenant      = "default"
  }
}
