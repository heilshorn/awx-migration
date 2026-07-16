"""Export-format migration infrastructure.

Old export files are upgraded to the current format transparently when they are
read.  Each migration step ``migration_NNN.upgrade(document, kind)`` transforms
a document from format version ``NNN`` to ``NNN + 1``.  The runner
:func:`migrate_document` chains the required steps so a document at any older
version is brought up to the target version.

Currently only format version 1 exists, so :data:`LATEST_FORMAT` is ``1`` and
no step runs at the default target.  :mod:`lib.migrations.migration_001` is a
registered pass-through placeholder, ready for when version 2 is introduced.

This package depends only on the standard library so it can be imported from
any layer without creating cycles.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import migration_001

#: Highest export format version this build understands.
LATEST_FORMAT: int = 1

#: Maps a source format version to the callable that upgrades it by one.
#: ``migration_001`` (version 1 → 2) is registered ahead of time; it is only
#: invoked once ``LATEST_FORMAT`` reaches 2 or a caller passes ``target >= 2``.
STEPS: dict[int, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    1: migration_001.upgrade,
}


class MigrationError(RuntimeError):
    """Raised when a document cannot be migrated to the target version."""


def migrate_document(
    document: dict[str, Any],
    kind: str,
    *,
    target: int = LATEST_FORMAT,
    steps: Mapping[int, Callable[[dict[str, Any], str], dict[str, Any]]]
    | None = None,
) -> dict[str, Any]:
    """Migrate *document* up to *target* by chaining registered steps.

    The document's current version is read from its ``format_version`` field
    (defaulting to 1 when absent).  Each step from that version up to (but not
    including) *target* is applied in ascending order.

    Args:
        document: Parsed export document (type file or manifest).
        kind: Document kind, ``"type_file"`` or ``"manifest"``.
        target: Version to migrate to.  Defaults to :data:`LATEST_FORMAT`.
        steps: Step table to use.  Defaults to :data:`STEPS`; overridable for
            testing.

    Returns:
        The document at version *target*.  When it is already at *target* the
        input is returned unchanged.

    Raises:
        MigrationError: If the document's version is newer than *target*, or if
            a required migration step is missing.
    """
    table = STEPS if steps is None else steps
    version = int(document.get("format_version", 1))
    if version > target:
        raise MigrationError(
            f"Document format_version {version} is newer than the supported "
            f"target version {target}; upgrade the tool to read this file."
        )
    migrated = document
    while version < target:
        upgrade = table.get(version)
        if upgrade is None:
            raise MigrationError(
                f"No migration step registered for format version {version} "
                f"→ {version + 1}."
            )
        migrated = upgrade(migrated, kind)
        version += 1
    return migrated
