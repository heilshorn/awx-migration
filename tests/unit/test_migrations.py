"""Unit tests for lib.migrations — the export-format migration runner."""

from __future__ import annotations

from typing import Any

import pytest

from lib.migrations import (
    LATEST_FORMAT,
    STEPS,
    MigrationError,
    migrate_document,
    migration_001,
)


def test_latest_format_is_one() -> None:
    """Only format version 1 exists so far."""
    assert LATEST_FORMAT == 1


def test_default_target_is_noop_for_current_version() -> None:
    """A current-version document is returned unchanged at the default target."""
    doc = {"format_version": 1, "kind": "type_file", "objects": []}
    assert migrate_document(doc, "type_file") == doc


def test_migration_001_is_passthrough() -> None:
    """The registered step 001 currently makes no changes."""
    doc = {"format_version": 1, "value": 42}
    assert migration_001.upgrade(doc, "type_file") == doc


def test_step_001_is_registered() -> None:
    """migration_001 is wired into the runner's step table."""
    assert STEPS[1] is migration_001.upgrade


def test_runner_chains_steps_in_order() -> None:
    """Steps are applied in ascending version order up to the target."""

    def step_1_to_2(doc: dict[str, Any], kind: str) -> dict[str, Any]:
        return {**doc, "trace": [*doc.get("trace", []), "1->2"]}

    def step_2_to_3(doc: dict[str, Any], kind: str) -> dict[str, Any]:
        return {**doc, "trace": [*doc.get("trace", []), "2->3"]}

    steps = {1: step_1_to_2, 2: step_2_to_3}
    result = migrate_document(
        {"format_version": 1}, "type_file", target=3, steps=steps
    )
    assert result["trace"] == ["1->2", "2->3"]


def test_runner_passes_kind_to_steps() -> None:
    """The document kind is forwarded to each step."""
    seen: list[str] = []

    def step(doc: dict[str, Any], kind: str) -> dict[str, Any]:
        seen.append(kind)
        return doc

    migrate_document(
        {"format_version": 1}, "manifest", target=2, steps={1: step}
    )
    assert seen == ["manifest"]


def test_version_newer_than_target_is_rejected() -> None:
    """A document from the future cannot be downgraded."""
    with pytest.raises(MigrationError):
        migrate_document({"format_version": 5}, "type_file", target=1)


def test_missing_step_is_reported() -> None:
    """A gap in the migration chain raises rather than silently stopping."""
    with pytest.raises(MigrationError):
        migrate_document(
            {"format_version": 1}, "type_file", target=2, steps={}
        )


def test_absent_format_version_defaults_to_one() -> None:
    """A document without format_version is treated as version 1."""
    doc = {"kind": "type_file", "objects": []}
    assert migrate_document(doc, "type_file") == doc
