from __future__ import annotations

from dataclasses import dataclass


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
