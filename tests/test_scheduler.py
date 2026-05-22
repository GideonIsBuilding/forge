from __future__ import annotations

import pytest

from engine.scheduler import JobCycleError, topological_batches


def test_topological_batches_parallel_jobs() -> None:
    batches = topological_batches({"build": [], "lint": [], "package": ["build", "lint"]})
    assert batches == [["build", "lint"], ["package"]]


def test_topological_batches_detects_cycle() -> None:
    with pytest.raises(JobCycleError):
        topological_batches({"a": ["b"], "b": ["a"]})
