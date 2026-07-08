"""Kubernetes Secrets management for awx-migration.

All Secret operations are routed through this module.
Backup and restore scripts must never call kubectl for Secret export or
import directly — every access goes through this module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .kubectl import Kubectl, KubectlError

try:
    from .config import SECRETS
except ImportError:
    SECRETS: list[str] = []

log: logging.Logger = logging.getLogger("awx-migration")

# Metadata fields that must never appear in exported secrets
_METADATA_STRIP: frozenset[str] = frozenset({
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "managedFields",
    "ownerReferences",
    "selfLink",
})

_SECRET_KEY_NAME: str = "awx-secret-key"
_SECRET_KEY_FIELD: str = "secret_key"

# Annotation removed from metadata before export (kubectl internal bookkeeping)
_ANNOTATION_STRIP: str = "kubectl.kubernetes.io/last-applied-configuration"

# Import order for AWX secrets: wrong key-order causes the operator to start
# with an invalid encryption key, breaking the entire installation.
_IMPORT_PRIORITY: dict[str, int] = {
    "awx-secret-key": 0,
    "awx-postgres-configuration": 1,
}


class SecretError(RuntimeError):
    """Raised on any Kubernetes Secret operation failure."""


class Secrets:
    """Manages Kubernetes Secrets for AWX backup and restore.

    All kubectl interactions are delegated to the injected
    :class:`~lib.kubectl.Kubectl` instance.  The set of managed secrets
    is driven by :data:`~lib.config.SECRETS`.

    Exported secrets have volatile metadata stripped so they can be safely
    applied to a different cluster without uid/resourceVersion conflicts.
    """

    def __init__(self, kubectl: Kubectl) -> None:
        """Initialise with a configured Kubectl instance.

        Args:
            kubectl: Kubectl wrapper bound to the target namespace.
        """
        self._kubectl = kubectl

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_raw(self, name: str) -> dict[str, Any]:
        """Fetch *name* from Kubernetes and return the raw resource dict.

        Args:
            name: Secret name.

        Returns:
            Full Kubernetes Secret resource as a dict (data base64-encoded).

        Raises:
            SecretError: On kubectl failure.
        """
        try:
            return self._kubectl.json(["get", "secret", name, "-o", "json"])
        except KubectlError as exc:
            raise SecretError(
                f"Failed to fetch secret '{name}': {exc}"
            ) from exc

    def _clean(self, resource: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *resource* with volatile metadata removed.

        Strips every field in :data:`_METADATA_STRIP` from ``metadata``,
        removes :data:`_ANNOTATION_STRIP` from ``metadata.annotations``
        (all other annotations are preserved), and drops the top-level
        ``status`` key so the result is safe to apply on any cluster.

        Args:
            resource: Raw Kubernetes Secret resource dict.

        Returns:
            Cleaned copy ready for export or re-apply.
        """
        cleaned = dict(resource)
        meta = {
            k: v for k, v in cleaned.get("metadata", {}).items()
            if k not in _METADATA_STRIP
        }
        annotations = dict(meta.get("annotations", {}))
        annotations.pop(_ANNOTATION_STRIP, None)
        if annotations:
            meta["annotations"] = annotations
        else:
            meta.pop("annotations", None)
        cleaned["metadata"] = meta
        cleaned.pop("status", None)
        return cleaned

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        """Write *data* to *path* as indented UTF-8 JSON.

        Args:
            path: Destination file path. Parent directories are created.
            data: JSON-serialisable dict.

        Raises:
            SecretError: If the file cannot be written.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
        except OSError as exc:
            raise SecretError(
                f"Cannot write secret file '{path}': {exc}"
            ) from exc

    def _read_json(self, path: Path) -> dict[str, Any]:
        """Read and parse a JSON secret file from *path*.

        Args:
            path: Path to the JSON file.

        Returns:
            Parsed secret dict.

        Raises:
            SecretError: If the file cannot be read or contains invalid JSON.
        """
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except OSError as exc:
            raise SecretError(
                f"Cannot read secret file '{path}': {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SecretError(
                f"Invalid JSON in '{path}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list(self) -> list[str]:
        """Return the names of all Secrets present in the namespace.

        Returns:
            Sorted list of secret names.

        Raises:
            SecretError: On kubectl failure.
        """
        try:
            data = self._kubectl.json(["get", "secrets", "-o", "json"])
        except KubectlError as exc:
            raise SecretError(f"Failed to list secrets: {exc}") from exc
        return sorted(
            item["metadata"]["name"]
            for item in data.get("items", [])
        )

    def exists(self, name: str) -> bool:
        """Return True if secret *name* exists in the namespace.

        Args:
            name: Secret name.

        Returns:
            True when the secret is present, False otherwise.
        """
        try:
            self._kubectl.run(
                ["get", "secret", name],
                timeout=10,
                retries=1,
            )
            return True
        except KubectlError:
            return False

    def get(self, name: str) -> dict[str, Any]:
        """Return the cleaned Secret resource dict for *name*.

        Volatile metadata (uid, resourceVersion, …) is stripped before
        returning so the result is ready for export or direct re-apply.

        Args:
            name: Secret name.

        Returns:
            Cleaned Kubernetes Secret resource dict.

        Raises:
            SecretError: On kubectl failure.
        """
        return self._clean(self._get_raw(name))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_secret(self, name: str, outfile: str | Path) -> None:
        """Export secret *name* to *outfile* as cleaned JSON.

        Volatile metadata fields are stripped so the file is safe for
        import into a different cluster.

        Args:
            name: Secret name.
            outfile: Destination path for the JSON file.

        Raises:
            SecretError: On kubectl or I/O failure.
        """
        log.info("Exporting Secret '%s' to '%s'...", name, outfile)
        cleaned = self.get(name)
        self._write_json(Path(outfile), cleaned)
        log.debug("Secret '%s' written (%d keys)", name, len(cleaned.get("data") or {}))

    def export_all(self, directory: str | Path) -> list[str]:
        """Export every secret listed in config.SECRETS to *directory*.

        Each secret is written as ``<directory>/<name>.json``.

        Args:
            directory: Destination directory. Created if absent.

        Returns:
            Names of successfully exported secrets.

        Raises:
            SecretError: If any individual export fails.
        """
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        exported: list[str] = []
        for name in SECRETS:
            self.export_secret(name, dest / f"{name}.json")
            exported.append(name)
        log.info("Exported %d secret(s) to '%s'", len(exported), dest)
        return sorted(exported)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_secret(self, filename: str | Path) -> None:
        """Read a JSON secret file and apply it to the cluster via kubectl.

        Args:
            filename: Path to the JSON file produced by :meth:`export_secret`.

        Raises:
            SecretError: On I/O or kubectl failure.
        """
        path = Path(filename)
        resource = self._read_json(path)
        name = resource.get("metadata", {}).get("name", path.stem)
        log.info("Importing Secret '%s' from '%s'...", name, path)
        manifest = json.dumps(resource, indent=2, ensure_ascii=False)
        try:
            self._kubectl.apply(manifest)
        except KubectlError as exc:
            raise SecretError(
                f"kubectl apply failed for secret '{name}': {exc}"
            ) from exc
        log.debug("kubectl apply completed for secret '%s'", name)

    def import_all(self, directory: str | Path) -> list[str]:
        """Import all ``*.json`` files from *directory* via kubectl apply.

        Secrets are applied in a fixed AWX-safe order:

        1. ``awx-secret-key`` — must exist before the operator starts.
        2. ``awx-postgres-configuration`` — required for database connectivity.
        3. All remaining secrets in alphabetical order.

        Args:
            directory: Source directory containing exported secret JSON files.

        Returns:
            Names (derived from filenames) of successfully imported secrets,
            in the actual import order.

        Raises:
            SecretError: If any individual import fails.
        """
        src = Path(directory)
        json_files = sorted(
            src.glob("*.json"),
            key=lambda p: (_IMPORT_PRIORITY.get(p.stem, 2), p.stem),
        )
        if not json_files:
            log.warning("No JSON secret files found in '%s'", src)
            return []
        imported: list[str] = []
        for path in json_files:
            self.import_secret(path)
            imported.append(path.stem)
        log.info("Imported %d secret(s) from '%s'", len(imported), src)
        return imported

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, name: str) -> None:
        """Delete secret *name* from the namespace.

        A missing secret is silently ignored.

        Args:
            name: Secret name.

        Raises:
            SecretError: On kubectl failure.
        """
        log.info("Deleting Secret '%s'...", name)
        try:
            self._kubectl.delete("secret", name, ignore_not_found=True)
        except KubectlError as exc:
            raise SecretError(
                f"Failed to delete secret '{name}': {exc}"
            ) from exc

    def delete_all(self) -> list[str]:
        """Delete every secret listed in config.SECRETS.

        Only the secrets defined in :data:`~lib.config.SECRETS` are removed —
        no other secrets in the namespace are touched.

        Returns:
            Names of deleted secrets.

        Raises:
            SecretError: If any individual deletion fails.
        """
        deleted: list[str] = []
        for name in SECRETS:
            self.delete(name)
            deleted.append(name)
        log.info("Deleted %d configured secret(s)", len(deleted))
        return deleted

    # ------------------------------------------------------------------
    # AWX secret key
    # ------------------------------------------------------------------

    def secret_key(self) -> str:
        """Return the decoded AWX secret key string.

        Fetches ``awx-secret-key`` from Kubernetes and returns the
        decoded value of the ``secret_key`` data field.

        Returns:
            Decoded secret key string.

        Raises:
            SecretError: If the secret or field is absent, or kubectl fails.
        """
        try:
            data = self._kubectl.get_secret(_SECRET_KEY_NAME)
        except KubectlError as exc:
            raise SecretError(
                f"Failed to fetch '{_SECRET_KEY_NAME}': {exc}"
            ) from exc
        if _SECRET_KEY_FIELD not in data:
            raise SecretError(
                f"Field '{_SECRET_KEY_FIELD}' not found in secret "
                f"'{_SECRET_KEY_NAME}'. Available keys: "
                f"{sorted(data.keys())}"
            )
        return data[_SECRET_KEY_FIELD]

    def compare_secret_key(self, other_key: str) -> bool:
        """Compare the current AWX secret key with *other_key*.

        Args:
            other_key: Decoded secret key string to compare against.

        Returns:
            True if both keys are identical, False otherwise.

        Raises:
            SecretError: If the current secret key cannot be retrieved.
        """
        log.info("Comparing %s...", _SECRET_KEY_NAME)
        current = self.secret_key()
        match = current == other_key
        log.debug(
            "Secret key comparison: %s", "match" if match else "mismatch"
        )
        return match

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Check that all configured secrets are present and well-formed.

        Verifies each secret in :data:`~lib.config.SECRETS` for:

        - Existence in the namespace.
        - Non-empty ``data`` field.
        - Presence of ``metadata.name``.
        - Presence of ``metadata.namespace``.
        - Presence of a valid ``type`` field.

        Returns:
            List of error strings.  An empty list means all checks passed.
        """
        errors: list[str] = []
        for name in SECRETS:
            if not self.exists(name):
                errors.append(f"Secret '{name}' not found in namespace")
                continue
            try:
                resource = self._get_raw(name)
            except SecretError as exc:
                errors.append(f"Secret '{name}' could not be fetched: {exc}")
                continue
            if not resource.get("data"):
                errors.append(f"Secret '{name}' contains no data")
            meta = resource.get("metadata", {})
            if not meta.get("name"):
                errors.append(f"Secret '{name}' is missing metadata.name")
            if not meta.get("namespace"):
                errors.append(f"Secret '{name}' is missing metadata.namespace")
            if not resource.get("type"):
                errors.append(f"Secret '{name}' is missing a valid type")
        return errors
