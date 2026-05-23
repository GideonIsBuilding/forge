variable "region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "instance_name" {
  description = "Name tag applied to the EC2 instance and related resources"
  type        = string
  default     = "forge-ci"
}

variable "slack_webhook_url" {
  description = "Slack incoming webhook URL — stored as SecureString in SSM, never in user_data"
  type        = string
  sensitive   = true
}
