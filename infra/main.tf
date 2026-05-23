terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# ---------------------------------------------------------------------------
# Key Pair — uploads ~/.ssh/id_ed25519.pub to AWS
# ---------------------------------------------------------------------------
resource "aws_key_pair" "forge" {
  key_name   = "${var.instance_name}-key"
  public_key = file("~/.ssh/id_ed25519.pub")
}

# ---------------------------------------------------------------------------
# AMI — latest Ubuntu 22.04 LTS (Canonical)
# Resolves the correct AMI ID per region automatically.
# ---------------------------------------------------------------------------
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# ---------------------------------------------------------------------------
# SSM Parameter — Slack webhook stored as SecureString (KMS-encrypted)
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "slack_webhook" {
  name        = "/forge/slack_webhook_url"
  type        = "SecureString"
  value       = var.slack_webhook_url
  description = "Forge CI/CD — Slack incoming webhook URL"

  tags = {
    Name = "${var.instance_name}-slack-webhook"
  }
}

# ---------------------------------------------------------------------------
# IAM — EC2 instance role with least-privilege SSM read access
# ---------------------------------------------------------------------------
resource "aws_iam_role" "forge" {
  name = "${var.instance_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "forge_ssm" {
  name = "${var.instance_name}-ssm-read"
  role = aws_iam_role.forge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = aws_ssm_parameter.slack_webhook.arn
    }]
  })
}

resource "aws_iam_instance_profile" "forge" {
  name = "${var.instance_name}-profile"
  role = aws_iam_role.forge.name
}

# ---------------------------------------------------------------------------
# Security Group — allow SSH + Forge API inbound; all outbound
# ---------------------------------------------------------------------------
resource "aws_security_group" "forge" {
  name        = "${var.instance_name}-sg"
  description = "Forge CI/CD platform"

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Forge API"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.instance_name}-sg"
  }
}

# ---------------------------------------------------------------------------
# EC2 instance — 4 vCPU / 16 GB RAM / 40 GB root EBS
# t3.xlarge: cost-effective general-purpose; burstable CPU suits CI workloads.
# The Docker socket is mounted into the forge-api container so it can spawn
# job containers — instance must therefore run Docker directly (not ECS/Fargate).
# ---------------------------------------------------------------------------
resource "aws_instance" "forge" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.xlarge"
  key_name               = aws_key_pair.forge.key_name
  vpc_security_group_ids = [aws_security_group.forge.id]
  iam_instance_profile   = aws_iam_instance_profile.forge.name
  user_data              = file("${path.module}/provision.sh")

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 40
    delete_on_termination = true
    encrypted             = true
  }

  tags = {
    Name = var.instance_name
  }
}

# ---------------------------------------------------------------------------
# Elastic IP — static public IP that survives instance stop/start
# ---------------------------------------------------------------------------
resource "aws_eip" "forge" {
  instance = aws_instance.forge.id
  domain   = "vpc"

  tags = {
    Name = "${var.instance_name}-eip"
  }
}
