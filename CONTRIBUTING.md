# Forge Team Contribution Guide

This guide explains how the four-person team should create branches, make commits, open pull requests, and avoid conflicting work while building Forge.

## Team Workstreams

Split the project by subsystem so each person has clear ownership.

| Member | Primary Area | Main Folders |
| --- | --- | --- |
| Nehemiah | API gateway, run lifecycle, config | `engine/main.py`, `config.yaml`, `compose.yaml` |
| Adeshipo | Artifact registry, metadata, auth | `registry/storage.py`, `registry/metadata.py`, `registry/auth.py` |
| Gideon | Resolver, parser, scheduler | `registry/resolver.py`, `engine/parser.py`, `engine/scheduler.py` |
| Vivian | Runner, logs, CLI, deployment docs | `engine/runner.py`, `engine/logs.py`, `cli/forge.py`, `README.md` |

Before starting a task, post the branch name and files you expect to touch in the team channel.

## Branch Naming

Use short lowercase branch names:

```text
<type>/<member-name>/<short-task>
```

Allowed branch types:

```text
feature
fix
test
docs
chore
refactor
```

Examples:

```text
feature/ada/artifact-upload
feature/tolu/dag-scheduler
fix/ife/checksum-mismatch
test/zainab/resolver-conflicts
docs/ada/vps-setup
chore/tolu/docker-compose
```

Do not use spaces, uppercase letters, or vague names like:

```text
my-branch
updates
final-work
stuff
```

## Commit Message Style

Use this format:

```text
<type>: <short summary>
```

Examples:

```text
feature: add immutable artifact upload
fix: reject invalid semver versions
test: cover duplicate artifact publishing
docs: document fresh VPS setup
chore: add docker compose volumes
```

Keep commits focused. One commit should represent one clear change.

Good:

```text
feature: add token creation command
test: add auth verification tests
```

Avoid:

```text
feature: update everything
fix: changes
```

## Daily Git Flow

Start from the latest `main`:

```bash
git checkout main
git pull origin main
git checkout -b feature/member-name/task-name
```

Work locally, then check your changes:

```bash
git status
python3 -m pytest -q
```

Commit:

```bash
git add .
git commit -m "feature: add artifact upload endpoint"
```

Push:

```bash
git push origin feature/member-name/task-name
```

Open a pull request into `main`.

## Pull Request Rules

Each PR must include:

- what changed
- how it was tested
- any unfinished work or known limitation
- screenshots or logs when useful

Small PRs are easier to review. Prefer PRs that touch one subsystem at a time.

Before merging:

- at least one teammate must review
- tests must pass locally
- no unrelated formatting or file churn
- no secrets, tokens, or `.env` files committed

## Protected Files

Coordinate before changing these files because they affect everyone:

```text
compose.yaml
config.yaml
requirements.txt
pyproject.toml
README.md
```

If you need to change one of these, say so in the team channel before opening the PR.

## Merge Conflict Rules

If there is a conflict:

1. Pull the latest `main`.
2. Rebase or merge into your branch.
3. Resolve only the files related to your task.
4. Ask the file owner to review if you touched their area.

Recommended:

```bash
git checkout main
git pull origin main
git checkout feature/member-name/task-name
git merge main
```

Then fix conflicts, run tests, commit, and push.

## Testing Expectations

Every feature should include at least one test where practical.

Minimum expected tests by area:

| Area | Required Test Focus |
| --- | --- |
| Registry | checksum mismatch, duplicate upload, invalid semver |
| Resolver | caret, tilde, comparator ranges, conflicts, cycles |
| Parser | unknown fields, missing fields, valid pipeline |
| Scheduler | DAG order, parallel batches, cycle detection |
| Runner | timeout, memory limit, network denial, filesystem isolation |
| Logs | backlog replay, follow mode, large log streaming |
| CLI | command arguments and HTTP request behavior |

## Secrets Policy

Never commit:

```text
.env
tokens
Slack webhook URLs
private keys
server passwords
production config with secrets
```

Use `config.yaml` for safe defaults only. Put real secrets in environment variables or a private server config file that is not committed.

## Definition of Done

A task is done when:

- the code is implemented
- tests are added or updated
- existing tests pass
- the README or docs are updated if behavior changed
- the PR description explains the change clearly
- the branch is merged into `main`

## Suggested First Branches

The team can start with these:

```text
feature/member-1/run-api
feature/member-2/artifact-registry
feature/member-3/resolver-core
feature/member-4/log-streaming
```

After those land, continue with:

```text
feature/member-1/run-state-db
feature/member-2/auth-tokens
feature/member-3/pipeline-parser
feature/member-4/docker-runner
```

