"""
engine/config.py

Loads config.yaml and exposes typed settings to the rest of the engine.
All other modules import from here — never read config.yaml directly.
"""

import logging
from pathlib import Path
from typing import Optional, Union

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("config.yaml")
_config: dict = {}


def load(path: Optional[Union[str, Path]] = None) -> None:
    """Load config.yaml into memory. Call once at application startup."""
    global _config
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    with open(config_path) as f:
        _config = yaml.safe_load(f) or {}
    logger.info("Config loaded from %s", config_path)


def get(key: str, default=None):
    """Fetch a config value using dot notation: get('engine.max_concurrency')"""
    keys = key.split(".")
    val = _config
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k)
        if val is None:
            return default
    return val


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------

def db_path() -> str:
    return get("registry.db_path", "data/registry.db")


def blob_dir() -> str:
    return get("registry.blob_dir", "data/blobs")


def log_dir() -> str:
    return get("engine.log_dir", "data/logs")


def max_job_duration_s() -> int:
    return int(get("engine.max_job_duration_s", 1800))


def max_concurrency() -> int:
    return int(get("engine.max_concurrency", 4))


def registry_url() -> str:
    return get("registry.url", "http://localhost:8080")


def slack_webhook_url() -> Optional[str]:
    return get("slack.webhook_url")


def engine_host() -> str:
    return get("engine.host", "0.0.0.0")


def engine_port() -> int:
    return int(get("engine.port", 8080))
