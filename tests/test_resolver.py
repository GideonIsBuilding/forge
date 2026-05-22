from __future__ import annotations

from registry.resolver import satisfies, select_highest


def test_caret_constraint() -> None:
    assert satisfies("1.2.3", "^1.0.0")
    assert not satisfies("2.0.0", "^1.0.0")


def test_tilde_constraint() -> None:
    assert satisfies("1.2.9", "~1.2.0")
    assert not satisfies("1.3.0", "~1.2.0")


def test_select_highest_matching_all_constraints() -> None:
    assert select_highest(["1.0.0", "1.2.0", "2.0.0"], ["^1.0.0", ">=1.1.0"]) == "1.2.0"
