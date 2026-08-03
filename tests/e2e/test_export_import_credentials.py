"""End-to-end roundtrip for a job template's credential references.

Exercises Option 1 (credential references only, no credential objects/secrets):
a job template with an attached ``Machine`` credential is exported, and the
credential reference must survive as an AWX-agnostic natural key, be re-attached
on import, and — when the referenced credential is absent — be reported clearly
while the job template still imports.

Skipped automatically unless AWX_E2E_HOST and the awx CLI are available (see
``conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.awx_client import AwxClient
from lib.awx_objects import OBJECT_TYPES
from lib.canonical import CanonicalObject
from lib.export_format import read_type_file, write_type_file
from lib.exporter import Exporter
from lib.importer import Importer

pytestmark = pytest.mark.e2e

_TOOL_VERSION = "e2e"
_AWX_VERSION = "e2e"
_META = {
    "tool_version": _TOOL_VERSION,
    "awx_version": _AWX_VERSION,
    "exported_at": "2026-08-03T00:00:00Z",
}


def _read_jt(bundle: Path, name: str) -> CanonicalObject:
    type_file = read_type_file(bundle / OBJECT_TYPES["job_templates"].filename)
    matches = [o for o in type_file.objects if o.fields.get("name") == name]
    assert len(matches) == 1, f"expected one JT named {name!r}"
    return matches[0]


def test_credential_reference_roundtrips_and_reattaches(
    e2e_client: AwxClient,
    provisioned_jt_with_credential: dict[str, str],
    tmp_path: Path,
) -> None:
    names = provisioned_jt_with_credential
    jt_name, cred_name = names["job_template"], names["credential"]
    jt_type = OBJECT_TYPES["job_templates"]
    exporter = Exporter(
        e2e_client, [jt_type],
        tool_version=_TOOL_VERSION, awx_version=_AWX_VERSION,
    )

    # 1. Export: the credential reference is captured as a natural key.
    dir_a = tmp_path / "export-a"
    summary_a = exporter.export_object(dir_a, jt_type, jt_name)
    assert summary_a.counts["job_templates"] == 1
    assert any(cred_name in c for c in summary_a.referenced_credentials)

    obj_a = _read_jt(dir_a, jt_name)
    creds = obj_a.fields.get("credentials")
    assert creds and creds[0]["name"] == cred_name
    assert creds[0]["credential_type"] == {"name": "Machine", "kind": "ssh"}
    # No AWX 'type' markers or internal IDs leak into the canonical form.
    assert "type" not in creds[0]

    # 2. Import back: credential exists, so nothing is stripped, no NoneType.
    import_summary = Importer(e2e_client).import_path(dir_a, on_conflict="update")
    assert not import_summary.errors, import_summary.errors
    assert import_summary.missing_credentials == []

    # 3. Re-export: the credential is still attached (roundtrip is stable).
    dir_b = tmp_path / "export-b"
    exporter.export_object(dir_b, jt_type, jt_name)
    obj_b = _read_jt(dir_b, jt_name)
    assert obj_a.fields == obj_b.fields


def test_missing_credential_is_reported_and_jt_still_imports(
    e2e_client: AwxClient,
    provisioned_jt_with_credential: dict[str, str],
    tmp_path: Path,
) -> None:
    names = provisioned_jt_with_credential
    jt_name = names["job_template"]
    jt_type = OBJECT_TYPES["job_templates"]
    exporter = Exporter(
        e2e_client, [jt_type],
        tool_version=_TOOL_VERSION, awx_version=_AWX_VERSION,
    )

    # Export, then rewrite the bundle to reference a credential that does not
    # exist in the target.
    bundle = tmp_path / "export"
    exporter.export_object(bundle, jt_type, jt_name)
    obj = _read_jt(bundle, jt_name)
    ghost = {
        "name": "awxmig-e2e-ghost-cred",
        "credential_type": {"name": "Machine", "kind": "ssh"},
        "organization": None,
    }
    obj.fields["credentials"] = [ghost]
    write_type_file(
        bundle / jt_type.filename, "job_templates", [obj], **_META
    )

    summary = Importer(e2e_client).import_path(bundle, on_conflict="update")

    # The job template imports; the absent credential is reported, not fatal.
    assert not summary.errors, summary.errors
    assert any("ghost-cred" in m for m in summary.missing_credentials)
    assert any(
        "ghost-cred" in w and "not present in the target" in w
        for w in summary.warnings
    )
