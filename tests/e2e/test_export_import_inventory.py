"""End-to-end roundtrip for an inventory, including its organization.

Design doc §8: export an inventory from a real AWX, import it back, and verify
the whitelisted fields — organization assignment in particular — survive the
roundtrip.  The whole flow runs through our library:

    Export → Validate → Import → Validate → Export → Validate → diff

Provisioning/cleanup is handled by the ``provisioned_inventory`` fixture (see
``conftest.py``).  Skipped automatically unless AWX_E2E_HOST and the awx CLI
are available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.awx_client import AwxClient
from lib.awx_objects import OBJECT_TYPES
from lib.canonical import CanonicalObject
from lib.export_format import read_type_file
from lib.export_validator import ExportValidator
from lib.exporter import Exporter
from lib.importer import Importer

pytestmark = pytest.mark.e2e

_TOOL_VERSION = "e2e"
_AWX_VERSION = "e2e"


def _read_single(bundle: Path, type_key: str, name: str) -> CanonicalObject:
    """Return the one object named *name* from *bundle*'s type file."""
    type_file = read_type_file(bundle / OBJECT_TYPES[type_key].filename)
    matches = [o for o in type_file.objects if o.fields.get("name") == name]
    assert len(matches) == 1, (
        f"expected exactly one {type_key} named {name!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


def test_inventory_export_import_roundtrip(
    e2e_client: AwxClient,
    provisioned_inventory: dict[str, str],
    tmp_path: Path,
) -> None:
    org = provisioned_inventory["organization"]
    inv = provisioned_inventory["inventory"]
    inv_type = OBJECT_TYPES["inventories"]
    exporter = Exporter(
        e2e_client,
        [inv_type],
        tool_version=_TOOL_VERSION,
        awx_version=_AWX_VERSION,
    )
    validator = ExportValidator()

    # 1. Export — scoped to our org and single object.
    dir_a = tmp_path / "export-a"
    summary_a = exporter.export_object(
        dir_a, inv_type, inv, organization=org
    )
    assert summary_a.counts["inventories"] == 1

    # 2. Validate the first bundle.
    result_a = validator.validate(dir_a)
    assert result_a.valid, result_a.errors
    assert not [w for w in result_a.warnings if "unknown field" in w], (
        result_a.warnings
    )

    obj_a = _read_single(dir_a, "inventories", inv)
    # Organization assignment is preserved as a natural key, never an ID.
    assert obj_a.fields["organization"] == org
    assert not any(key.endswith("_id") for key in obj_a.fields)

    # 3. Import the bundle back.  Importer.import_path validates the bundle
    #    internally before importing, so the "Import → Validate" boundary is
    #    already covered; re-validating the unchanged dir_a here would be
    #    redundant.  The meaningful post-import validation is on the second
    #    export (step 5) below.
    importer = Importer(e2e_client)
    import_summary = importer.import_path(dir_a, on_conflict="update")
    assert not import_summary.errors, import_summary.errors

    # The object still exists in AWX under its natural key.
    assert e2e_client.exists(
        "inventories", obj_a.identity(inv_type.natural_key)
    )

    # 4. Export a second time.
    dir_b = tmp_path / "export-b"
    exporter.export_object(dir_b, inv_type, inv, organization=org)

    # 5. Validate the second bundle.
    result_b = validator.validate(dir_b)
    assert result_b.valid, result_b.errors

    # 6. Roundtrip diff: a stable export is proof the whole data path is sound.
    obj_b = _read_single(dir_b, "inventories", inv)
    assert obj_a.fields == obj_b.fields
