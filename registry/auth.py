from __future__ import annotations

import secrets

import click
from passlib.hash import argon2

from registry.metadata import MetadataStore


class TokenStore:
    def __init__(self, metadata: MetadataStore) -> None:
        self.metadata = metadata

    def create(self, identity: str) -> str:
        token = f"forge_{secrets.token_urlsafe(32)}"
        self.metadata.store_token_hash(identity, argon2.hash(token))
        return token

    def verify(self, token: str) -> str | None:
        for row in self.metadata.token_hashes():
            if argon2.verify(token, row["token_hash"]):
                return row["identity"]
        return None


@click.group()
def cli() -> None:
    pass


@cli.command("create-token")
@click.option("--identity", required=True)
@click.option("--db", default="data/forge.db", show_default=True)
def create_token(identity: str, db: str) -> None:
    store = TokenStore(MetadataStore(db))
    click.echo(store.create(identity))


if __name__ == "__main__":
    cli()
