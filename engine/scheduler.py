from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass


class SchedulerError(ValueError):
    pass


class JobCycleError(SchedulerError):
    pass


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    needs: tuple[str, ...]


def topological_batches(jobs: dict[str, list[str]]) -> list[list[str]]:
    """Return runnable job batches where each batch can run in parallel."""
    missing = {dep for deps in jobs.values() for dep in deps if dep not in jobs}
    if missing:
        raise SchedulerError(f"unknown job dependency: {', '.join(sorted(missing))}")

    indegree = {name: len(needs) for name, needs in jobs.items()}
    dependents: dict[str, list[str]] = defaultdict(list)
    for name, needs in jobs.items():
        for dep in needs:
            dependents[dep].append(name)

    ready = deque(sorted(name for name, degree in indegree.items() if degree == 0))
    batches: list[list[str]] = []
    visited = 0

    while ready:
        batch = list(ready)
        ready.clear()
        batches.append(batch)
        for name in batch:
            visited += 1
            for dependent in sorted(dependents[name]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)

    if visited != len(jobs):
        cycle_nodes = sorted(name for name, degree in indegree.items() if degree > 0)
        raise JobCycleError(f"job dependency cycle detected: {' -> '.join(cycle_nodes)}")

    return batches
