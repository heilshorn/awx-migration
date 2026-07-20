"""Fixtures for the opt-in end-to-end suite (design doc §8).

These tests exercise the real ``awx`` CLI against a real AWX instance through
our own library (``Exporter``/``Importer``/``ExportValidator``/``AwxClient``).
They are **opt-in** and skip automatically unless both

* the ``AWX_E2E_HOST`` environment variable is set, and
* the ``awx`` binary is available on ``PATH``.

Connection is taken straight from the environment (no cluster / kubectl
involved) so the tests can point at any reachable AWX:

===========================  ====================================================
``AWX_E2E_HOST``             AWX base URL, e.g. ``https://awx.example:30080``
``AWX_E2E_TOKEN``            OAuth2 token (highest priority), or …
``AWX_E2E_USERNAME`` / …     … username + ``AWX_E2E_PASSWORD``
``AWX_E2E_INSECURE``         set to a truthy value to disable TLS verification
``AWX_E2E_PROJECT_SCM_URL``  git URL for the provisioned test project
``AWX_E2E_PLAYBOOK``         playbook name used by the provisioned job template
``AWX_E2E_SYNC_TIMEOUT``     seconds to wait for the initial project sync (180)
===========================  ====================================================

Test data is **self-provisioned and torn down** by these fixtures.  Every run
uses a fresh, unique organization (``awxmig-e2e-org-<uuid>``) so that parallel
runs never collide, and every object carries the ``awxmig-e2e-`` prefix.
Provisioning and cleanup use the ``awx`` CLI directly (allowed scaffolding);
the tests themselves touch AWX only through our library.  Cleanup is
best-effort and never turns a passing test into a failure.

Prerequisite for the job-template test: AWX must be able to run the project's
initial SCM update, i.e. clone the git repository from inside its execution
environment.  Behind a proxy this means AWX itself needs the proxy in its job
environment (Settings → Job Settings → "Extra Environment Variables":
``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY``) — a proxy on the host alone does
not reach the execution environment where project updates run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from lib.awx_cli import AwxCli
from lib.awx_client import AwxClient, make_client
from lib.awx_connection import AwxConnection

log: logging.Logger = logging.getLogger("awx-migration.e2e")

#: Every object created by the E2E suite carries this prefix so cleanup can
#: never touch pre-existing data.
E2E_PREFIX: str = "awxmig-e2e-"

_DEFAULT_SCM_URL: str = "https://github.com/ansible/test-playbooks.git"
_DEFAULT_PLAYBOOK: str = "debug.yml"

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: Project SCM-update polling.  A creating a git-backed project triggers an
#: automatic project_update; the job template cannot reference a playbook until
#: that update has finished successfully.
_SYNC_TIMEOUT_DEFAULT: float = 180.0
_SYNC_POLL_INTERVAL: float = 3.0
_SYNC_SUCCESS: frozenset[str] = frozenset({"successful", "ok"})
_SYNC_FAILURE: frozenset[str] = frozenset(
    {"failed", "error", "canceled", "missing"}
)


def _bool_env(name: str) -> bool:
    """Return whether environment variable *name* holds a truthy value."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _unique_suffix() -> str:
    """Return a short, run-unique suffix for object names."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Connection / client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_connection() -> AwxConnection:
    """Resolve the AWX connection from the environment, or skip.

    Skips the test when the E2E preconditions are not met so the suite is a
    no-op on machines without AWX (CI stays green).
    """
    host = os.environ.get("AWX_E2E_HOST")
    if not host:
        pytest.skip("AWX_E2E_HOST is not set — skipping E2E tests")
    if shutil.which("awx") is None:
        pytest.skip("the 'awx' CLI is not on PATH — skipping E2E tests")

    verify_ssl = not _bool_env("AWX_E2E_INSECURE")
    token = os.environ.get("AWX_E2E_TOKEN")
    if token:
        return AwxConnection(
            host=host.rstrip("/"), token=token, verify_ssl=verify_ssl
        )

    username = os.environ.get("AWX_E2E_USERNAME")
    password = os.environ.get("AWX_E2E_PASSWORD")
    if username and password:
        return AwxConnection(
            host=host.rstrip("/"),
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        )

    pytest.skip(
        "no E2E credentials — set AWX_E2E_TOKEN or "
        "AWX_E2E_USERNAME + AWX_E2E_PASSWORD"
    )


@pytest.fixture(scope="session")
def e2e_cli() -> AwxCli:
    """Return the detected ``awx`` binary wrapper used for provisioning."""
    return AwxCli.detect()


@pytest.fixture
def e2e_client(e2e_connection: AwxConnection, e2e_cli: AwxCli) -> AwxClient:
    """Return the library AWX client under test (real CLI, real AWX)."""
    return make_client(e2e_connection, cli=e2e_cli)


# ---------------------------------------------------------------------------
# Provisioning helpers (awx CLI — scaffolding only)
# ---------------------------------------------------------------------------


def _awx_env(conn: AwxConnection) -> dict[str, str]:
    """Build the AWX environment for a raw ``awx`` CLI call.

    Mirrors :meth:`lib.awx_client.AwxCliClient._build_env`; kept local so
    provisioning does not reach into the facade's internals.
    """
    verify = "true" if conn.verify_ssl else "false"
    env: dict[str, str] = {
        "TOWER_HOST": conn.host,
        "CONTROLLER_HOST": conn.host,
        "TOWER_VERIFY_SSL": verify,
        "CONTROLLER_VERIFY_SSL": verify,
    }
    if conn.token:
        env["TOWER_OAUTH_TOKEN"] = conn.token
        env["CONTROLLER_OAUTH_TOKEN"] = conn.token
    else:
        env["TOWER_USERNAME"] = conn.username or ""
        env["CONTROLLER_USERNAME"] = conn.username or ""
        env["TOWER_PASSWORD"] = conn.password or ""
        env["CONTROLLER_PASSWORD"] = conn.password or ""
    return env


def _create(
    cli: AwxCli, env: dict[str, str], resource: str, **fields: Any
) -> dict[str, Any]:
    """Create an AWX *resource* via the CLI and return its JSON object.

    Raises:
        AssertionError: If the CLI does not return a parseable object with an
            ``id`` (a setup failure — surfaced clearly, not silently skipped).
    """
    args: list[str] = [resource, "create", "-f", "json"]
    for key, value in fields.items():
        args.extend([f"--{key}", str(value)])
    out = cli.run(args, env=env)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:  # pragma: no cover - real-AWX only
        raise AssertionError(
            f"awx {resource} create did not return JSON: {exc}\n{out}"
        ) from exc
    assert isinstance(data, dict) and "id" in data, (
        f"awx {resource} create returned an unexpected object: {data!r}"
    )
    return data


def _wait_for_project_sync(
    cli: AwxCli, env: dict[str, str], project_id: Any
) -> None:
    """Poll a project until its initial SCM update finishes successfully.

    Creating a git-backed project kicks off an automatic ``project_update``;
    until it succeeds AWX rejects a job template that references a playbook, and
    the project cannot be deleted.  This polls the project's ``status`` (rather
    than sleeping a fixed amount) and returns as soon as it is successful.

    Raises:
        AssertionError: If the sync ends in a failure state or does not finish
            within ``AWX_E2E_SYNC_TIMEOUT`` seconds.
    """
    timeout = float(
        os.environ.get("AWX_E2E_SYNC_TIMEOUT", _SYNC_TIMEOUT_DEFAULT)
    )
    deadline = time.monotonic() + timeout
    last_status = ""
    while True:
        out = cli.run(
            ["projects", "get", str(project_id), "-f", "json"], env=env
        )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:  # pragma: no cover - real-AWX only
            raise AssertionError(
                f"awx projects get did not return JSON: {exc}\n{out}"
            ) from exc
        last_status = str(data.get("status", "")).lower()
        if last_status in _SYNC_SUCCESS:
            return
        if last_status in _SYNC_FAILURE:
            last_update = data.get("summary_fields", {}).get("last_update", {})
            explanation = (
                last_update.get("job_explanation")
                if isinstance(last_update, dict)
                else ""
            )
            raise AssertionError(
                f"project {project_id} sync ended with status "
                f"{last_status!r} (scm_url={data.get('scm_url')!r})"
                f"{'; ' + explanation if explanation else ''}. "
                "Inspect the project_update job in AWX for the cause — most "
                "often git is missing in the execution environment or the SCM "
                "URL is unreachable from AWX."
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout:.0f}s waiting for project "
                f"{project_id} to sync (last status: {last_status!r})"
            )
        time.sleep(_SYNC_POLL_INTERVAL)


def _delete_quiet(
    cli: AwxCli, env: dict[str, str], resource: str, obj_id: Any
) -> None:
    """Delete an AWX object best-effort; never raise.

    Cleanup must not turn a passing test into a failure, so every error is
    swallowed and only logged.
    """
    try:
        cli.run([resource, "delete", str(obj_id)], env=env)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort by design
        log.warning(
            "E2E cleanup: could not delete %s id=%s: %s", resource, obj_id, exc
        )


# ---------------------------------------------------------------------------
# Provisioned-object fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provisioned_inventory(
    e2e_connection: AwxConnection, e2e_cli: AwxCli
) -> Iterator[dict[str, str]]:
    """Provision a unique organization + inventory, yield their names.

    Tears both down (inventory first, then organization) on the way out.
    """
    env = _awx_env(e2e_connection)
    suffix = _unique_suffix()
    org_name = f"{E2E_PREFIX}org-{suffix}"
    inv_name = f"{E2E_PREFIX}inv-{suffix}"
    created: list[tuple[str, Any]] = []
    try:
        org = _create(
            e2e_cli,
            env,
            "organizations",
            name=org_name,
            description="awxmig e2e test organization",
        )
        created.append(("organizations", org["id"]))
        inv = _create(
            e2e_cli,
            env,
            "inventories",
            name=inv_name,
            organization=org["id"],
            description="awxmig e2e test inventory",
        )
        created.append(("inventories", inv["id"]))
        yield {"organization": org_name, "inventory": inv_name}
    finally:
        for resource, obj_id in reversed(created):
            _delete_quiet(e2e_cli, env, resource, obj_id)


@pytest.fixture
def provisioned_job_template(
    e2e_connection: AwxConnection, e2e_cli: AwxCli
) -> Iterator[dict[str, str]]:
    """Provision org → project → inventory → job template, yield their names.

    The project's initial SCM update is awaited (see
    :func:`_wait_for_project_sync`) before the job template is created, because
    AWX rejects a job template whose playbook cannot yet be resolved.
    Everything is torn down in reverse dependency order.
    """
    env = _awx_env(e2e_connection)
    suffix = _unique_suffix()
    org_name = f"{E2E_PREFIX}org-{suffix}"
    proj_name = f"{E2E_PREFIX}proj-{suffix}"
    inv_name = f"{E2E_PREFIX}inv-{suffix}"
    jt_name = f"{E2E_PREFIX}jt-{suffix}"
    scm_url = os.environ.get("AWX_E2E_PROJECT_SCM_URL", _DEFAULT_SCM_URL)
    playbook = os.environ.get("AWX_E2E_PLAYBOOK", _DEFAULT_PLAYBOOK)
    created: list[tuple[str, Any]] = []
    try:
        org = _create(
            e2e_cli,
            env,
            "organizations",
            name=org_name,
            description="awxmig e2e test organization",
        )
        created.append(("organizations", org["id"]))
        project = _create(
            e2e_cli,
            env,
            "projects",
            name=proj_name,
            organization=org["id"],
            scm_type="git",
            scm_url=scm_url,
        )
        created.append(("projects", project["id"]))
        # The job template's playbook is only resolvable once the project's
        # initial SCM update has completed — wait for it before creating it.
        _wait_for_project_sync(e2e_cli, env, project["id"])
        inv = _create(
            e2e_cli,
            env,
            "inventories",
            name=inv_name,
            organization=org["id"],
            description="awxmig e2e test inventory",
        )
        created.append(("inventories", inv["id"]))
        job_template = _create(
            e2e_cli,
            env,
            "job_templates",
            name=jt_name,
            job_type="run",
            inventory=inv["id"],
            project=project["id"],
            playbook=playbook,
        )
        created.append(("job_templates", job_template["id"]))
        yield {
            "organization": org_name,
            "project": proj_name,
            "inventory": inv_name,
            "job_template": jt_name,
            "playbook": playbook,
        }
    finally:
        for resource, obj_id in reversed(created):
            _delete_quiet(e2e_cli, env, resource, obj_id)
