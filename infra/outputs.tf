output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.forge.id
}

output "static_ip" {
  description = "Elastic IP (static) — use this in DNS and the README public URL"
  value       = aws_eip.forge.public_ip
}

output "ssh_command" {
  description = "SSH into the server"
  value       = "ssh ubuntu@${aws_eip.forge.public_ip}"
}

output "forge_api_url" {
  description = "Forge API base URL — paste into README and config.yaml registry.url"
  value       = "http://${aws_eip.forge.public_ip}:8080"
}
