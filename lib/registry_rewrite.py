"""Execution Environment registry rewrite for awx-migration.

Rewrites image registry prefixes in AWX Execution Environment records
after a database restore.  Changes are applied exclusively via the AWX
Django ORM (``awx-manage shell``) — no direct SQL updates are made.

The rewrite must run *after* the AWX web pod is Running and connected to
the restored database.  At that point the pod's credentials are consistent
with the imported secrets, and the Django ORM can reach the database.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

from .kubectl import Kubectl, KubectlError

log: logging.Logger = logging.getLogger("awx-migration")

# ---------------------------------------------------------------------------
# Python script executed inside the AWX pod via ``awx-manage shell -c``.
#
# Placeholders {source} and {target} are replaced with repr() strings before
# the code is sent to the pod, so quoting is always correct.  Every other
# brace in the script is doubled ({{ / }}) to survive str.format().
# ---------------------------------------------------------------------------
_REWRITE_SCRIPT: str = """\
import json
import sys
from awx.main.models import ExecutionEnvironment

_source = {source}
_target = {target}

_results = []
try:
    _ees = list(ExecutionEnvironment.objects.all())
except Exception as _exc:
    print(json.dumps({{"error": str(_exc), "results": []}}))
    sys.exit(1)

for _ee in _ees:
    _image = _ee.image or ""
    if _image.startswith(_source):
        _new_image = _target + _image[len(_source):]
        try:
            _ee.image = _new_image
            _ee.save(update_fields=["image"])
            _results.append({{
                "name": _ee.name,
                "old_image": _image,
                "new_image": _new_image,
                "updated": True,
            }})
        except Exception as _save_exc:
            _results.append({{
                "name": _ee.name,
                "old_image": _image,
                "new_image": _new_image,
                "error": str(_save_exc),
                "updated": False,
            }})
    else:
        _results.append({{"name": _ee.name, "image": _image, "updated": False}})

print(json.dumps({{"results": _results}}))
"""


# ---------------------------------------------------------------------------
# Read-only script: list the distinct, non-empty image references of every
# Execution Environment.  Used by the registry backup to determine which
# images are actually in use (so only those are saved).
# ---------------------------------------------------------------------------
_LIST_IMAGES_SCRIPT: str = """\
import json
from awx.main.models import ExecutionEnvironment

_images = sorted({
    ee.image for ee in ExecutionEnvironment.objects.all() if ee.image
})
print(json.dumps({"images": _images}))
"""


class RegistryRewriteError(RuntimeError):
    """Raised on any registry rewrite operation failure."""


def _extract_shell_json(raw: str) -> dict[str, Any]:
    """Extract the JSON result object from ``awx-manage shell`` output.

    Django's ``shell --command`` may emit startup diagnostics to stdout before
    the script's own ``print()`` call.  This scans lines from the bottom upward
    and returns the first line that parses as a JSON object.

    Args:
        raw: Raw stdout captured from the ``awx-manage shell`` command.

    Returns:
        Parsed JSON dict produced by the executed script.

    Raises:
        RegistryRewriteError: If no valid JSON object line is found.
    """
    for line in reversed(raw.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                continue
    raise RegistryRewriteError(
        "No JSON result found in awx-manage shell output. "
        f"Raw output (last 500 chars): {raw[-500:]!r}"
    )


def list_execution_environment_images(kubectl: Kubectl) -> list[str]:
    """Return the distinct image references used by all Execution Environments.

    Runs a read-only ``awx-manage shell`` script inside the AWX web pod and
    reuses :func:`_extract_shell_json` to parse the result.  No database rows
    are modified.

    Args:
        kubectl: Kubectl wrapper bound to the AWX namespace.

    Returns:
        Sorted list of distinct, non-empty EE image references.

    Raises:
        RegistryRewriteError: If the web pod cannot be reached, the command
                              fails, or the output cannot be parsed.
    """
    try:
        pod = kubectl.web_pod()
    except KubectlError as exc:
        raise RegistryRewriteError(
            f"Cannot locate AWX web pod to list EE images: {exc}"
        ) from exc

    try:
        raw = kubectl.exec(
            pod,
            ["awx-manage", "shell", "-c", _LIST_IMAGES_SCRIPT],
            timeout=RegistryRewrite.SHELL_TIMEOUT,
        )
    except KubectlError as exc:
        raise RegistryRewriteError(
            f"awx-manage shell failed while listing EE images in pod "
            f"'{pod}': {exc}"
        ) from exc

    parsed = _extract_shell_json(raw)
    images: list[str] = parsed.get("images", [])
    log.info("Discovered %d distinct Execution Environment image(s)", len(images))
    return images


@dataclasses.dataclass(frozen=True)
class RegistryRewriteConfig:
    """Immutable configuration for a single registry address rewrite.

    Attributes:
        source: Registry prefix to replace, e.g. ``"10.6.207.31:30500"``.
        target: Replacement registry prefix,
                e.g. ``"registry.example.local:30500"``.
    """

    source: str
    target: str

    def __post_init__(self) -> None:
        """Validate that both *source* and *target* are non-empty strings.

        Raises:
            RegistryRewriteError: If either field is blank.
        """
        if not self.source.strip():
            raise RegistryRewriteError(
                "Registry rewrite 'source' must not be empty"
            )
        if not self.target.strip():
            raise RegistryRewriteError(
                "Registry rewrite 'target' must not be empty"
            )


class RegistryRewrite:
    """Rewrites AWX Execution Environment image references after a restore.

    All EE records in the database are inspected.  For each record whose
    ``image`` field starts with ``config.source``, the prefix is replaced
    with ``config.target``.  Only the registry/host part is changed; the
    image name and tag remain intact.

    Changes are applied via ``awx-manage shell -c`` inside the AWX web pod
    so the AWX Django ORM — not raw SQL — performs every write.

    Example::

        cfg = RegistryRewriteConfig(
            source="10.6.207.31:30500",
            target="registry.example.local:30500",
        )
        rw = RegistryRewrite(kubectl, cfg)
        rw.rewrite_execution_environments()
    """

    #: Timeout in seconds for the ``awx-manage shell`` execution.
    SHELL_TIMEOUT: int = 60

    def __init__(self, kubectl: Kubectl, config: RegistryRewriteConfig) -> None:
        """Initialise with a Kubectl instance and rewrite configuration.

        Args:
            kubectl: Kubectl wrapper bound to the target namespace.
            config: Source/target registry configuration.
        """
        self._kubectl = kubectl
        self._config = config

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_script(self) -> str:
        """Return the Python script to be run inside the AWX pod.

        Substitutes ``source`` and ``target`` using :func:`repr` so that
        the resulting code contains correctly quoted string literals,
        regardless of special characters in the registry addresses.

        Returns:
            Formatted Python code string ready for ``awx-manage shell -c``.
        """
        return _REWRITE_SCRIPT.format(
            source=repr(self._config.source),
            target=repr(self._config.target),
        )

    def _parse_output(self, raw: str) -> dict[str, Any]:
        """Extract the JSON result object from ``awx-manage shell`` output.

        Django's ``shell --command`` may emit startup diagnostics to stdout
        before the script's own ``print()`` call.  This method scans lines
        from the bottom upward and returns the first line that is valid JSON.

        Args:
            raw: Raw stdout captured from the ``awx-manage shell`` command.

        Returns:
            Parsed JSON dict produced by the rewrite script.

        Raises:
            RegistryRewriteError: If no valid JSON object line is found in
                                   the output.
        """
        return _extract_shell_json(raw)

    def _log_results(self, results: list[dict[str, Any]]) -> int:
        """Log rewrite results and return the number of failures.

        Updated EEs are logged at INFO level with before/after images.
        Unchanged EEs are logged at DEBUG level.
        Failed EEs are logged at ERROR level.

        Args:
            results: List of result dicts from the rewrite script.

        Returns:
            Number of EEs whose ``save()`` call reported an error.
        """
        failed = 0
        updated = 0

        for entry in results:
            name = entry.get("name", "<unknown>")
            if entry.get("updated"):
                updated += 1
                log.info(
                    "Execution Environment '%s':\n"
                    "  image before: %s\n"
                    "  image after : %s",
                    name,
                    entry.get("old_image", ""),
                    entry.get("new_image", ""),
                )
            elif "error" in entry:
                failed += 1
                log.error(
                    "Execution Environment '%s': update FAILED — %s\n"
                    "  image before: %s\n"
                    "  image after : %s",
                    name,
                    entry["error"],
                    entry.get("old_image", ""),
                    entry.get("new_image", ""),
                )
            else:
                log.debug(
                    "Execution Environment '%s': no registry rewrite required"
                    " (image: %s)",
                    name,
                    entry.get("image", ""),
                )

        if not updated and not failed:
            log.info(
                "No registry rewrite required"
                " — no Execution Environment image matched prefix '%s'.",
                self._config.source,
            )
        elif not failed:
            log.info(
                "Registry rewrite complete: %d of %d Execution Environment(s)"
                " updated.",
                updated,
                len(results),
            )

        return failed

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def rewrite_execution_environments(self) -> list[dict[str, Any]]:
        """Rewrite registry prefixes in all AWX Execution Environments.

        Locates the AWX web pod, sends the rewrite Python script via
        ``awx-manage shell -c``, parses the JSON result, and logs the
        outcome for every EE.

        Returns:
            List of result dicts, one per EE.  Each dict contains at least
            ``name`` (str) and ``updated`` (bool).  Rewritten entries also
            carry ``old_image`` and ``new_image``; unchanged entries carry
            ``image``; failed entries carry ``error``.

        Raises:
            RegistryRewriteError: If the AWX web pod cannot be reached, the
                                   ``awx-manage shell`` command fails, the
                                   output cannot be parsed, or any individual
                                   EE update is rejected by the ORM.
        """
        log.info(
            "Registry rewrite: '%s'  →  '%s'",
            self._config.source,
            self._config.target,
        )

        try:
            pod = self._kubectl.web_pod()
        except KubectlError as exc:
            raise RegistryRewriteError(
                f"Cannot locate AWX web pod for registry rewrite: {exc}"
            ) from exc

        script = self._build_script()
        log.debug(
            "Executing awx-manage shell in pod '%s' "
            "(source=%r, target=%r)",
            pod,
            self._config.source,
            self._config.target,
        )

        try:
            raw = self._kubectl.exec(
                pod,
                ["awx-manage", "shell", "-c", script],
                timeout=self.SHELL_TIMEOUT,
            )
        except KubectlError as exc:
            raise RegistryRewriteError(
                f"awx-manage shell failed in pod '{pod}': {exc}"
            ) from exc

        parsed = self._parse_output(raw)

        if "error" in parsed:
            raise RegistryRewriteError(
                "Registry rewrite script reported a fatal error: "
                f"{parsed['error']}"
            )

        results: list[dict[str, Any]] = parsed.get("results", [])

        if not results:
            log.info(
                "No Execution Environments found in the database"
                " — registry rewrite skipped."
            )
            return results

        failed_count = self._log_results(results)

        if failed_count:
            raise RegistryRewriteError(
                f"Registry rewrite failed for {failed_count} Execution"
                f" Environment(s). See log output above for details."
            )

        return results
