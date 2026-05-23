"""
engine/slack.py

Slack webhook alerts for the Forge CI/CD platform.

Required alert events
---------------------
  pipeline_started     — pipeline name, run ID
  pipeline_succeeded   — pipeline name, run ID, duration
  pipeline_failed      — pipeline name, run ID, duration, failing job
  integrity_failure    — artifact coordinate, expected/actual SHA-256, run ID
  resolution_failure   — pipeline name, conflict/cycle details

Tag injection
-------------
Tags are read from config.yaml under slack.tags:
  platform: "@platform-team"
  security: "@security-team"

Public interface
----------------
  notify_pipeline_started(run_id, pipeline_name)
  notify_pipeline_succeeded(run_id, pipeline_name, duration_s)
  notify_pipeline_failed(run_id, pipeline_name, duration_s, failing_job)
  notify_integrity_failure(run_id, artifact_name, version, expected_sha256, actual_sha256)
  notify_resolution_failure(run_id, pipeline_name, detail)
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

from engine import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _webhook_url() -> Optional[str]:
    url = config.slack_webhook_url()
    if not url or not url.strip():
        return None
    return url.strip()


def _tag(key: str) -> str:
    """Return the configured Slack tag for a role, e.g. '@platform-team'."""
    val = config.get(f"slack.tags.{key}", "")
    return str(val) if val else ""


def _platform_tag() -> str:
    return _tag("platform")


def _security_tag() -> str:
    return _tag("security")


def _send(payload: dict) -> None:
    """POST a JSON payload to the configured Slack webhook.

    Fires and forgets — any network error is logged but never propagated.
    A missing or empty webhook URL silently skips the send so dev/test
    environments don't need a real Slack workspace.
    """
    url = _webhook_url()
    if not url:
        logger.debug("Slack webhook not configured, skipping notification")
        return

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            if status != 200:
                logger.warning("Slack webhook returned HTTP %d", status)
            else:
                logger.debug("Slack notification sent (HTTP %d)", status)
    except Exception:
        logger.exception("Failed to send Slack notification to %s", url)


def _build_message(text: str) -> dict:
    """Wrap a plain text message in the Slack Incoming Webhooks payload format."""
    return {"text": text}


def _fmt_duration(duration_s: Optional[float]) -> str:
    if duration_s is None:
        return "unknown"
    if duration_s < 60:
        return f"{duration_s:.1f}s"
    minutes = int(duration_s) // 60
    seconds = int(duration_s) % 60
    return f"{minutes}m {seconds}s"


# ---------------------------------------------------------------------------
# Public notification functions
# ---------------------------------------------------------------------------


def notify_pipeline_started(run_id: str, pipeline_name: str) -> None:
    """Alert: a pipeline has been submitted and is now running."""
    platform = _platform_tag()
    tag_line = f" {platform}" if platform else ""
    text = (
        f":rocket: *Pipeline started*{tag_line}\n"
        f"• Pipeline: `{pipeline_name}`\n"
        f"• Run ID: `{run_id}`"
    )
    _send(_build_message(text))
    logger.info("Slack: pipeline started — run=%s pipeline=%s", run_id, pipeline_name)


def notify_pipeline_succeeded(
    run_id: str,
    pipeline_name: str,
    duration_s: Optional[float] = None,
) -> None:
    """Alert: a pipeline completed successfully."""
    platform = _platform_tag()
    tag_line = f" {platform}" if platform else ""
    text = (
        f":white_check_mark: *Pipeline succeeded*{tag_line}\n"
        f"• Pipeline: `{pipeline_name}`\n"
        f"• Run ID: `{run_id}`\n"
        f"• Duration: {_fmt_duration(duration_s)}"
    )
    _send(_build_message(text))
    logger.info(
        "Slack: pipeline succeeded — run=%s pipeline=%s duration=%.1fs",
        run_id,
        pipeline_name,
        duration_s or 0,
    )


def notify_pipeline_failed(
    run_id: str,
    pipeline_name: str,
    duration_s: Optional[float] = None,
    failing_job: Optional[str] = None,
) -> None:
    """Alert: a pipeline failed (build error, OOM, timeout, etc.)."""
    platform = _platform_tag()
    tag_line = f" {platform}" if platform else ""
    job_line = f"\n• Failing job: `{failing_job}`" if failing_job else ""
    text = (
        f":x: *Pipeline failed*{tag_line}\n"
        f"• Pipeline: `{pipeline_name}`\n"
        f"• Run ID: `{run_id}`\n"
        f"• Duration: {_fmt_duration(duration_s)}"
        f"{job_line}"
    )
    _send(_build_message(text))
    logger.info(
        "Slack: pipeline failed — run=%s pipeline=%s failing_job=%s",
        run_id,
        pipeline_name,
        failing_job,
    )


def notify_integrity_failure(
    run_id: str,
    artifact_name: str,
    version: str,
    expected_sha256: str,
    actual_sha256: str,
) -> None:
    """
    Alert: SHA-256 mismatch detected when pulling a dependency.

    This is a security event — both the platform team and security team
    are notified.
    """
    platform = _platform_tag()
    security = _security_tag()
    tags = " ".join(t for t in [platform, security] if t)
    tag_line = f" {tags}" if tags else ""

    text = (
        f":warning: *Integrity failure*{tag_line}\n"
        f"• Run ID: `{run_id}`\n"
        f"• Artifact: `{artifact_name}@{version}`\n"
        f"• Expected SHA-256: `{expected_sha256}`\n"
        f"• Actual SHA-256:   `{actual_sha256}`\n"
        f"_The dependency checksum does not match the lockfile. "
        f"This may indicate a tampered or corrupted artifact._"
    )
    _send(_build_message(text))
    logger.warning(
        "Slack: integrity failure — run=%s artifact=%s@%s expected=%s actual=%s",
        run_id,
        artifact_name,
        version,
        expected_sha256,
        actual_sha256,
    )


def notify_resolution_failure(
    run_id: str,
    pipeline_name: str,
    detail: str,
) -> None:
    """Alert: dependency resolution failed (conflict or cycle)."""
    platform = _platform_tag()
    tag_line = f" {platform}" if platform else ""
    text = (
        f":no_entry: *Resolution failure*{tag_line}\n"
        f"• Pipeline: `{pipeline_name}`\n"
        f"• Run ID: `{run_id}`\n"
        f"• Detail: {detail}"
    )
    _send(_build_message(text))
    logger.info(
        "Slack: resolution failure — run=%s pipeline=%s detail=%s",
        run_id,
        pipeline_name,
        detail,
    )
