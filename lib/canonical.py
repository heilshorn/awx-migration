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
        fields: Whitelisted business field mapping.  References to other
            objects are stored in an AWX-agnostic canonical form — a plain name
            for a reference to a non-org-scoped type, or ``{"name": …,
            "organization": …}`` for an org-scoped one — never as internal IDs
            and never as raw AWX natural-key dicts.
        natural_key: Optional identity **metadata**, derived from the
            ``natural_key`` AWX supplies on export and reduced to plain names
            (e.g. ``{"name": "Deploy", "organization": "Default"}``).  It is
            kept separate from :attr:`fields` on purpose: it is the object's
            identity, not a business field.  When present it is the source of
            truth for :meth:`identity` (this cleanly covers objects such as job
            templates that carry no top-level ``organization`` field).
    """

    type: str
    fields: dict[str, Any] = field(default_factory=dict)
    natural_key: dict[str, Any] | None = None

    def identity(self, natural_key: Sequence[str]) -> tuple[Any, ...]:
        """Return this object's natural-key identity tuple.

        The identity is used for conflict detection and de-duplication.  The
        caller supplies the ordered natural-key field names
        (``ObjectType.natural_key``) so that this module stays free of any
        object-type knowledge.  Values are taken from :attr:`natural_key` when
        that metadata is present, otherwise from :attr:`fields`.

        Args:
            natural_key: Ordered field names forming the natural key of this
                object's type.

        Returns:
            Tuple of the values addressed by *natural_key*, in order.

        Raises:
            KeyError: If a natural-key field is absent from the identity source.
        """
        source = self.natural_key if self.natural_key is not None else self.fields
        try:
            return tuple(source[name] for name in natural_key)
        except KeyError as exc:
            missing = exc.args[0]
            raise KeyError(
                f"CanonicalObject(type={self.type!r}) is missing natural-key "
                f"field {missing!r}; available keys: {sorted(source)}"
            ) from exc
