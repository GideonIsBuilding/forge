from __future__ import annotations

import pytest

from engine.parser import PipelineValidationError, parse_pipeline_yaml

_MINIMAL = """
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


def test_parse_minimal_pipeline() -> None:
    pipeline = parse_pipeline_yaml(_MINIMAL)
    assert pipeline.name == "build-lib-core"
    assert pipeline.version == "1.0.0"
    assert list(pipeline.jobs) == ["build"]
    assert pipeline.dependencies == []


def test_parse_full_pipeline_with_dependencies_and_needs() -> None:
    pipeline = parse_pipeline_yaml("""
name: build-lib-http
version: 1.2.3
dependencies:
  - name: lib-core
    version: "^1.0.0"
jobs:
  test:
    runtime: alpine:3.18
    steps:
      - name: run-tests
        run: sh ./test.sh
  package:
    runtime: alpine:3.18
    needs: [test]
    resources:
      cpu: 2.0
      memory: 1Gi
    steps:
      - name: pack
        run: tar czf out.tar.gz src/
artifacts:
  - name: lib-http
    version: 1.2.3
    path: ./out.tar.gz
""")
    assert pipeline.name == "build-lib-http"
    assert len(pipeline.dependencies) == 1
    assert pipeline.dependencies[0].name == "lib-core"
    assert pipeline.jobs["package"].needs == ["test"]
    assert pipeline.jobs["package"].resources.cpu == 2.0
    assert pipeline.jobs["package"].resources.memory == "1Gi"


# ---------------------------------------------------------------------------
# Line-aware errors
# ---------------------------------------------------------------------------

def test_line_number_in_unknown_field_error() -> None:
    with pytest.raises(PipelineValidationError, match=r"line \d+"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    typo_field: oops
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_line_number_in_missing_field_error() -> None:
    with pytest.raises(PipelineValidationError, match=r"line \d+"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


# ---------------------------------------------------------------------------
# Unknown and missing fields
# ---------------------------------------------------------------------------

def test_unknown_root_field_fails() -> None:
    with pytest.raises(PipelineValidationError, match="unknown field"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
surprise: nope
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_missing_runtime_fails() -> None:
    with pytest.raises(PipelineValidationError, match="runtime"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_unknown_job_field_fails() -> None:
    with pytest.raises(PipelineValidationError, match="unknown field"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    secret: yes
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_unknown_step_field_fails() -> None:
    with pytest.raises(PipelineValidationError, match="unknown field"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: echo hi
        extra: nope
artifacts: []
""")


# ---------------------------------------------------------------------------
# Semver validation
# ---------------------------------------------------------------------------

def test_invalid_pipeline_version_fails() -> None:
    with pytest.raises(PipelineValidationError, match="semver"):
        parse_pipeline_yaml("""
name: bad
version: "not-semver"
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_invalid_artifact_version_fails() -> None:
    with pytest.raises(PipelineValidationError, match="semver"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: echo hi
artifacts:
  - name: lib
    version: "^1.0.0"
    path: ./out.tar.gz
""")


# ---------------------------------------------------------------------------
# Resource constraints
# ---------------------------------------------------------------------------

def test_negative_cpu_fails() -> None:
    with pytest.raises(PipelineValidationError, match="positive"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    resources:
      cpu: -1
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_zero_cpu_fails() -> None:
    with pytest.raises(PipelineValidationError, match="positive"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    resources:
      cpu: 0
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_invalid_memory_format_fails() -> None:
    with pytest.raises(PipelineValidationError, match="memory"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    resources:
      memory: "lots"
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_valid_memory_formats() -> None:
    for mem in ("512Mi", "1Gi", "256M", "4096"):
        pipeline = parse_pipeline_yaml(f"""
name: test
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    resources:
      memory: "{mem}"
    steps:
      - name: s
        run: echo hi
artifacts: []
""")
        assert pipeline.jobs["build"].resources.memory == mem


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def test_empty_steps_list_fails() -> None:
    with pytest.raises(PipelineValidationError, match="non-empty"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    steps: []
artifacts: []
""")


def test_empty_run_command_fails() -> None:
    with pytest.raises(PipelineValidationError, match="run"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: "   "
artifacts: []
""")


# ---------------------------------------------------------------------------
# needs cross-reference
# ---------------------------------------------------------------------------

def test_needs_unknown_job_fails() -> None:
    with pytest.raises(PipelineValidationError, match="unknown job"):
        parse_pipeline_yaml("""
name: bad
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    needs: [ghost]
    steps:
      - name: s
        run: echo hi
artifacts: []
""")


def test_needs_valid_sibling_passes() -> None:
    pipeline = parse_pipeline_yaml("""
name: test
version: 1.0.0
jobs:
  lint:
    runtime: alpine:3.18
    steps:
      - name: l
        run: echo lint
  build:
    runtime: alpine:3.18
    needs: [lint]
    steps:
      - name: b
        run: echo build
artifacts: []
""")
    assert pipeline.jobs["build"].needs == ["lint"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_dependencies_null_treated_as_empty() -> None:
    pipeline = parse_pipeline_yaml("""
name: test
version: 1.0.0
dependencies: null
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: echo hi
artifacts: []
""")
    assert pipeline.dependencies == []


def test_non_yaml_fails() -> None:
    with pytest.raises(PipelineValidationError, match="invalid YAML"):
        parse_pipeline_yaml(": : : not yaml : :")


def test_empty_pipeline_name_fails() -> None:
    with pytest.raises(PipelineValidationError, match="name"):
        parse_pipeline_yaml("""
name: "   "
version: 1.0.0
jobs:
  build:
    runtime: alpine:3.18
    steps:
      - name: s
        run: echo hi
artifacts: []
""")
