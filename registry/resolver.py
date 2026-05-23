from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class ResolutionError(ValueError):
    pass


class VersionConflictError(ResolutionError):
    pass


class DependencyCycleError(ResolutionError):
    pass


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> "Version":
        parts = raw.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError(f"invalid semver: {raw}")
        return cls(*(int(part) for part in parts))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def satisfies(version: str, constraint: str) -> bool:
    parsed = Version.parse(version)
    constraint = constraint.strip()
    if constraint.startswith("^"):
        base = Version.parse(constraint[1:])
        upper = Version(base.major + 1, 0, 0) if base.major > 0 else Version(0, base.minor + 1, 0)
        return base <= parsed < upper
    if constraint.startswith("~"):
        base = Version.parse(constraint[1:])
        upper = Version(base.major, base.minor + 1, 0)
        return base <= parsed < upper
    if any(constraint.startswith(op) for op in (">=", "<=", ">", "<")):
        return _satisfies_comparators(parsed, constraint)
    return parsed == Version.parse(constraint)


def select_highest(versions: list[str], constraints: list[str]) -> str:
    candidates = sorted((Version.parse(version) for version in versions), reverse=True)
    for candidate in candidates:
        raw = str(candidate)
        if all(satisfies(raw, constraint) for constraint in constraints):
            return raw
    raise VersionConflictError(f"no version satisfies constraints: {', '.join(constraints)}")


def resolve(
    direct_deps: list[tuple[str, str]],
    fetch_versions: Callable[[str], list[str]],
    fetch_meta: Callable[[str, str], tuple[str, list[tuple[str, str]]]],
) -> dict:
    """Resolve direct_deps transitively and return a lockfile dict.

    Args:
        direct_deps:   [(name, constraint), ...] from pipeline.dependencies.
        fetch_versions: callable(name) -> [version_str, ...]
        fetch_meta:     callable(name, version) -> (sha256, [(dep_name, dep_constraint), ...])

    Returns:
        {"resolved": [{"name": ..., "version": ..., "sha256": ...}, ...]}

    Raises:
        VersionConflictError  if no version satisfies the accumulated constraints.
        DependencyCycleError  if a cycle is detected in the dependency graph.
    """
    # Accumulate all constraints as the graph is walked.
    constraints: dict[str, list[str]] = {}
    for name, constraint in direct_deps:
        constraints.setdefault(name, []).append(constraint)

    resolved: dict[str, dict] = {}
    # DFS stack — a package in here is currently being resolved.
    # Encountering it again before it finishes means there is a cycle.
    in_stack: set[str] = set()

    def _resolve_one(name: str) -> None:
        # Cycle check must come first: resolved[name] is set before transitive
        # deps are walked, so a cycle would silently return via the resolved guard.
        if name in in_stack:
            raise DependencyCycleError(
                f"dependency cycle detected involving {name!r}"
            )

        if name in resolved:
            # Already resolved — verify that any newly accumulated constraints
            # are still satisfied by the chosen version.
            ver = resolved[name]["version"]
            bad = [c for c in constraints.get(name, []) if not satisfies(ver, c)]
            if bad:
                raise VersionConflictError(
                    f"{name}@{ver} does not satisfy: {', '.join(bad)}"
                )
            return

        in_stack.add(name)

        versions = fetch_versions(name)
        if not versions:
            raise VersionConflictError(f"no versions published for {name!r}")

        version = select_highest(versions, constraints.get(name, []))
        sha256, transitive = fetch_meta(name, version)
        resolved[name] = {"name": name, "version": version, "sha256": sha256}

        for dep_name, dep_constraint in transitive:
            constraints.setdefault(dep_name, []).append(dep_constraint)
            _resolve_one(dep_name)

        in_stack.discard(name)

    for name in list(constraints.keys()):
        _resolve_one(name)

    return {"resolved": list(resolved.values())}


def _satisfies_comparators(version: Version, constraint: str) -> bool:
    for part in constraint.split():
        if part.startswith(">="):
            if not version >= Version.parse(part[2:]):
                return False
        elif part.startswith("<="):
            if not version <= Version.parse(part[2:]):
                return False
        elif part.startswith(">"):
            if not version > Version.parse(part[1:]):
                return False
        elif part.startswith("<"):
            if not version < Version.parse(part[1:]):
                return False
    return True
