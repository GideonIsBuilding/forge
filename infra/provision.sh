#!/usr/bin/env bash
# =============================================================================
# Forge CI/CD Platform — Server Provisioning Script
# Target OS : Ubuntu 22.04 LTS
# Runs as   : root via cloud-init user_data on first boot
# =============================================================================
set -euo pipefail

LOG=/var/log/forge-provision.log
exec > >(tee -a "$LOG") 2>&1
echo "[forge-provision] started at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# -----------------------------------------------------------------------------
# 1. System packages
# -----------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq \
  ca-certificates \
  curl \
  git \
  gnupg \
  htop \
  jq \
  lsb-release \
  ufw \
  fail2ban \
  unzip \
  wget

echo "[forge-provision] base packages installed"

# -----------------------------------------------------------------------------
# 2. Docker CE
# Installs the official Docker Engine (not the snap or distro package).
# Includes docker-compose-plugin (v2) so `docker compose` works.
# -----------------------------------------------------------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -q
apt-get install -yq \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

systemctl enable --now docker
echo "[forge-provision] Docker $(docker --version) installed"

# -----------------------------------------------------------------------------
# 3. Verify cgroups v2
# Required for --memory, --memory-swap, --pids-limit, --cpu-quota in runner.py.
# Ubuntu 22.04 defaults to cgroups v2, but guard against kernels that don't.
# -----------------------------------------------------------------------------
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  echo "[forge-provision] cgroups v2 active: $(cat /sys/fs/cgroup/cgroup.controllers)"
else
  echo "[forge-provision] WARNING: cgroups v2 not detected — enabling via GRUB (reboot required)"
  sed -i \
    's/GRUB_CMDLINE_LINUX="\(.*\)"/GRUB_CMDLINE_LINUX="\1 systemd.unified_cgroup_hierarchy=1 cgroup_no_v1=all"/' \
    /etc/default/grub
  update-grub
fi

# -----------------------------------------------------------------------------
# 4. Docker daemon configuration
# - Log rotation: prevents runaway container logs filling the disk
# - live-restore: keeps containers running if the daemon restarts
# - default-cgroupns-mode private: each container gets its own cgroup namespace
# -----------------------------------------------------------------------------
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  },
  "default-cgroupns-mode": "private",
  "live-restore": true
}
EOF
systemctl restart docker
echo "[forge-provision] Docker daemon configured"

# -----------------------------------------------------------------------------
# 5. Directories
# /opt/forge       — project clone destination
# /tmp/forge-workspaces — per-run isolated workspaces
#   compose.yaml mounts this path on both host and container sides so the
#   DockerRunner (which calls docker run from inside the container using the
#   host Docker socket) can mount the correct host path into each job container.
# -----------------------------------------------------------------------------
mkdir -p /opt/forge
mkdir -p /tmp/forge-workspaces
chmod 1777 /tmp/forge-workspaces
echo "[forge-provision] directories created"

# -----------------------------------------------------------------------------
# 6. Kernel tuning
# fs.inotify limits: engine/logs.py uses threading.Event per job; each job log
#   watcher consumes an inotify instance. Default (128) is too low for concurrent
#   pipelines.
# net.core.somaxconn: SSE keeps many long-lived connections open; raise the
#   accept backlog so uvicorn doesn't drop connections under load.
# -----------------------------------------------------------------------------
cat > /etc/sysctl.d/99-forge.conf <<'EOF'
fs.inotify.max_user_watches   = 524288
fs.inotify.max_user_instances = 512
net.core.somaxconn            = 4096
net.ipv4.tcp_tw_reuse         = 1
EOF
sysctl --system -q
echo "[forge-provision] sysctl tuned"

# -----------------------------------------------------------------------------
# 7. UFW firewall
# Port 22   — SSH management
# Port 8080 — Forge API (HTTP, publicly accessible for grading)
# Port 80   — reserved for future reverse-proxy / TLS termination
# Port 443  — reserved for HTTPS
# Build containers are isolated by Docker's internal network (forge-build-net
# is marked internal: true in compose.yaml), not by host-level firewall rules.
# -----------------------------------------------------------------------------
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment "SSH"
ufw allow 8080/tcp comment "Forge API"
ufw allow 80/tcp   comment "HTTP"
ufw allow 443/tcp  comment "HTTPS"
ufw --force enable
echo "[forge-provision] UFW configured"

# -----------------------------------------------------------------------------
# 8. SSH hardening
# Disable password auth and root password login; key-only access only.
# -----------------------------------------------------------------------------
sed -i \
  -e 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' \
  -e 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' \
  -e 's/^#\?ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' \
  -e 's/^#\?X11Forwarding.*/X11Forwarding no/' \
  /etc/ssh/sshd_config
systemctl restart ssh
echo "[forge-provision] SSH hardened"

# -----------------------------------------------------------------------------
# 9. fail2ban — rate-limit SSH brute-force attempts
# -----------------------------------------------------------------------------
systemctl enable --now fail2ban
echo "[forge-provision] fail2ban enabled"

# -----------------------------------------------------------------------------
# 10. Logrotate for Forge application logs
# Rotates daily, keeps 14 days, compresses old logs.
# Matches the log_dir path from config.yaml (data/logs/ inside the container,
# mapped to the forge-logs Docker volume).
# -----------------------------------------------------------------------------
cat > /etc/logrotate.d/forge <<'EOF'
/var/lib/docker/volumes/forge_forge-logs/_data/*.log {
  daily
  rotate 14
  compress
  delaycompress
  missingok
  notifempty
  copytruncate
}
EOF
echo "[forge-provision] logrotate configured"

# -----------------------------------------------------------------------------
# 11. Install AWS CLI v2 and fetch Slack webhook from SSM
# The IAM instance profile attached by Terraform grants ssm:GetParameter on
# /forge/slack_webhook_url. The webhook is written to /opt/forge/.env so
# Docker Compose picks it up automatically (Compose reads .env in the project dir).
# -----------------------------------------------------------------------------
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
rm -rf /tmp/awscliv2.zip /tmp/aws
echo "[forge-provision] AWS CLI $(aws --version) installed"

REGION=$(curl -sf \
  -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT \
    'http://169.254.169.254/latest/api/token' \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" \
  http://169.254.169.254/latest/meta-data/placement/region)

SLACK_URL=$(aws ssm get-parameter \
  --name "/forge/slack_webhook_url" \
  --with-decryption \
  --query "Parameter.Value" \
  --output text \
  --region "$REGION" 2>/dev/null || true)

mkdir -p /opt/forge
if [ -n "$SLACK_URL" ]; then
  echo "SLACK_WEBHOOK_URL=$SLACK_URL" > /opt/forge/.env
  chmod 600 /opt/forge/.env
  echo "[forge-provision] Slack webhook written to /opt/forge/.env"
else
  echo "[forge-provision] WARNING: could not fetch Slack webhook from SSM — .env not written"
fi

# -----------------------------------------------------------------------------
# 12. Pre-pull base images
# Avoids a cold-start delay when the first pipeline runs against alpine:3.18.
# Runs in the background so provisioning completes quickly.
# -----------------------------------------------------------------------------
docker pull alpine:3.18 &
echo "[forge-provision] pulling alpine:3.18 in background (pid $!)"

# -----------------------------------------------------------------------------
# Done — print next steps
# -----------------------------------------------------------------------------
echo ""
echo "[forge-provision] ============================================"
echo "[forge-provision] Provisioning complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[forge-provision] ============================================"
echo "[forge-provision]"
echo "[forge-provision] Next steps (run as root on the server):"
echo "[forge-provision]"
echo "[forge-provision]   1. Clone the repo:"
echo "[forge-provision]      cd /opt/forge && git clone <repo-url> ."
echo "[forge-provision]"
echo "[forge-provision]   2. Start the platform (.env already written by this script):"
echo "[forge-provision]      docker compose up -d --build"
echo "[forge-provision]"
echo "[forge-provision]   3. Create the first auth token:"
echo "[forge-provision]      docker compose exec forge-api python -m registry.auth create-token --identity admin"
echo "[forge-provision]"
echo "[forge-provision]   4. Update README with the static IP from Terraform output."
