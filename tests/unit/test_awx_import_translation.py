"""Unit tests for AwxCliClient.import_objects() / exists() — canonical → AWX."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from lib.awx_cli import AwxCliError
from lib.awx_client import AwxCliClient, AwxClientError, ImportResult
from lib.awx_connection import AwxConnection
from lib.awx_objects import OBJECT_TYPES, ObjectType, Relation
from lib.canonical import CanonicalObject


class FakeCli:
    """AwxCli stand-in recording calls (incl. stdin) and returning output."""

    def __init__(self, output: str = "", error: Exception | None = None):
        self.output = output
        self.error = error
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
        self.calls.append(
            {"args": list(args), "env": dict(env or {}), "stdin": stdin}
        )
        if self.error is not None:
            raise self.error
        return self.output


def _conn() -> AwxConnection:
    return AwxConnection(
        host="https://awx.example", username="admin", password="pw"
    )


def _client(
    output: str = "", error: Exception | None = None, object_types=OBJECT_TYPES
) -> tuple[AwxCliClient, FakeCli]:
    cli = FakeCli(output=output, error=error)
    return AwxCliClient(_conn(), cli=cli, object_types=object_types), cli


def _job_template() -> CanonicalObject:
    return CanonicalObject(
        "job_templates",
        {
            "name": "Deploy",
            "organization": "Default",
            "inventory": {"name": "Linux", "organization": "Default"},
            "project": {"name": "Infra", "organization": "Default"},
            "playbook": "deploy.yml",
            # Fields that must NOT survive translation (not in the whitelist):
            "id": 42,
            "url": "/api/v2/job_templates/42/",
            "summary_fields": {"x": 1},
        },
    )


def _sent_bundle(cli: FakeCli) -> dict[str, Any]:
    """Parse the JSON bundle piped to ``awx import``."""
    assert cli.calls, "awx import was not called"
    return json.loads(cli.calls[0]["stdin"])


# -- bundle structure & translation -----------------------------------


def test_import_calls_awx_import_with_bundle() -> None:
    client, cli = _client()
    client.import_objects("job_templates", [_job_template()])
    assert cli.calls[0]["args"] == ["import"]
    bundle = _sent_bundle(cli)
    assert list(bundle) == ["job_templates"]
    assert isinstance(bundle["job_templates"], list)
    assert len(bundle["job_templates"]) == 1


def test_references_use_natural_keys() -> None:
    client, cli = _client()
    client.import_objects("job_templates", [_job_template()])
    asset = _sent_bundle(cli)["job_templates"][0]
    # Non-org-scoped reference: {type, name}.
    assert asset["organization"] == {"type": "organization", "name": "Default"}
    # Org-scoped references nest their organization, as awx import expects.
    assert asset["inventory"] == {
        "type": "inventory",
        "name": "Linux",
        "organization": {"type": "organization", "name": "Default"},
    }
    assert asset["project"] == {
        "type": "project",
        "name": "Infra",
        "organization": {"type": "organization", "name": "Default"},
    }


def test_no_ids_are_written() -> None:
    client, cli = _client()
    client.import_objects("job_templates", [_job_template()])
    asset = _sent_bundle(cli)["job_templates"][0]
    assert "id" not in asset
    assert "url" not in asset
    assert not any(key.endswith("_id") for key in asset)


def test_only_whitelist_fields_are_written() -> None:
    client, cli = _client()
    client.import_objects("job_templates", [_job_template()])
    asset = _sent_bundle(cli)["job_templates"][0]
    allowed = set(OBJECT_TYPES["job_templates"].fields)
    # 'natural_key' is identity metadata the asset must carry, not a business
    # field; everything else stays within the whitelist.
    assert set(asset) - {"natural_key"} <= allowed
    assert "summary_fields" not in asset
    assert asset["name"] == "Deploy"
    assert asset["playbook"] == "deploy.yml"


def test_asset_carries_its_own_natural_key() -> None:
    client, cli = _client()
    obj = CanonicalObject(
        "inventories",
        {"name": "Demo", "organization": "Default"},
        natural_key={"name": "Demo", "organization": "Default"},
    )
    client.import_objects("inventories", [obj])
    asset = _sent_bundle(cli)["inventory"][0]
    assert asset["natural_key"] == {
        "type": "inventory",
        "name": "Demo",
        "organization": {"type": "organization", "name": "Default"},
    }


def test_inventory_import_bundle_uses_awx_singular_key() -> None:
    # The import bundle must use AWX's asset key ("inventory"), not the plural
    # registry key, or awx import silently ignores the objects.
    client, cli = _client()
    obj = CanonicalObject(
        "inventories", {"name": "Demo", "organization": "Default"}
    )
    client.import_objects("inventories", [obj])
    bundle = _sent_bundle(cli)
    assert list(bundle) == ["inventory"]
    assert bundle["inventory"][0]["name"] == "Demo"
    assert bundle["inventory"][0]["organization"] == {
        "type": "organization",
        "name": "Default",
    }


def test_multiple_objects_become_multiple_assets() -> None:
    client, cli = _client()
    objects = [
        CanonicalObject("organizations", {"name": "Default"}),
        CanonicalObject("organizations", {"name": "Eng"}),
    ]
    client.import_objects("organizations", objects)
    bundle = _sent_bundle(cli)
    assert [a["name"] for a in bundle["organizations"]] == ["Default", "Eng"]


def test_many_relation_becomes_list_of_natural_keys() -> None:
    things = ObjectType(
        key="things",
        cli_flag="--things",
        filename="things.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name", "members"),
        relations=(Relation("members", "organizations", many=True),),
    )
    orgs = ObjectType(
        key="organizations",
        cli_flag="--organizations",
        filename="organizations.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name",),
        awx_type_name="organization",
    )
    client, cli = _client(
        object_types={"things": things, "organizations": orgs}
    )
    obj = CanonicalObject("things", {"name": "T", "members": ["A", "B"]})
    client.import_objects("things", [obj])
    asset = _sent_bundle(cli)["things"][0]
    assert asset["members"] == [
        {"type": "organization", "name": "A"},
        {"type": "organization", "name": "B"},
    ]


# -- conflict policy infrastructure -----------------------------------


def test_default_update_has_no_warning() -> None:
    client, _ = _client()
    result = client.import_objects("organizations", [_org()])
    assert result.warnings == []
    assert isinstance(result, ImportResult)


def test_skip_and_fail_warn_that_enforcement_is_deferred() -> None:
    for policy in ("skip", "fail"):
        client, _ = _client()
        result = client.import_objects(
            "organizations", [_org()], on_conflict=policy
        )
        assert any(policy in w for w in result.warnings)


def test_invalid_conflict_policy_raises() -> None:
    client, _ = _client()
    with pytest.raises(ValueError):
        client.import_objects("organizations", [_org()], on_conflict="bogus")


# -- error & edge paths -----------------------------------------------


def test_failed_awx_import_is_reported_not_raised() -> None:
    client, _ = _client(error=AwxCliError("boom"))
    result = client.import_objects("organizations", [_org()])
    assert result.errors
    assert "boom" in result.errors[0]


def test_empty_object_list_does_not_call_cli() -> None:
    client, cli = _client()
    result = client.import_objects("job_templates", [])
    assert result == ImportResult()
    assert cli.calls == []  # nothing imported → no CLI invocation


def test_unknown_type_raises_on_import() -> None:
    client, _ = _client()
    with pytest.raises(AwxClientError):
        client.import_objects("does_not_exist", [_org()])


# -- exists() ---------------------------------------------------------


def _org_export(*names: str) -> str:
    return json.dumps(
        {"organizations": [{"id": i, "name": n} for i, n in enumerate(names)]}
    )


def test_exists_true_when_object_present() -> None:
    client, cli = _client(output=_org_export("Default", "Eng"))
    assert client.exists("organizations", ("Default",)) is True
    # exists queries via `awx export`.
    assert cli.calls[0]["args"] == ["export", "--organizations"]


def test_exists_false_when_object_absent() -> None:
    client, _ = _client(output=_org_export("Default"))
    assert client.exists("organizations", ("Missing",)) is False


def test_exists_false_on_empty_type() -> None:
    client, _ = _client(output=json.dumps({"organizations": []}))
    assert client.exists("organizations", ("Default",)) is False


def test_exists_false_on_invalid_json_no_exception() -> None:
    client, _ = _client(output="not json")
    assert client.exists("organizations", ("Default",)) is False


def test_exists_false_on_cli_error_no_exception() -> None:
    client, _ = _client(error=AwxCliError("connection refused"))
    assert client.exists("organizations", ("Default",)) is False


def test_exists_matches_composite_natural_key() -> None:
    export = json.dumps(
        {
            "job_templates": [
                {"name": "Deploy", "organization": {"name": "Default"}}
            ]
        }
    )
    client, _ = _client(output=export)
    assert client.exists("job_templates", ("Deploy", "Default")) is True
    assert client.exists("job_templates", ("Deploy", "Other")) is False


def test_exists_unknown_type_raises() -> None:
    client, _ = _client()
    with pytest.raises(AwxClientError):
        client.exists("does_not_exist", ("x",))


def _org() -> CanonicalObject:
    return CanonicalObject("organizations", {"name": "Default"})
