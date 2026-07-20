# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2026-07-20

### Added

- **New feature: AWX Object Export / Import.** New `awx_export.py` and
  `awx_import.py` commands export and import individual AWX objects as
  versioned JSON bundles — separate from, and additive to, the existing full
  backup/restore.
- **Export/import via natural keys (ID-independent).** Objects and their
  references are expressed through AWX natural keys (names), never internal
  database IDs, so bundles are portable across instances and readable in git.
- **Support for the currently implemented AWX object types** — Organizations,
  Inventories, Projects and Job Templates. Further types are added through a
  single registry entry, without touching the exporter or importer.
- **Validated against AWX 24.6.1** — export → import → export round-trips
  cleanly (whitelist, reference resolution and normalization all round-trip).
- **Opt-in end-to-end tests against a real AWX instance** (`tests/e2e/`),
  skipped automatically unless `AWX_E2E_HOST` and the `awx` CLI are available;
  they self-provision and clean up their test data.
- Versioned export format (`format_version` / `schema_version`) with a
  migration layer, and a structural export-bundle validator.

### Changed

- **Refactored the CLI implementation to share common infrastructure between
  `awx_export.py` and `awx_import.py`** (`lib/cli_common`: connection building,
  organization listing, result summaries, common error handling).
- Tool version unified to **2.0.0** across the export/import commands.

### Notes

- Backup/restore functionality and its on-disk formats are **unchanged**; all
  export/import work is additive.
- Export bundles **never contain secrets** and are not a substitute for a full
  backup.
