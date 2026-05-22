"""
cli/forge.py

Forge CLI — pip-installable command-line tool for the Forge CI/CD platform.

Commands:
    forge login <url>                          store server URL and token
    forge run <pipeline.yaml>                  submit a pipeline
    forge logs <run-id> [--follow]             stream logs
    forge publish <path> --name <n> --version  publish an artifact
    forge resolve <pipeline.yaml>              print lockfile without running
    forge ls <package>                         list versions of a package
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

CONFIG_PATH = Path.home() / ".forge" / "config.json"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _get_url() -> str:
    cfg = _load_config()
    url = cfg.get("url")
    if not url:
        print("Error: not logged in. Run: forge login <url>")
        sys.exit(1)
    return url.rstrip("/")


def _get_token() -> str:
    cfg = _load_config()
    token = cfg.get("token")
    if not token:
        print("Error: no token found. Run: forge login <url>")
        sys.exit(1)
    return token


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> None:
    """Store the server URL and prompt for a bearer token."""
    url = args.url.rstrip("/")
    token = input(f"Enter bearer token for {url}: ").strip()
    if not token:
        print("Error: token cannot be empty.")
        sys.exit(1)
    _save_config({"url": url, "token": token})
    print(f"Logged in to {url}")


def cmd_run(args: argparse.Namespace) -> None:
    """Submit a pipeline YAML file and print the run ID."""
    pipeline_path = Path(args.pipeline)
    if not pipeline_path.exists():
        print(f"Error: file not found: {pipeline_path}")
        sys.exit(1)

    url = _get_url()
    with open(pipeline_path, "rb") as f:
        resp = requests.post(
            f"{url}/runs",
            files={"pipeline": f},
            headers=_auth_headers(),
        )

    if resp.status_code != 200:
        print(f"Error: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    print(f"Run submitted: {data['run_id']}")


def cmd_logs(args: argparse.Namespace) -> None:
    """Fetch or stream logs for a run."""
    url = _get_url()
    follow = args.follow
    params = {"follow": "true"} if follow else {}

    resp = requests.get(
        f"{url}/runs/{args.run_id}/logs",
        headers={**_auth_headers(), "Accept": "text/event-stream"},
        params=params,
        stream=True,
    )

    if resp.status_code != 200:
        print(f"Error: {resp.status_code} {resp.text}")
        sys.exit(1)

    try:
        for line in resp.iter_lines(decode_unicode=True):
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                ts = payload.get("ts", "")
                job = payload.get("job", "")
                text = payload.get("line", "")
                print(f"[{ts}] [{job}] {text}")
    except KeyboardInterrupt:
        pass


def cmd_publish(args: argparse.Namespace) -> None:
    """Publish an artifact to the registry."""
    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}")
        sys.exit(1)

    import hashlib
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    url = _get_url()
    with open(path, "rb") as f:
        resp = requests.post(
            f"{url}/artifacts/{args.name}/{args.version}",
            files={"file": f},
            data={"checksum": f"sha256:{sha256}"},
            headers=_auth_headers(),
        )

    if resp.status_code == 201:
        print(f"Published {args.name}@{args.version} (sha256:{sha256})")
    elif resp.status_code == 409:
        print(f"Error: {args.name}@{args.version} already exists (immutable)")
        sys.exit(1)
    elif resp.status_code == 400:
        print(f"Error: checksum mismatch — {resp.text}")
        sys.exit(1)
    else:
        print(f"Error: {resp.status_code} {resp.text}")
        sys.exit(1)


def cmd_resolve(args: argparse.Namespace) -> None:
    """Print the resolved lockfile for a pipeline without running it."""
    pipeline_path = Path(args.pipeline)
    if not pipeline_path.exists():
        print(f"Error: file not found: {pipeline_path}")
        sys.exit(1)

    url = _get_url()
    with open(pipeline_path, "rb") as f:
        resp = requests.post(
            f"{url}/runs",
            files={"pipeline": f},
            headers=_auth_headers(),
            params={"resolve_only": "true"},
        )

    if resp.status_code != 200:
        print(f"Error: {resp.status_code} {resp.text}")
        sys.exit(1)

    run_id = resp.json()["run_id"]
    lockfile_resp = requests.get(
        f"{url}/runs/{run_id}/lockfile",
        headers=_auth_headers(),
    )
    print(json.dumps(lockfile_resp.json(), indent=2))


def cmd_ls(args: argparse.Namespace) -> None:
    """List all versions of a package in the registry."""
    url = _get_url()
    resp = requests.get(
        f"{url}/artifacts/{args.package}",
        headers=_auth_headers(),
    )

    if resp.status_code == 404:
        print(f"No versions found for {args.package}")
        sys.exit(1)
    elif resp.status_code != 200:
        print(f"Error: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    versions = data.get("versions", [])
    if not versions:
        print(f"No versions found for {args.package}")
    else:
        print(f"{args.package}:")
        for v in versions:
            print(f"  {v}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Forge CI/CD platform CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # login
    p_login = sub.add_parser("login", help="Store server URL and token")
    p_login.add_argument("url", help="Forge server URL")

    # run
    p_run = sub.add_parser("run", help="Submit a pipeline")
    p_run.add_argument("pipeline", help="Path to pipeline YAML file")

    # logs
    p_logs = sub.add_parser("logs", help="Fetch or stream run logs")
    p_logs.add_argument("run_id", help="Run ID")
    p_logs.add_argument("--follow", action="store_true", help="Stream live logs")

    # publish
    p_pub = sub.add_parser("publish", help="Publish an artifact")
    p_pub.add_argument("path", help="Path to artifact file")
    p_pub.add_argument("--name", required=True, help="Artifact name")
    p_pub.add_argument("--version", required=True, help="Artifact version")

    # resolve
    p_res = sub.add_parser("resolve", help="Print lockfile without running")
    p_res.add_argument("pipeline", help="Path to pipeline YAML file")

    # ls
    p_ls = sub.add_parser("ls", help="List versions of a package")
    p_ls.add_argument("package", help="Package name")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "login": cmd_login,
        "run": cmd_run,
        "logs": cmd_logs,
        "publish": cmd_publish,
        "resolve": cmd_resolve,
        "ls": cmd_ls,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
