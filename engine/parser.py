from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


class PipelineValidationError(ValueError):
    """Raised when a pipeline YAML file does not match the Forge schema."""


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str


@dataclass(frozen=True)
class Step:
    name: str
    run: str


@dataclass(frozen=True)
class Resources:
    cpu: float = 1.0
    memory: str = "512Mi"


@dataclass(frozen=True)
class Job:
    name: str
    runtime: str
    steps: list[Step]
    needs: list[str] = field(default_factory=list)
    resources: Resources = field(default_factory=Resources)


@dataclass(frozen=True)
class Artifact:
    name: str
    version: str
    path: str


@dataclass(frozen=True)
class Pipeline:
    name: str
    version: str
    dependencies: list[Dependency]
    jobs: dict[str, Job]
    artifacts: list[Artifact]


ROOT_FIELDS = {"name", "version", "dependencies", "jobs", "artifacts"}
JOB_FIELDS = {"runtime", "resources", "steps", "needs"}
RESOURCE_FIELDS = {"cpu", "memory"}
STEP_FIELDS = {"name", "run"}
DEPENDENCY_FIELDS = {"name", "version"}
ARTIFACT_FIELDS = {"name", "version", "path"}


def parse_pipeline_yaml(raw: str) -> Pipeline:
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise PipelineValidationError("pipeline must be a YAML mapping")

    _reject_unknown("pipeline", loaded, ROOT_FIELDS)
    _require("pipeline", loaded, {"name", "version", "jobs", "artifacts"})

    dependencies = [_parse_dependency(item, i) for i, item in enumerate(loaded.get("dependencies", []))]
    jobs = _parse_jobs(loaded["jobs"])
    artifacts = [_parse_artifact(item, i) for i, item in enumerate(loaded["artifacts"])]

    return Pipeline(
        name=str(loaded["name"]),
        version=str(loaded["version"]),
        dependencies=dependencies,
        jobs=jobs,
        artifacts=artifacts,
    )


def _parse_jobs(raw_jobs: Any) -> dict[str, Job]:
    if not isinstance(raw_jobs, dict) or not raw_jobs:
        raise PipelineValidationError("jobs must be a non-empty mapping")

    jobs: dict[str, Job] = {}
    for name, body in raw_jobs.items():
        if not isinstance(body, dict):
            raise PipelineValidationError(f"jobs.{name} must be a mapping")
        _reject_unknown(f"jobs.{name}", body, JOB_FIELDS)
        _require(f"jobs.{name}", body, {"runtime", "steps"})

        resources = body.get("resources", {})
        if not isinstance(resources, dict):
            raise PipelineValidationError(f"jobs.{name}.resources must be a mapping")
        _reject_unknown(f"jobs.{name}.resources", resources, RESOURCE_FIELDS)

        steps = [_parse_step(step, name, i) for i, step in enumerate(body["steps"])]
        needs = body.get("needs", [])
        if not isinstance(needs, list):
            raise PipelineValidationError(f"jobs.{name}.needs must be a list")

        jobs[str(name)] = Job(
            name=str(name),
            runtime=str(body["runtime"]),
            resources=Resources(
                cpu=float(resources.get("cpu", 1.0)),
                memory=str(resources.get("memory", "512Mi")),
            ),
            steps=steps,
            needs=[str(item) for item in needs],
        )
    return jobs


def _parse_step(raw_step: Any, job_name: str, index: int) -> Step:
    if not isinstance(raw_step, dict):
        raise PipelineValidationError(f"jobs.{job_name}.steps[{index}] must be a mapping")
    _reject_unknown(f"jobs.{job_name}.steps[{index}]", raw_step, STEP_FIELDS)
    _require(f"jobs.{job_name}.steps[{index}]", raw_step, STEP_FIELDS)
    return Step(name=str(raw_step["name"]), run=str(raw_step["run"]))


def _parse_dependency(raw_dep: Any, index: int) -> Dependency:
    if not isinstance(raw_dep, dict):
        raise PipelineValidationError(f"dependencies[{index}] must be a mapping")
    _reject_unknown(f"dependencies[{index}]", raw_dep, DEPENDENCY_FIELDS)
    _require(f"dependencies[{index}]", raw_dep, DEPENDENCY_FIELDS)
    return Dependency(name=str(raw_dep["name"]), version=str(raw_dep["version"]))


def _parse_artifact(raw_artifact: Any, index: int) -> Artifact:
    if not isinstance(raw_artifact, dict):
        raise PipelineValidationError(f"artifacts[{index}] must be a mapping")
    _reject_unknown(f"artifacts[{index}]", raw_artifact, ARTIFACT_FIELDS)
    _require(f"artifacts[{index}]", raw_artifact, ARTIFACT_FIELDS)
    return Artifact(
        name=str(raw_artifact["name"]),
        version=str(raw_artifact["version"]),
        path=str(raw_artifact["path"]),
    )


def _reject_unknown(path: str, body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise PipelineValidationError(f"{path}: unknown field(s): {', '.join(unknown)}")


def _require(path: str, body: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(body))
    if missing:
        raise PipelineValidationError(f"{path}: missing required field(s): {', '.join(missing)}")
