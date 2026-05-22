"""
registry/auth.py

Bearer token auth for the Forge registry.
Tokens are stored as bcrypt hashes — never plaintext.

Public interface
----------------
create_token(identity)       -> raw token string (shown once, then gone)
verify_token(raw_token)      -> identity string | None
require_auth(request)        -> identity string (raises AuthError if invalid)
"""

import logging
import secrets

import bcrypt

from registry import db, metadata

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when a request carries no token or an invalid one."""
    def __init__(self, message: str = "Unauthorized") -> None:
        self.message = message
        super().__init__(message)


def create_token(identity: str) -> str:
    """
    Generate a new bearer token for the given identity.
    Stores the bcrypt hash in the DB and returns the raw token.
    The raw token is shown exactly once — it cannot be recovered later.
    """
    from datetime import datetime, timezone
    raw = secrets.token_hex(32)  # 64-char hex string
    token_hash = bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).isoformat()

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tokens (token_hash, identity, created_at) VALUES (?, ?, ?)",
            (token_hash, identity, now),
        )

    logger.info("Token created for identity: %s", identity)
    return raw


def verify_token(raw_token: str) -> str | None:
    """
    Check raw_token against every stored hash.
    Returns the identity string on match, or None if invalid.
    """
    rows = db.fetchall("SELECT token_hash, identity FROM tokens")
    for row in rows:
        if bcrypt.checkpw(raw_token.encode(), row["token_hash"].encode()):
            return row["identity"]
    return None


def require_auth(authorization_header: str | None) -> str:
    """
    Validate the Authorization header from an HTTP request.
    Expects: 'Bearer <token>'
    Returns the identity on success.
    Raises AuthError on missing or invalid token.
    """
    if not authorization_header:
        raise AuthError("Missing Authorization header")

    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header must be: Bearer <token>")

    raw_token = parts[1].strip()
    identity = verify_token(raw_token)

    if identity is None:
        raise AuthError("Invalid or unrecognised token")

    return identity


def list_tokens() -> list[dict]:
    """
    Return all token records (hash + identity + created_at).
    Used for admin inspection — never returns raw tokens.
    """
    rows = db.fetchall("SELECT id, identity, created_at FROM tokens")
    return [dict(r) for r in rows]
