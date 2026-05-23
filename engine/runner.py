"""
engine/runner.py

Docker-based job runner for the Forge CI/CD platform.

Each job runs in a fresh, isolated container with:
  - Its own PID/mount/network namespace (Docker default)
  - CPU and memory limits enforced via cgroups v2
  - PID limit to prevent fork-bombs
  - Network restricted to forge-build-net (registry-only egress)
  - No access to host filesystem beyond the job workspace
  - Dropped capabilities + no-new-privileges
  - Wall-clock timeout (SIGKILL on breach)

Live log streaming
------------------
stdout/stderr are attached via docker logs --follow in a background thread.
Each line is timestamped and written through engine.logs so SSE clients
get it in real time. Logs are also persisted to disk automatically.

OOM detection
-------------
docker inspect after container exit exposes OOMKilled=true.
When detected the job is failed with a clear OOM log line.

Public interface
----------------
  RunnerConfig   — typed settings (from config.yaml)
  JobResult      — exit_code, status, oom_killed flag
  DockerRunner   — async run_job(job, workspace, forge_url, forge_token)
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine import config, logs
from engine.parser import Job

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerConfig:
    docker_network: str
    registry_host: str
    timeout_seconds: int
    pids_limit: int
    no_new_privileges: bool = True
    drop_capabilities: list[str] = field(default_factory=lambda: ["ALL"])


@dataclass
class JobResult:
    job: str
    exit_code: int
    status: str
    oom_killed: bool = False


# ---------------------------------------------------------------------------
# Memory unit parser
# ---------------------------------------------------------------------------


def _parse_memory_bytes(raw: str) -> int:
    """Convert a memory string like '512Mi', '1Gi', '256M' to bytes."""
    raw = raw.strip()
    units = {
        "ki": 1024,
        "mi": 1024**2,
        "gi": 1024**3,
        "ti": 1024**4,
        "pi": 1024**5,
        "k": 1000,
        "m": 1000**2,
        "g": 1000**3,
        "t": 1000**4,
        "p": 1000**5,
    }
    lower = raw.lower()
    for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
        if lower.endswith(suffix):
            return int(raw[: -len(suffix)]) * multiplier
    return int(raw)  # plain bytes


# ---------------------------------------------------------------------------
# Docker runner
# ---------------------------------------------------------------------------


class DockerRunner:
    """Runs Forge jobs in isolated Docker containers.

    The implementation should use Docker Engine API or docker CLI with:
    memory/cpu limits, pids limit, dropped capabilities, no-new-privileges,
    a dedicated workspace mount, and a network that only reaches Forge.
    """

    def __init__(self, runner_cfg: Optional[RunnerConfig] = None) -> None:
        if runner_cfg is None:
            runner_cfg = RunnerConfig(
                docker_network=config.get("runner.docker_network", "forge_build_net"),
                registry_host=config.get("runner.registry_host", "forge-api"),
                timeout_seconds=config.max_job_duration_s(),
                pids_limit=int(config.get("runner.pids_limit", 256)),
                no_new_privileges=bool(config.get("runner.no_new_privileges", True)),
                drop_capabilities=list(config.get("runner.drop_capabilities", ["ALL"])),
            )

        self.cfg = runner_cfg

    async def run_job(
        self, job: Job, workspace: Path, forge_url: str, forge_token: str, run_id: str
    ) -> JobResult:
        """
        Execute *job* inside a fresh Docker container and return a JobResult.

        The coroutine offloads all blocking Docker I/O to a thread-pool
        executor so the asyncio event loop stays responsive.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            self._run_job_sync,
            job,
            workspace,
            forge_url,
            forge_token,
            run_id,
        )
        return result

    # ------------------------------------------------------------------
    # Synchronous implementation (runs in thread-pool)
    # ------------------------------------------------------------------

    def _run_job_sync(
        self,
        job: Job,
        workspace: Path,
        forge_url: str,
        forge_token: str,
        run_id: str,
    ) -> JobResult:
        container_name = f"forge-{run_id}-{job.name}".replace("/", "-")

        # Build up the docker run command
        cmd = self._build_docker_cmd(
            job=job,
            workspace=workspace,
            container_name=container_name,
            forge_url=forge_url,
            forge_token=forge_token,
        )

        logs.write_line(run_id, job.name, f"[forge] Starting container: {container_name}")
        logs.write_line(run_id, job.name, f"[forge] Image: {job.runtime}")
        logs.write_line(
            run_id,
            job.name,
            f"[forge] Resources: cpu={job.resources.cpu} memory={job.resources.memory}",
        )

        container_id: Optional[str] = None
        exit_code = -1
        oom_killed = False

        try:
            # Launch the container detached so we can stream logs independently
            run_result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if run_result.returncode != 0:
                err = run_result.stderr.strip()
                logs.write_line(
                    run_id, job.name, f"[forge] ERROR: failed to start container: {err}"
                )
                logger.error("Job %s/%s: docker run failed: %s", run_id, job.name, err)
                return JobResult(job=job.name, exit_code=1, status="failed")

            container_id = run_result.stdout.strip()
            logs.write_line(run_id, job.name, f"[forge] Container started: {container_id[:12]}")

            # Stream logs in a background thread while we wait
            stop_event = threading.Event()
            log_thread = threading.Thread(
                target=self._stream_container_logs,
                args=(container_id, run_id, job.name, stop_event),
                daemon=True,
            )
            log_thread.start()

            # Wait for the container to finish (with timeout)
            exit_code = self._wait_for_container(
                container_id=container_id,
                run_id=run_id,
                job_name=job.name,
                timeout_seconds=self.cfg.timeout_seconds,
            )

            # Signal log thread to stop and give it a moment to drain
            stop_event.set()
            log_thread.join(timeout=5.0)

            # Inspect for OOM kill
            oom_killed = self._check_oom(container_id)
            if oom_killed:
                logs.write_line(
                    run_id,
                    job.name,
                    f"[forge] OOM: container was killed by kernel out-of-memory killer "
                    f"(limit={job.resources.memory})",
                )
                logger.warning(
                    "Job %s/%s OOM killed (memory limit=%s)",
                    run_id,
                    job.name,
                    job.resources.memory,
                )
                exit_code = 137  # SIGKILL exit code

        except subprocess.TimeoutExpired:
            logs.write_line(
                run_id,
                job.name,
                f"[forge] TIMEOUT: job exceeded {self.cfg.timeout_seconds}s wall-clock limit",
            )
            logger.warning("Job %s/%s timed out", run_id, job.name)
            exit_code = 124
            if container_id:
                self._kill_container(container_id)

        except Exception:
            logger.exception("Job %s/%s: unexpected error in runner", run_id, job.name)
            logs.write_line(
                run_id, job.name, "[forge] ERROR: unexpected runner error (see server logs)"
            )
            exit_code = -1

        finally:
            if container_id:
                self._remove_container(container_id)

        status = "succeeded" if exit_code == 0 else "failed"
        logs.write_line(
            run_id, job.name, f"[forge] Job finished: status={status} exit_code={exit_code}"
        )
        return JobResult(job=job.name, exit_code=exit_code, status=status, oom_killed=oom_killed)
        # ------------------------------------------------------------------

    # Docker command builder
    # ------------------------------------------------------------------

    def _build_docker_cmd(
        self,
        job: Job,
        workspace: Path,
        container_name: str,
        forge_url: str,
        forge_token: str,
    ) -> list[str]:
        """Build the `docker run` argument list for a job."""
        mem_bytes = _parse_memory_bytes(job.resources.memory)
        cpu_quota = int(job.resources.cpu * 100_000)  # in microseconds per 100ms period

        # Build the shell command: run every step in sequence, stop on first failure
        step_commands = " && ".join(
            f"echo '[forge:step] {shlex.quote(step.name)}' && {step.run}" for step in job.steps
        )

        cmd = [
            "docker",
            "run",
            "--detach",  # run in background
            "--name",
            container_name,
            "--network",
            self.cfg.docker_network,  # registry-only network
            "--workdir",
            "/workspace",
            # Workspace: job reads/writes here; deps already materialized
            "--volume",
            f"{workspace.resolve()}:/workspace:rw",
            # Resource limits
            f"--memory={mem_bytes}",
            f"--memory-swap={mem_bytes}",  # no swap beyond memory limit
            f"--cpu-quota={cpu_quota}",
            "--cpu-period=100000",
            f"--pids-limit={self.cfg.pids_limit}",
            # Security hardening
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges:true",
            "--read-only",  # root FS read-only ...
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",  # ... except /tmp
            # Environment injected for forge publish inside pipelines
            f"--env=FORGE_URL={forge_url}",
            f"--env=FORGE_TOKEN={forge_token}",
            f"--env=FORGE_REGISTRY_HOST={self.cfg.registry_host}",
            # Remove container automatically when stopped (we inspect first)
            # Note: we do NOT use --rm here because we need to inspect exit code
            "--restart=no",
            # Image
            job.runtime,
            # Command: sh -c "<all steps joined with &&>"
            "sh",
            "-c",
            step_commands,
        ]

        # Add any extra dropped capabilities beyond ALL (already dropped above)
        # In practice this is belt-and-suspenders since we already drop ALL.
        for cap in self.cfg.drop_capabilities:
            if cap != "ALL":
                cmd.insert(-3, f"--cap-drop={cap}")

        return cmd

    # ------------------------------------------------------------------
    # Container lifecycle helpers
    # ------------------------------------------------------------------

    def _stream_container_logs(
        self,
        container_id: str,
        run_id: str,
        job_name: str,
        stop_event: threading.Event,
    ) -> None:
        """
        Stream container stdout/stderr into engine.logs in a background thread.

        Uses `docker logs --follow` which streams lines as they are produced
        by the container process — no buffering on our side.
        """
        try:
            proc = subprocess.Popen(
                ["docker", "logs", "--follow", "--timestamps", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                if stop_event.is_set():
                    break
                # Docker prepends a timestamp when --timestamps is used;
                # strip it and let write_line add our own normalized timestamp.
                if " " in line:
                    _, _, text = line.partition(" ")
                    line = text
                logs.write_line(run_id, job_name, line.rstrip("\n"))
            proc.wait(timeout=5)
        except Exception:
            logger.exception(
                "Log streaming error for container %s (job %s/%s)",
                container_id[:12],
                run_id,
                job_name,
            )

    def _wait_for_container(
        self,
        container_id: str,
        run_id: str,
        job_name: str,
        timeout_seconds: int,
    ) -> int:
        """
        Block until the container exits or the timeout fires.

        `docker wait` blocks until the container stops and prints the exit code.
        We run it with a subprocess timeout so we can enforce the wall-clock
        limit ourselves.
        """
        try:
            result = subprocess.run(
                ["docker", "wait", container_id],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code_str = result.stdout.strip()
            if exit_code_str.lstrip("-").isdigit():
                return int(exit_code_str)
            logger.warning(
                "Job %s/%s: unexpected docker wait output: %r",
                run_id,
                job_name,
                exit_code_str,
            )
            return -1

        except subprocess.TimeoutExpired:
            logs.write_line(
                run_id,
                job_name,
                f"[forge] TIMEOUT: wall-clock limit of {timeout_seconds}s exceeded, killing container",
            )
            self._kill_container(container_id)
            return 124

    def _check_oom(self, container_id: str) -> bool:
        """Return True if the container was OOM-killed."""
        try:
            result = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.OOMKilled}}",
                    container_id,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip().lower() == "true"
        except Exception:
            return False

    def _kill_container(self, container_id: str) -> None:
        """Send SIGKILL to a running container."""
        try:
            subprocess.run(
                ["docker", "kill", container_id],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            logger.exception("Failed to kill container %s", container_id[:12])

    def _remove_container(self, container_id: str) -> None:
        """Remove a stopped container, ignoring errors (best-effort cleanup)."""
        try:
            subprocess.run(
                ["docker", "rm", "--force", container_id],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            logger.debug("Failed to remove container %s (non-fatal)", container_id[:12])
