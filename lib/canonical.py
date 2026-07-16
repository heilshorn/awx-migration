"""Canonical, AWX-agnostic object model for export/import.

A :class:`CanonicalObject` is the tool's internal representation of a single
AWX object.  It flows between the export/import orchestration layers and the
AWX facade, but carries **no AWX-specific logic**: only an object-type key and
a plain field mapping.  References to other objects are expressed exclusively
via natural keys (names), never via internal AWX IDs.

This module deliberately depends on nothing else in the project so it can be
imported from any layer without creating cycles.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalObject:
    """An AWX object in the tool's stable, AWX-independent representation.

    Attributes:
        type: Registry key of the object type, e.g. ``"job_templates"``.
        fields: Whitelisted field mapping.  References to other objects are
            stored as natural keys (names) — e.g.
            ``{"organization": "Default"}`` — never as internal IDs.
    """

    type: str
    fields: dict[str, Any] = field(default_factory=dict)

    def identity(self, natural_key: Sequence[str]) -> tuple[Any, ...]:
        """Return this object's natural-key identity tuple.

        The identity is used for conflict detection and de-duplication.  It is
        built purely from :attr:`fields`; the caller supplies the ordered
        natural-key field names (``ObjectType.natural_key``) so that this
        module stays free of any object-type knowledge.

        Args:
            natural_key: Ordered field names forming the natural key of this
                object's type.

        Returns:
            Tuple of the field values addressed by *natural_key*, in order.

        Raises:
            KeyError: If a natural-key field is absent from :attr:`fields`.
        """
        try:
            return tuple(self.fields[name] for name in natural_key)
        except KeyError as exc:
            missing = exc.args[0]
            raise KeyError(
                f"CanonicalObject(type={self.type!r}) is missing natural-key "
                f"field {missing!r}; present fields: {sorted(self.fields)}"
            ) from exc
