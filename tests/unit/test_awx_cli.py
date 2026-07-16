"""Unit tests for lib.awx_cli — the awx binary wrapper (mocked subprocess)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from lib import awx_cli
from lib.awx_cli import AwxCli, AwxCliError


class _Result:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry delays instantaneous."""
    monkeypatch.setattr(awx_cli.time, "sleep", lambda *_: None)


# -- detect() ---------------------------------------------------------


def test_detect_finds_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(awx_cli.shutil, "which", lambda name: "/usr/bin/awx")
    cli = AwxCli.detect()
    assert cli.binary == "/usr/bin/awx"


def test_detect_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(awx_cli.shutil, "which", lambda name: None)
    with pytest.raises(AwxCliError):
        AwxCli.detect()


def test_init_with_explicit_binary_does_not_probe_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        awx_cli.shutil,
        "which",
        lambda name: pytest.fail("which should not be called"),
    )
    assert AwxCli("/opt/awx").binary == "/opt/awx"


# -- run() success paths ----------------------------------------------


def test_run_returns_stripped_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result(0, stdout="  hello  \n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli = AwxCli("/usr/bin/awx")
    out = cli.run(["organizations", "list"], env={"TOWER_HOST": "h"})

    assert out == "hello"
    assert captured["cmd"] == ["/usr/bin/awx", "organizations", "list"]
    # Supplemental env is merged over the process environment.
    assert captured["kwargs"]["env"]["TOWER_HOST"] == "h"


def test_run_passes_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["input"] = kwargs.get("input")
        return _Result(0, stdout="ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    AwxCli("/usr/bin/awx").run(["import"], stdin='{"a": 1}')
    assert captured["input"] == '{"a": 1}'


def test_run_without_env_leaves_env_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        return _Result(0, stdout="ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    AwxCli("/usr/bin/awx").run(["version"])
    assert captured["kwargs"]["env"] is None


# -- run() failure paths & retries ------------------------------------


def test_run_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return _Result(1, stderr="transient")
        return _Result(0, stdout="recovered")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = AwxCli("/usr/bin/awx").run(["export"], retries=3)
    assert out == "recovered"
    assert calls["n"] == 2


def test_run_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return _Result(2, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AwxCliError):
        AwxCli("/usr/bin/awx").run(["export"], retries=3)
    assert calls["n"] == 3


def test_run_retries_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AwxCliError):
        AwxCli("/usr/bin/awx").run(["export"], retries=2)
    assert calls["n"] == 2


def test_run_oserror_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AwxCliError):
        AwxCli("/usr/bin/awx").run(["export"], retries=3)
    assert calls["n"] == 1  # launch failure is terminal
