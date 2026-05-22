from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engine.parser import Job


@dataclass(frozen=True)
class RunnerConfig:
    docker_network: str
    registry_host: str
    timeout_seconds: int
    pids_limit: int


@dataclass(frozen=True)
class JobResult:
    job: str
    exit_code: int
    status: str


class DockerRunner:
    """Runs Forge jobs in isolated Docker containers.

    The implementation should use Docker Engine API or docker CLI with:
    memory/cpu limits, pids limit, dropped capabilities, no-new-privileges,
    a dedicated workspace mount, and a network that only reaches Forge.
    """

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config

    async def run_job(self, job: Job, workspace: Path, forge_url: str, forge_token: str) -> JobResult:
        raise NotImplementedError("Docker runner isolation is the next implementation slice")
