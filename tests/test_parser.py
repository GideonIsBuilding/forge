from __future__ import annotations

import pytest

from engine.parser import PipelineValidationError, parse_pipeline_yaml


def test_parse_minimal_pipeline() -> None:
    pipeline = parse_pipeline_yaml(
        """
name: build-lib-core
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: test
        run: echo ok
artifacts:
  - name: lib-core
    version: 1.0.0
    path: ./out.tar.gz
"""
    )
    assert pipeline.name == "build-lib-core"
    assert list(pipeline.jobs) == ["build"]


def test_unknown_field_fails() -> None:
    with pytest.raises(PipelineValidationError):
        parse_pipeline_yaml(
            """
name: bad
version: 1.0.0
surprise: nope
jobs: {}
artifacts: []
"""
        )
