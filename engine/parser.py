from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
import yaml.resolver


class PipelineValidationError(ValueError):
    """Raised when a pipeline YAML file does not match the Forge schema."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Allowed field sets and validation patterns
# ---------------------------------------------------------------------------

ROOT_FIELDS = {"name", "version", "dependencies", "jobs", "artifacts"}
JOB_FIELDS = {"runtime", "resources", "steps", "needs"}
RESOURCE_FIELDS = {"cpu", "memory"}
STEP_FIELDS = {"name", "run"}
DEPENDENCY_FIELDS = {"name", "version"}
ARTIFACT_FIELDS = {"name", "version", "path"}

# Keys injected by _LineLoader — never written by the user.
_RESERVED = {"__line__"}

# Exact version coordinates must be X.Y.Z; constraint strings (^1.0.0) are the resolver's concern.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Accepts plain bytes (512) or a standard SI/IEC unit suffix (512Mi, 1Gi, 4M, …).
_MEMORY_RE = re.compile(r"^\d+([KMGTPkmgtp]i?)?$")


# ---------------------------------------------------------------------------
# Line-tracking YAML loader
#
# yaml.safe_load() discards all position information after parsing. To get
# line-aware error messages we override construct_mapping in a SafeLoader
# subclass so that every dict node carries a __line__ key. This is a single-
# pass approach — no need to walk a separate node tree in parallel.
# ---------------------------------------------------------------------------

class _LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping_with_line(loader: _LineLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    pairs = loader.construct_pairs(node, deep=True)
    mapping = dict(pairs)
    mapping["__line__"] = node.start_mark.line + 1  # convert to 1-indexed
    return mapping


_LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_with_line,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_pipeline_yaml(raw: str) -> Pipeline:
    try:
        loaded = yaml.load(raw, Loader=_LineLoader)  # _LineLoader is a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise PipelineValidationError(f"invalid YAML: {exc}") from exc

    if not isinstance(loaded, dict):
        raise PipelineValidationError("pipeline must be a YAML mapping")

    _reject_unknown("pipeline", loaded, ROOT_FIELDS)
    _require("pipeline", loaded, {"name", "version", "jobs", "artifacts"})

    name = str(loaded["name"]).strip()
    if not name:
        raise PipelineValidationError("pipeline.name must not be empty")

    version = str(loaded["version"])
    if not _SEMVER_RE.match(version):
        raise PipelineValidationError(
            f"pipeline.version must be exact semver (X.Y.Z), got: {version!r}"
        )

    # dependencies is optional; explicit `null` is treated as absent.
    raw_deps = loaded.get("dependencies") or []
    if not isinstance(raw_deps, list):
        raise PipelineValidationError("pipeline.dependencies must be a list")

    raw_artifacts = loaded["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise PipelineValidationError("pipeline.artifacts must be a list")

    dependencies = [_parse_dependency(item, i) for i, item in enumerate(raw_deps)]
    jobs = _parse_jobs(loaded["jobs"])
    artifacts = [_parse_artifact(item, i) for i, item in enumerate(raw_artifacts)]

    _validate_job_needs(jobs)

    return Pipeline(
        name=name,
        version=version,
        dependencies=dependencies,
        jobs=jobs,
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# Sub-parsers
# ---------------------------------------------------------------------------

def _parse_jobs(raw_jobs: Any) -> dict[str, Job]:
    if not isinstance(raw_jobs, dict):
        raise PipelineValidationError("jobs must be a mapping")

    # _LineLoader injects __line__ into every mapping, so filter it out when
    # counting real job names.
    job_names = [k for k in raw_jobs if k not in _RESERVED]
    if not job_names:
        raise PipelineValidationError("jobs must have at least one job")

    jobs: dict[str, Job] = {}
    for name in job_names:
        body = raw_jobs[name]
        if not isinstance(body, dict):
            raise PipelineValidationError(f"jobs.{name} must be a mapping")
        _reject_unknown(f"jobs.{name}", body, JOB_FIELDS)
        _require(f"jobs.{name}", body, {"runtime", "steps"})

        raw_steps = body["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PipelineValidationError(f"jobs.{name}.steps must be a non-empty list")

        resources = body.get("resources", {})
        if resources is None:
            resources = {}
        if not isinstance(resources, dict):
            raise PipelineValidationError(f"jobs.{name}.resources must be a mapping")
        _reject_unknown(f"jobs.{name}.resources", resources, RESOURCE_FIELDS)

        cpu = _parse_cpu(resources.get("cpu", 1.0), f"jobs.{name}.resources.cpu")
        memory = _parse_memory(
            resources.get("memory", "512Mi"), f"jobs.{name}.resources.memory"
        )

        needs = body.get("needs", [])
        if not isinstance(needs, list):
            raise PipelineValidationError(f"jobs.{name}.needs must be a list")

        steps = [_parse_step(step, name, i) for i, step in enumerate(raw_steps)]

        jobs[str(name)] = Job(
            name=str(name),
            runtime=str(body["runtime"]),
            resources=Resources(cpu=cpu, memory=memory),
            steps=steps,
            needs=[str(item) for item in needs],
        )
    return jobs


def _parse_step(raw_step: Any, job_name: str, index: int) -> Step:
    if not isinstance(raw_step, dict):
        raise PipelineValidationError(f"jobs.{job_name}.steps[{index}] must be a mapping")
    _reject_unknown(f"jobs.{job_name}.steps[{index}]", raw_step, STEP_FIELDS)
    _require(f"jobs.{job_name}.steps[{index}]", raw_step, STEP_FIELDS)

    run = str(raw_step["run"]).strip()
    if not run:
        raise PipelineValidationError(
            f"jobs.{job_name}.steps[{index}].run must not be empty"
        )

    return Step(name=str(raw_step["name"]), run=run)


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

    version = str(raw_artifact["version"])
    if not _SEMVER_RE.match(version):
        raise PipelineValidationError(
            f"artifacts[{index}].version must be exact semver (X.Y.Z), got: {version!r}"
        )

    return Artifact(
        name=str(raw_artifact["name"]),
        version=version,
        path=str(raw_artifact["path"]),
    )


# ---------------------------------------------------------------------------
# Value validators
# ---------------------------------------------------------------------------

def _parse_cpu(raw: Any, path: str) -> float:
    if raw is None:
        raise PipelineValidationError(f"{path} must not be null")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise PipelineValidationError(f"{path} must be a number, got: {raw!r}")
    if value <= 0:
        raise PipelineValidationError(f"{path} must be positive, got: {value}")
    return value


def _parse_memory(raw: Any, path: str) -> str:
    if raw is None:
        raise PipelineValidationError(f"{path} must not be null")
    value = str(raw)
    if not _MEMORY_RE.match(value):
        raise PipelineValidationError(
            f"{path} must be a number with an optional unit (e.g. 512Mi, 1Gi, 256M), got: {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Cross-reference validation
# ---------------------------------------------------------------------------

def _validate_job_needs(jobs: dict[str, Job]) -> None:
    job_names = set(jobs)
    for name, job in jobs.items():
        unknown = sorted(set(job.needs) - job_names)
        if unknown:
            raise PipelineValidationError(
                f"jobs.{name}.needs: references unknown job(s): {', '.join(unknown)}"
            )


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def _reject_unknown(path: str, body: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed - _RESERVED)
    if unknown:
        line_hint = f" (line {body['__line__']})" if "__line__" in body else ""
        raise PipelineValidationError(
            f"{path}{line_hint}: unknown field(s): {', '.join(unknown)}"
        )


def _require(path: str, body: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - (set(body) - _RESERVED))
    if missing:
        line_hint = f" (line {body['__line__']})" if "__line__" in body else ""
        raise PipelineValidationError(
            f"{path}{line_hint}: missing required field(s): {', '.join(missing)}"
        )
