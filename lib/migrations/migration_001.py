"""Format migration step 001: export format version 1 → 2.

This is a **placeholder** that is currently a pass-through.  It is registered in
the migration runner so the full upgrade chain exists and is exercised by the
tests, but it is not applied while the latest format version is 1 (the runner
only invokes steps needed to reach its target version).

When export format version 2 is introduced, fill in the real transformation
here — read a version-1 document and return its version-2 equivalent — and bump
``LATEST_FORMAT`` in :mod:`lib.migrations`.
"""

from __future__ import annotations

from typing import Any


def upgrade(document: dict[str, Any], kind: str) -> dict[str, Any]:
    """Upgrade a single format-version-1 document to version 2.

    Args:
        document: Parsed export document (a type file or a manifest).
        kind: Document kind, either ``"type_file"`` or ``"manifest"``.

    Returns:
        The upgraded document.  Currently a pass-through: the input is
        returned unchanged because there is no structural difference yet.
    """
    return document
