from __future__ import annotations

import pytest

from registry.resolver import (
    DependencyCycleError,
    VersionConflictError,
    resolve,
    satisfies,
    select_highest,
)


# ---------------------------------------------------------------------------
# satisfies — existing constraint tests
# ---------------------------------------------------------------------------

def test_caret_constraint() -> None:
    assert satisfies("1.2.3", "^1.0.0")
    assert not satisfies("2.0.0", "^1.0.0")


def test_tilde_constraint() -> None:
    assert satisfies("1.2.9", "~1.2.0")
    assert not satisfies("1.3.0", "~1.2.0")


def test_select_highest_matching_all_constraints() -> None:
    assert select_highest(["1.0.0", "1.2.0", "2.0.0"], ["^1.0.0", ">=1.1.0"]) == "1.2.0"


# ---------------------------------------------------------------------------
# resolve() — helpers
# ---------------------------------------------------------------------------

def _make_registry(packages: dict[str, dict[str, tuple[str, list[tuple[str, str]]]]]):
    """Build fetch_versions and fetch_meta callables from a dict.

    packages = {
        "name": {
            "1.0.0": ("sha256hex", [("dep_name", "constraint"), ...]),
        }
    }
    """
    def fetch_versions(name: str) -> list[str]:
        return list(packages.get(name, {}).keys())

    def fetch_meta(name: str, version: str) -> tuple[str, list[tuple[str, str]]]:
        return packages[name][version]

    return fetch_versions, fetch_meta


# ---------------------------------------------------------------------------
# resolve() — basic cases
# ---------------------------------------------------------------------------

def test_resolve_no_deps_returns_empty() -> None:
    fv, fm = _make_registry({})
    result = resolve([], fv, fm)
    assert result == {"resolved": []}


def test_resolve_single_direct_dep() -> None:
    fv, fm = _make_registry({"mylib": {"1.2.3": ("sha-123", [])}})
    result = resolve([("mylib", "^1.0.0")], fv, fm)
    assert result["resolved"] == [{"name": "mylib", "version": "1.2.3", "sha256": "sha-123"}]


def test_resolve_picks_highest_satisfying_version() -> None:
    fv, fm = _make_registry({
        "lib": {
            "1.0.0": ("sha-1", []),
            "1.5.0": ("sha-2", []),
            "2.0.0": ("sha-3", []),
        }
    })
    result = resolve([("lib", "^1.0.0")], fv, fm)
    assert result["resolved"][0]["version"] == "1.5.0"


def test_resolve_exact_version_constraint() -> None:
    fv, fm = _make_registry({"tool": {"1.0.0": ("sha-t", []), "2.0.0": ("sha-t2", [])}})
    result = resolve([("tool", "1.0.0")], fv, fm)
    assert result["resolved"][0]["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# resolve() — transitive walk
# ---------------------------------------------------------------------------

def test_resolve_transitive_dep() -> None:
    reg = {
        "bar": {"1.0.0": ("sha-bar", [("baz", "^3.0.0")])},
        "baz": {"3.1.0": ("sha-baz", [])},
    }
    fv, fm = _make_registry(reg)
    result = resolve([("bar", "1.0.0")], fv, fm)
    names = {e["name"] for e in result["resolved"]}
    assert names == {"bar", "baz"}


def test_resolve_deep_transitive_chain() -> None:
    # a -> b -> c
    reg = {
        "a": {"1.0.0": ("sha-a", [("b", "^1.0.0")])},
        "b": {"1.0.0": ("sha-b", [("c", "^1.0.0")])},
        "c": {"1.0.0": ("sha-c", [])},
    }
    fv, fm = _make_registry(reg)
    result = resolve([("a", "1.0.0")], fv, fm)
    names = {e["name"] for e in result["resolved"]}
    assert names == {"a", "b", "c"}


def test_resolve_shared_transitive_dep_deduplicated() -> None:
    # Both foo and bar depend on baz — baz should appear only once.
    reg = {
        "foo": {"1.0.0": ("sha-foo", [("baz", "^1.0.0")])},
        "bar": {"1.0.0": ("sha-bar", [("baz", "^1.0.0")])},
        "baz": {"1.5.0": ("sha-baz", [])},
    }
    fv, fm = _make_registry(reg)
    result = resolve([("foo", "1.0.0"), ("bar", "1.0.0")], fv, fm)
    baz_entries = [e for e in result["resolved"] if e["name"] == "baz"]
    assert len(baz_entries) == 1


# ---------------------------------------------------------------------------
# resolve() — VersionConflictError
# ---------------------------------------------------------------------------

def test_resolve_missing_package_raises_conflict() -> None:
    fv, fm = _make_registry({})
    with pytest.raises(VersionConflictError):
        resolve([("ghost", "^1.0.0")], fv, fm)


def test_resolve_no_satisfying_version_raises_conflict() -> None:
    fv, fm = _make_registry({"lib": {"1.0.0": ("sha", [])}})
    with pytest.raises(VersionConflictError):
        resolve([("lib", "^2.0.0")], fv, fm)


def test_resolve_transitive_conflict_raises_conflict() -> None:
    # bar@1.0.0 needs baz@^2.0.0 but only baz@1.x exists
    reg = {
        "bar": {"1.0.0": ("sha-bar", [("baz", "^2.0.0")])},
        "baz": {"1.9.0": ("sha-baz", [])},
    }
    fv, fm = _make_registry(reg)
    with pytest.raises(VersionConflictError):
        resolve([("bar", "1.0.0")], fv, fm)


def test_resolve_incompatible_direct_constraints_raises_conflict() -> None:
    # Both callers want lib but with incompatible ranges
    reg = {"lib": {"1.0.0": ("sha1", []), "2.0.0": ("sha2", [])}}
    fv, fm = _make_registry(reg)
    with pytest.raises(VersionConflictError):
        # ^1.0.0 picks 1.x; then ^2.0.0 constraint cannot be satisfied by 1.x
        resolve([("lib", "^1.0.0"), ("lib", "^2.0.0")], fv, fm)


# ---------------------------------------------------------------------------
# resolve() — DependencyCycleError
# ---------------------------------------------------------------------------

def test_resolve_direct_cycle_raises() -> None:
    reg = {
        "foo": {"1.0.0": ("sha-foo", [("bar", "^1.0.0")])},
        "bar": {"1.0.0": ("sha-bar", [("foo", "^1.0.0")])},
    }
    fv, fm = _make_registry(reg)
    with pytest.raises(DependencyCycleError):
        resolve([("foo", "1.0.0")], fv, fm)


def test_resolve_self_dep_cycle_raises() -> None:
    reg = {"foo": {"1.0.0": ("sha", [("foo", "^1.0.0")])}}
    fv, fm = _make_registry(reg)
    with pytest.raises(DependencyCycleError):
        resolve([("foo", "1.0.0")], fv, fm)
