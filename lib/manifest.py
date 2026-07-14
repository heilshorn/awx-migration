"""Manifest creation, persistence, and validation for awx-migration backups.

A manifest describes a complete AWX backup without containing any payload
data.  It holds metadata about the tool, the cluster, the database dump,
the secrets, and the checksums of every file in the backup directory.

No Kubernetes calls, no PostgreSQL calls, no filesystem scans — the
Manifest class stores only information passed to it via its setter methods.
"""

from __future__ import annotations

import json
import logging
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log: logging.Logger = logging.getLogger("awx-migration")

MANIFEST_VERSION: int = 1
BACKUP_FORMAT: str = "tar.gz"

# Required top-level sections and their expected Python types
_REQUIRED_SECTIONS: dict[str, type] = {
    "manifest_version": int,
    "tool": dict,
    "backup": dict,
    "awx": dict,
    "postgres": dict,
    "database": dict,
    "secrets": dict,
    "checksums": dict,
}

# Required keys inside each nested section
_SECTION_KEYS: dict[str, list[str]] = {
    "tool": ["name", "version", "python", "platform"],
    "backup": ["created", "hostname", "namespace", "format"],
    "awx": ["version", "operator_version"],
    "postgres": ["version", "database", "database_size"],
    "database": ["filename", "sha256", "size"],
    "secrets": ["count", "names"],
}


class ManifestError(RuntimeError):
    """Raised on any manifest operation failure."""


class Manifest:
    """Creates, persists, and validates AWX backup manifest files.

    The manifest is a plain Python dict internally.  Use the ``set_*``
    methods to populate it, then :meth:`save` to write it to disk and
    :meth:`load` to read it back.

    The class performs no I/O beyond reading and writing the manifest file
    itself — all payload information must be supplied by the caller.
    """

    def __init__(self) -> None:
        """Initialise an empty manifest."""
        self._data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _set_section(self, section: str, values: dict[str, Any]) -> None:
        """Merge *values* into the named top-level *section*.

        Creates the section if it does not yet exist.

        Args:
            section: Top-level key in the manifest dict.
            values: Key-value pairs to store under *section*.
        """
        if section not in self._data:
            self._data[section] = {}
        self._data[section].update(values)

    def _require(self, value: Any, name: str) -> None:
        """Raise :exc:`ManifestError` when *value* is ``None``.

        Args:
            value: Value to check.
            name: Human-readable name used in the error message.

        Raises:
            ManifestError: If *value* is ``None``.
        """
        if value is None:
            raise ManifestError(f"Required value is None: '{name}'")

    # ------------------------------------------------------------------
    # Factory / persistence
    # ------------------------------------------------------------------

    def create(self) -> dict[str, Any]:
        """Initialise the manifest with its version and empty sections.

        Resets any previously stored data and populates
        ``manifest_version`` plus skeleton dicts for every required
        section.

        Returns:
            The freshly created manifest dict.
        """
        log.info("Creating manifest")
        self._data = {
            "manifest_version": MANIFEST_VERSION,
            "tool": {},
            "backup": {},
            "awx": {},
            "postgres": {},
            "database": {},
            "secrets": {},
            "checksums": {},
        }
        return self._data

    def load(self, filename: str | Path) -> dict[str, Any]:
        """Load a manifest from a JSON file.

        Args:
            filename: Path to the manifest JSON file.

        Returns:
            Parsed manifest dict.

        Raises:
            ManifestError: If the file cannot be read or is not valid JSON.
        """
        path = Path(filename)
        log.info("Loading manifest from '%s'", path)
        try:
            with path.open("r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except OSError as exc:
            raise ManifestError(
                f"Cannot read manifest '{path}': {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ManifestError(
                f"Invalid JSON in manifest '{path}': {exc}"
            ) from exc
        return self._data

    def save(self, filename: str | Path) -> None:
        """Serialise the current manifest to a JSON file.

        Args:
            filename: Destination path. Parent directories are created
                      if absent.

        Raises:
            ManifestError: If there is no data to save or the file cannot
                           be written.
        """
        if not self._data:
            raise ManifestError("Cannot save an empty manifest; call create() first")
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("Saving manifest to '%s'", path)
        try:
            with path.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True,
                          ensure_ascii=False)
                fh.write("\n")
        except OSError as exc:
            raise ManifestError(
                f"Cannot write manifest '{path}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, manifest: dict[str, Any] | None = None) -> list[str]:
        """Validate a manifest dict for structural correctness.

        Checks ``manifest_version``, the presence and types of all required
        top-level sections, and the required keys within each section.

        Args:
            manifest: Manifest dict to validate.  Defaults to the
                      internally stored manifest when ``None``.

        Returns:
            List of error strings.  An empty list means the manifest is
            valid.
        """
        data = manifest if manifest is not None else self._data
        log.info("Validating manifest")
        errors: list[str] = []

        if not data:
            return ["Manifest is empty"]

        version = data.get("manifest_version")
        if version is None:
            errors.append("Missing field: 'manifest_version'")
        elif not isinstance(version, int):
            errors.append(
                f"'manifest_version' must be int, got {type(version).__name__}"
            )
        elif version != MANIFEST_VERSION:
            errors.append(
                f"Unsupported manifest_version: {version} "
                f"(expected {MANIFEST_VERSION})"
            )

        for section, expected_type in _REQUIRED_SECTIONS.items():
            if section == "manifest_version":
                continue
            if section not in data:
                errors.append(f"Missing section: '{section}'")
                continue
            value = data[section]
            if not isinstance(value, expected_type):
                errors.append(
                    f"Section '{section}' must be {expected_type.__name__}, "
                    f"got {type(value).__name__}"
                )
                continue
            for key in _SECTION_KEYS.get(section, []):
                if key not in value:
                    errors.append(
                        f"Missing key: '{section}.{key}'"
                    )

        return errors

    # ------------------------------------------------------------------
    # Setter methods
    # ------------------------------------------------------------------

    def set_tool(
        self,
        version: str,
        *,
        python_version: str | None = None,
        os_platform: str | None = None,
    ) -> None:
        """Populate the ``tool`` section with runtime information.

        When *python_version* or *os_platform* are omitted, they are read
        from the current Python interpreter and OS automatically.

        Args:
            version: awx-migration tool version string, e.g. ``"0.1.0"``.
            python_version: Python version string. Auto-detected if None.
            os_platform: OS platform string. Auto-detected if None.
        """
        self._require(version, "version")
        self._set_section("tool", {
            "name": "awx-migration",
            "version": version,
            "python": python_version or sys.version,
            "platform": os_platform or platform.platform(),
        })

    def set_backup(
        self,
        hostname: str,
        namespace: str,
        created: str | None = None,
    ) -> None:
        """Populate the ``backup`` section with cluster and timing metadata.

        Args:
            hostname: Hostname of the machine running the backup.
                      Pass :func:`socket.gethostname` or auto-detect.
            namespace: Kubernetes namespace of the AWX installation.
            created: ISO-8601 timestamp string. Defaults to the current
                     UTC time when ``None``.
        """
        self._require(hostname, "hostname")
        self._require(namespace, "namespace")
        self._set_section("backup", {
            "created": created or datetime.now(tz=timezone.utc).isoformat(),
            "hostname": hostname,
            "namespace": namespace,
            "format": BACKUP_FORMAT,
        })

    def set_awx(self, version: str, operator_version: str) -> None:
        """Populate the ``awx`` section.

        Args:
            version: AWX application version string.
            operator_version: AWX Operator version string.
        """
        self._require(version, "awx.version")
        self._require(operator_version, "awx.operator_version")
        self._set_section("awx", {
            "version": version,
            "operator_version": operator_version,
        })

    def set_postgres(
        self,
        version: str,
        database: str,
        database_size: int,
    ) -> None:
        """Populate the ``postgres`` section.

        Args:
            version: PostgreSQL server version string, e.g. ``"15.12"``.
            database: Database name that was backed up.
            database_size: Size of the database in bytes.
        """
        self._require(version, "postgres.version")
        self._require(database, "postgres.database")
        self._set_section("postgres", {
            "version": version,
            "database": database,
            "database_size": database_size,
        })

    def set_database(
        self,
        filename: str,
        sha256: str,
        size: int,
    ) -> None:
        """Populate the ``database`` section with dump file metadata.

        Args:
            filename: Relative filename of the database dump within the
                      backup directory, e.g. ``"database.dump"``.
            sha256: Lowercase hex SHA-256 digest of the dump file.
            size: Size of the dump file in bytes.
        """
        self._require(filename, "database.filename")
        self._require(sha256, "database.sha256")
        self._set_section("database", {
            "filename": filename,
            "sha256": sha256,
            "size": size,
        })

    def set_secrets(self, names: list[str]) -> None:
        """Populate the ``secrets`` section.

        Args:
            names: Ordered list of exported secret names.
        """
        self._set_section("secrets", {
            "count": len(names),
            "names": sorted(names),
        })

    def set_registry(
        self,
        namespace: str,
        *,
        manifests: list[str],
        images: list[dict[str, str]],
        tool: str,
    ) -> None:
        """Populate the optional ``registry`` section.

        This section is written only when a registry backup was requested.
        It is intentionally **not** part of the required manifest schema, so
        backups created without a registry remain valid and existing restore
        logic is unaffected.

        Args:
            namespace: Kubernetes namespace the registry was backed up from.
            manifests: Backup-relative paths of the exported manifest files.
            images: List of ``{"file": <archive path>, "image": <reference>}``
                    entries describing every saved OCI image archive.
            tool: Name of the image tool used (``"skopeo"`` or ``"crane"``).
        """
        self._require(namespace, "registry.namespace")
        self._set_section("registry", {
            "namespace": namespace,
            "manifests": sorted(manifests),
            "images": images,
            "image_count": len(images),
            "tool": tool,
        })

    # ------------------------------------------------------------------
    # Checksum helpers
    # ------------------------------------------------------------------

    def add_checksum(self, relative_path: str, sha256: str) -> None:
        """Add a single file checksum to the ``checksums`` section.

        Args:
            relative_path: POSIX path of the file relative to the backup
                           directory root.
            sha256: Lowercase hex SHA-256 digest of the file.
        """
        self._require(relative_path, "checksum.relative_path")
        self._require(sha256, "checksum.sha256")
        if "checksums" not in self._data:
            self._data["checksums"] = {}
        self._data["checksums"][relative_path] = sha256

    def add_checksums(self, checksum_dict: dict[str, str]) -> None:
        """Merge a dict of checksums into the ``checksums`` section.

        Existing entries with the same key are overwritten.

        Args:
            checksum_dict: Mapping of relative POSIX path to hex SHA-256
                           digest (e.g. as returned by
                           :meth:`~lib.archive.Archive.checksums`).
        """
        if "checksums" not in self._data:
            self._data["checksums"] = {}
        self._data["checksums"].update(checksum_dict)

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        """Return the current manifest dict.

        Returns:
            The internal manifest dict (not a copy).
        """
        return self._data

    def get(self, section: str, key: str | None = None) -> Any:
        """Return a section or a specific key within a section.

        Args:
            section: Top-level section name.
            key: Optional key within the section.

        Returns:
            The section dict when *key* is ``None``, the specific value
            otherwise.  Returns ``None`` when the section or key is absent.
        """
        section_data = self._data.get(section)
        if key is None or section_data is None:
            return section_data
        return section_data.get(key)
