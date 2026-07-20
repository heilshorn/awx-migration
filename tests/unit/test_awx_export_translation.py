"""Unit tests for AwxCliClient.export() — AWX JSON → CanonicalObject."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from lib.awx_client import AwxCliClient, AwxClientError
from lib.awx_connection import AwxConnection
from lib.awx_objects import OBJECT_TYPES, ObjectType, Relation


class FakeCli:
    """AwxCli stand-in returning a preset output and recording calls."""

    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        timeout: int = 120,
        retries: int = 3,
    ) -> str:
        self.calls.append({"args": list(args), "env": dict(env or {})})
        return self.output


def _conn() -> AwxConnection:
    return AwxConnection(
        host="https://awx.example", username="admin", password="pw"
    )


def _client(output: str, object_types=OBJECT_TYPES) -> AwxCliClient:
    return AwxCliClient(_conn(), cli=FakeCli(output), object_types=object_types)


def _jt_export(**overrides: Any) -> str:
    """One job_templates export document with a realistic awxkit raw object.

    Mirrors ``awx export --job_templates``: no top-level ``organization`` (it
    lives only in ``natural_key``), references are nested natural keys.
    """
    obj: dict[str, Any] = {
        "id": 42,
        "url": "/api/v2/job_templates/42/",
        "type": "job_template",
        "created": "2020-01-01T00:00:00Z",
        "modified": "2021-01-01T00:00:00Z",
        "summary_fields": {"created_by": {"id": 1}},
        "related": {"launch": "/api/..."},
        "name": "Deploy",
        "description": "deploy things",
        "job_type": "run",
        "inventory": {
            "name": "Linux",
            "organization": {"name": "Default", "type": "organization"},
            "type": "inventory",
        },
        "project": {
            "name": "Infra",
            "organization": {"name": "Default", "type": "organization"},
            "type": "project",
        },
        "playbook": "deploy.yml",
        "secret_field": "should-not-appear",
        "natural_key": {
            "name": "Deploy",
            "organization": {"name": "Default", "type": "organization"},
            "type": "job_template",
        },
    }
    obj.update(overrides)
    return json.dumps({"job_templates": [obj]})


# -- whitelist --------------------------------------------------------


def test_whitelist_drops_internal_and_unknown_fields() -> None:
    client = _client(_jt_export())
    obj = client.export("job_templates")[0]
    for forbidden in (
        "id",
        "url",
        "type",
        "created",
        "modified",
        "summary_fields",
        "related",
        "secret_field",
    ):
        assert forbidden not in obj.fields


def test_whitelisted_scalar_fields_are_kept() -> None:
    client = _client(_jt_export())
    obj = client.export("job_templates")[0]
    assert obj.fields["name"] == "Deploy"
    assert obj.fields["description"] == "deploy things"
    assert obj.fields["job_type"] == "run"
    assert obj.fields["playbook"] == "deploy.yml"


def test_missing_whitelisted_field_is_absent() -> None:
    doc = json.loads(_jt_export())
    del doc["job_templates"][0]["description"]
    obj = _client(json.dumps(doc)).export("job_templates")[0]
    assert "description" not in obj.fields


# -- references -------------------------------------------------------


def test_references_become_natural_keys() -> None:
    client = _client(_jt_export())
    obj = client.export("job_templates")[0]
    # Reference to a non-org-scoped type collapses to a bare name; references
    # to org-scoped types keep {name, organization} (names only, no AWX type).
    assert obj.fields["inventory"] == {"name": "Linux", "organization": "Default"}
    assert obj.fields["project"] == {"name": "Infra", "organization": "Default"}
    # Job templates carry no top-level organization; identity comes from the
    # natural_key metadata instead.
    assert obj.natural_key == {"name": "Deploy", "organization": "Default"}


def test_natural_key_metadata_is_separate_from_fields() -> None:
    obj = _client(_jt_export()).export("job_templates")[0]
    assert obj.natural_key == {"name": "Deploy", "organization": "Default"}
    assert "natural_key" not in obj.fields


def test_job_template_org_filter_uses_natural_key_metadata() -> None:
    # Job templates carry no top-level organization; org filtering must fall
    # back to the natural_key metadata.
    doc = json.dumps(
        {
            "job_templates": [
                {
                    "name": "A",
                    "natural_key": {
                        "name": "A",
                        "organization": {
                            "name": "Default",
                            "type": "organization",
                        },
                        "type": "job_template",
                    },
                },
                {
                    "name": "B",
                    "natural_key": {
                        "name": "B",
                        "organization": {
                            "name": "Other",
                            "type": "organization",
                        },
                        "type": "job_template",
                    },
                },
            ]
        }
    )
    objs = _client(doc).export("job_templates", organization="Default")
    assert [o.fields["name"] for o in objs] == ["A"]


def test_reference_id_is_never_emitted() -> None:
    client = _client(_jt_export(inventory=42))  # raw integer id
    obj = client.export("job_templates")[0]
    assert obj.fields["inventory"] is None
    assert "inventory_id" not in obj.fields


def test_many_relation_maps_each_reference() -> None:
    things = ObjectType(
        key="things",
        cli_flag="--things",
        filename="things.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name", "members"),
        relations=(Relation("members", "organizations", many=True),),
    )
    doc = json.dumps(
        {
            "things": [
                {
                    "id": 1,
                    "name": "T",
                    "members": [{"name": "A"}, {"name": "B"}, 5],
                }
            ]
        }
    )
    obj = _client(doc, object_types={"things": things}).export("things")[0]
    assert obj.fields["members"] == ["A", "B", None]


# -- canonical object -------------------------------------------------


def test_export_returns_canonical_object_with_type() -> None:
    obj = _client(_jt_export()).export("job_templates")[0]
    assert obj.type == "job_templates"


# -- organization filter ----------------------------------------------


def test_org_filter_narrows_org_scoped_type() -> None:
    doc = json.dumps(
        {
            "job_templates": [
                {"name": "A", "organization": {"name": "Default"}},
                {"name": "B", "organization": {"name": "Other"}},
            ]
        }
    )
    objs = _client(doc).export("job_templates", organization="Default")
    assert [o.fields["name"] for o in objs] == ["A"]


def test_org_filter_not_applied_to_non_org_scoped_type() -> None:
    doc = json.dumps(
        {"organizations": [{"name": "Default"}, {"name": "Other"}]}
    )
    objs = _client(doc).export("organizations", organization="Default")
    assert sorted(o.fields["name"] for o in objs) == ["Default", "Other"]


# -- CLI wiring & error paths -----------------------------------------


def test_export_uses_correct_cli_flag_and_env() -> None:
    cli = FakeCli(_jt_export())
    client = AwxCliClient(_conn(), cli=cli, object_types=OBJECT_TYPES)
    client.export("job_templates")
    assert cli.calls[0]["args"] == ["export", "--job_templates"]
    assert cli.calls[0]["env"]["TOWER_HOST"] == "https://awx.example"


def test_unknown_type_raises() -> None:
    with pytest.raises(AwxClientError):
        _client("{}").export("does_not_exist")


def test_invalid_json_raises() -> None:
    with pytest.raises(AwxClientError):
        _client("not json").export("job_templates")


def test_unexpected_shape_raises() -> None:
    with pytest.raises(AwxClientError):
        _client('"a bare string"').export("job_templates")


def test_empty_export_returns_empty_list() -> None:
    assert _client('{"job_templates": []}').export("job_templates") == []


# -- AWX asset-type key mapping ---------------------------------------


def test_inventory_export_reads_awx_singular_key() -> None:
    # awxkit keys inventories in the singular ("inventory"), not "inventories".
    doc = json.dumps(
        {
            "inventory": [
                {"name": "Demo", "organization": {"name": "Default"}}
            ]
        }
    )
    objs = _client(doc).export("inventories")
    assert [o.fields["name"] for o in objs] == ["Demo"]
    assert objs[0].fields["organization"] == "Default"


def test_inventory_export_falls_back_to_registry_key() -> None:
    # Robustness: a payload keyed by the plural registry key still parses.
    doc = json.dumps({"inventories": [{"name": "Demo"}]})
    objs = _client(doc).export("inventories")
    assert [o.fields["name"] for o in objs] == ["Demo"]
