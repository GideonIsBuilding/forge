# Forge Deployment Guide

## Requirements

- Linux VPS — minimum 4 vCPU / 4GB RAM / 40GB disk
- Docker + Docker Compose installed
- Git installed
- Port 8080 open on the VPS firewall

## Fresh VPS Setup

### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Install Docker Compose

```bash
sudo apt-get install -y docker-compose-plugin
docker compose version
```

### 3. Clone the Repository

```bash
git clone https://github.com/GideonIsBuilding/forge.git
cd forge
```

### 4. Configure the Platform

```bash
cp config.yaml config.prod.yaml
```

Edit `config.prod.yaml` and set:

```yaml
engine:
  host: "0.0.0.0"
  port: 8080

registry:
  url: "http://<your-server-ip>:8080"
  db_path: "data/forge.db"
  blob_dir: "blobs"

slack:
  webhook_url: "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
  tags:
    platform: "@platform-team"
    security: "@security-team"
```

### 5. Start the Platform

```bash
docker compose up -d --build
```

Verify it is running:

```bash
docker compose ps
docker compose logs -f forge-api
```

### 6. Create the First Auth Token

```bash
docker compose exec forge-api python -c "
from registry import db, auth
from registry.init_db import create_schema
db.init('data/forge.db')
create_schema()
token = auth.create_token('admin')
print('Token:', token)"
```

This prints a raw token **once** — copy it immediately. It cannot be recovered.

### 7. Verify the API is Live

```bash
curl http://<your-server-ip>:8080/artifacts/forge
```

Should return `{"versions": []}`.

---

## Install the CLI

On your local machine:

```bash
pip install -e .
```

Then log in:

```bash
forge login http://<your-server-ip>:8080
# paste the token when prompted
```

---

## Run the Example Pipelines

Run them in order — each depends on the previous artifact being published:

```bash
# 1. Build and publish lib-core@1.0.0
forge run examples/lib-core.yaml

# 2. Resolve lib-core and publish lib-http@1.0.0
forge run examples/lib-http.yaml

# 3. Resolve both and publish service-api@0.1.0
forge run examples/service-api.yaml
```

Stream live logs for any run:

```bash
forge logs <run-id> --follow
```

List published versions:

```bash
forge ls lib-core
forge ls lib-http
forge ls service-api
```

---

## Docker Network Isolation

Forge creates two Docker networks:

- `forge-control` — the API container lives here
- `forge-build-net` — build containers live here (internal only, no public internet)

Build containers can only reach the Forge registry endpoint. All other
outbound traffic is blocked at the network level.

---

## Stopping the Platform

```bash
docker compose down
```

To wipe all data (blobs, logs, DB) as well:

```bash
docker compose down -v
```

---

## Troubleshooting

**API not reachable** — check firewall: `sudo ufw allow 8080`

**Docker socket permission denied** — add your user to the docker group:
`sudo usermod -aG docker $USER && newgrp docker`

**Token rejected** — tokens are bcrypt-hashed. If you lost a token, create a
new one: `docker compose exec forge-api python -m registry.auth create-token --identity <name>`

**Build container OOM** — increase the `memory` field in your pipeline YAML
or increase the VPS RAM.

**Logs not streaming** — ensure your client supports SSE (`--follow` flag in
the CLI handles this automatically).
