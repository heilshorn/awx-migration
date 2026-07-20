"""End-to-end roundtrip for a job template and its references.

Design doc §8: export a job template from a real AWX, import it back, and
verify the whitelisted fields — the ``inventory`` and ``project`` references in
particular — survive the roundtrip as natural keys (never IDs).  The flow runs
entirely through our library:

    Export → Validate → Import → Validate → Export → Validate → diff

Provisioning/cleanup (org → project → inventory → job template) is handled by
the ``provisioned_job_template`` fixture (see ``conftest.py``).  Skipped
automatically unless AWX_E2E_HOST and the awx CLI are available.
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


def test_job_template_export_import_roundtrip(
    e2e_client: AwxClient,
    provisioned_job_template: dict[str, str],
    tmp_path: Path,
) -> None:
    names = provisioned_job_template
    jt_name = names["job_template"]
    jt_type = OBJECT_TYPES["job_templates"]
    exporter = Exporter(
        e2e_client,
        [jt_type],
        tool_version=_TOOL_VERSION,
        awx_version=_AWX_VERSION,
    )
    validator = ExportValidator()

    # 1. Export just our job template (narrowed by name after fetching).
    dir_a = tmp_path / "export-a"
    summary_a = exporter.export_object(dir_a, jt_type, jt_name)
    assert summary_a.counts["job_templates"] == 1

    # 2. Validate the first bundle.
    result_a = validator.validate(dir_a)
    assert result_a.valid, result_a.errors
    assert not [w for w in result_a.warnings if "unknown field" in w], (
        result_a.warnings
    )

    obj_a = _read_single(dir_a, "job_templates", jt_name)
    # References are stored in canonical form (names, never internal IDs).
    # Org-scoped targets keep {name, organization}; no AWX 'type' leaks in.
    assert obj_a.fields["inventory"]["name"] == names["inventory"]
    assert obj_a.fields["project"]["name"] == names["project"]
    assert obj_a.fields["inventory"].get("organization") == names["organization"]
    assert "type" not in obj_a.fields["inventory"]
    assert not any(key.endswith("_id") for key in obj_a.fields)

    # 3. Import the bundle back.  Importer.import_path validates the bundle
    #    internally before importing, so the "Import → Validate" boundary is
    #    already covered; re-validating the unchanged dir_a here would be
    #    redundant.  The meaningful post-import validation is on the second
    #    export (step 5) below.
    importer = Importer(e2e_client)
    import_summary = importer.import_path(dir_a, on_conflict="update")
    assert not import_summary.errors, import_summary.errors

    # The job template still exists in AWX after the roundtrip.
    reimported = e2e_client.export("job_templates")
    assert any(o.fields.get("name") == jt_name for o in reimported)

    # 4. Export a second time.
    dir_b = tmp_path / "export-b"
    exporter.export_object(dir_b, jt_type, jt_name)

    # 5. Validate the second bundle.
    result_b = validator.validate(dir_b)
    assert result_b.valid, result_b.errors

    # 6. Roundtrip diff: identical exports prove the full data path is stable.
    obj_b = _read_single(dir_b, "job_templates", jt_name)
    assert obj_a.fields == obj_b.fields
