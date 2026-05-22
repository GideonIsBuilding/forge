# Forge

Forge is a Python CI/CD platform with an integrated artifact registry and dependency resolver.

Public URL: `TODO`

## Architecture

Forge exposes one HTTP API. Internally it has two cooperating subsystems:

- `engine/`: pipeline parsing, pre-build resolution, DAG scheduling, isolated job execution, log streaming, run state.
- `registry/`: bearer-token auth, content-addressed artifact storage, immutable metadata, dependency resolution.

![Architecture Diagram](./forge-architecture.png)

## Repository Structure

```text
engine/         # API gateway, parser, scheduler, runner, logs
registry/       # storage, metadata, resolver, auth
cli/forge.py    # pip-installable CLI
compose.yaml    # Docker Compose deployment
config.yaml     # runtime limits, paths, Slack webhook
requirements.txt
README.md
```

## Pipeline YAML Example

```yaml
name: build-lib-http
version: 1.0.0
dependencies:
  - name: lib-core
    version: "^1.0.0"
jobs:
  build:
    runtime: alpine:3.18
    resources: { cpu: 1.0, memory: 512Mi }
    steps:
      - { name: test, run: "sh ./test.sh" }
      - { name: package, run: "tar czf out.tar.gz src/" }
artifacts:
  - { name: lib-http, version: 1.0.0, path: ./out.tar.gz }
```

Validation rules:

- unknown fields fail
- missing required fields fail
- validation errors should point to the offending line
- dependency resolution and lockfile creation happen before any job starts

## HTTP API Contract

All write operations require `Authorization: Bearer <token>`.

```text
POST /runs
GET  /runs/{id}
GET  /runs/{id}/lockfile
GET  /runs/{id}/logs?follow=true
POST /artifacts/{name}/{version}
GET  /artifacts/{name}/{version}
GET  /artifacts/{name}/{version}/meta
GET  /artifacts/{name}
```

Required statuses:

```text
queued, running, succeeded, failed, integrity_failure, conflict_failure, cycle_failure
```

## Implementation Notes

### DAG Scheduler

The scheduler builds a graph from `jobs.<job>.needs`, detects cycles before execution, and runs independent jobs in topological order up to the configured concurrency limit. Failed jobs mark dependents as skipped.

### Isolation

Jobs run in Docker containers with a dedicated workspace mount, PID/mount/network namespaces, CPU and memory limits, pids limit, wall-clock timeout, dropped capabilities, and no Docker socket mounted into job containers. Build containers can only reach the Forge registry endpoint.

### Storage

Artifact blobs are stored by SHA-256. Metadata stores `(name, version, sha256, size, publisher, published_at, deps)`. A unique database constraint on `(name, version)` enforces immutability and makes racing duplicate publishes return `409`.

### Resolver

The resolver walks registry metadata transitively, supports exact, caret, tilde, and comparator ranges, detects cycles/conflicts, selects the highest satisfying version, and emits deterministic lockfiles with exact versions and hashes.

### Logs

Runner stdout/stderr is written to append-only disk logs with timestamps at write time. SSE clients receive backlog from disk and then live lines. Large logs are streamed in chunks and are not loaded fully into memory.

### Slack Alerts

Slack webhook URL is configured in `config.yaml`.

Required events:

- pipeline started
- pipeline succeeded
- pipeline failed
- integrity failure
- resolution failure

Screenshot: `TODO`

## Fresh VPS Setup

```bash
git clone <repo-url> forge
cd forge
cp config.yaml config.prod.yaml
docker compose up -d --build
docker compose exec forge-api python -m registry.auth create-token --identity admin
```

Then run:

```bash
forge login http://<server-ip>:8080
forge run examples/lib-core.yaml
```

## Required Capability Checklist

- [ ] pipeline builds and publishes `lib-core@1.0.0`
- [ ] pipeline resolves `lib-core@^1.0.0` and publishes `lib-http@1.0.0`
- [ ] pipeline resolves both and publishes `service-api@0.1.0`
- [ ] wrong checksum upload returns `400`
- [ ] duplicate upload returns `409`
- [ ] version conflict fails before build
- [ ] filesystem escape, memory exhaustion, and non-registry egress are contained
- [ ] 50MB logs stream live without buffering
