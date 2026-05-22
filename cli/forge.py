from __future__ import annotations

import json
from pathlib import Path

import click
import httpx


CONFIG_DIR = Path.home() / ".forge"
CONFIG_FILE = CONFIG_DIR / "credentials.json"


def _load_credentials() -> dict[str, str]:
    if not CONFIG_FILE.exists():
        raise click.ClickException("not logged in; run forge login <url>")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def _auth_headers() -> dict[str, str]:
    creds = _load_credentials()
    return {"Authorization": f"Bearer {creds['token']}"}


@click.group()
def main() -> None:
    """Forge command-line client."""


@main.command()
@click.argument("url")
@click.option("--token", prompt=True, hide_input=True)
def login(url: str, token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"url": url.rstrip("/"), "token": token}, indent=2), encoding="utf-8")
    click.echo(f"Logged in to {url.rstrip('/')}")


@main.command("run")
@click.argument("pipeline_yaml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def run_pipeline(pipeline_yaml: Path) -> None:
    creds = _load_credentials()
    with pipeline_yaml.open("rb") as handle:
        response = httpx.post(
            f"{creds['url']}/runs",
            headers=_auth_headers(),
            files={"pipeline": (pipeline_yaml.name, handle, "application/x-yaml")},
            timeout=30,
        )
    response.raise_for_status()
    click.echo(json.dumps(response.json(), indent=2))


@main.command()
@click.argument("run_id")
@click.option("--follow", is_flag=True)
def logs(run_id: str, follow: bool) -> None:
    creds = _load_credentials()
    with httpx.stream("GET", f"{creds['url']}/runs/{run_id}/logs", params={"follow": follow}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            click.echo(line)


@main.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--name", required=True)
@click.option("--version", required=True)
@click.option("--checksum", required=True, help="sha256:<hex>")
def publish(path: Path, name: str, version: str, checksum: str) -> None:
    creds = _load_credentials()
    with path.open("rb") as handle:
        response = httpx.post(
            f"{creds['url']}/artifacts/{name}/{version}",
            headers=_auth_headers(),
            files={"file": (path.name, handle), "checksum": (None, checksum)},
            timeout=60,
        )
    response.raise_for_status()
    click.echo(json.dumps(response.json(), indent=2))


@main.command()
@click.argument("pipeline_yaml", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def resolve(pipeline_yaml: Path) -> None:
    raise click.ClickException("resolve command scaffolded; wire to resolver endpoint next")


@main.command("ls")
@click.argument("package")
def list_versions(package: str) -> None:
    creds = _load_credentials()
    response = httpx.get(f"{creds['url']}/artifacts/{package}", timeout=30)
    response.raise_for_status()
    click.echo(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
