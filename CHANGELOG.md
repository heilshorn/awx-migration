# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] - 2026-08-03

### Added

- **Job template credential references.** A job template's attached
  credentials are now exported as AWX-agnostic natural-key references and
  re-attached on import. Credentials are treated as references only — no
  credential *objects* and no secrets are exported (AWX never releases secret
  values), keeping the "Export ≠ Backup" guarantee. A new reference-only
  registry concept (`RelatedRef`, plus non-exportable `credentials` /
  `credential_types` types) models the compound credential natural key
  (`name` + `credential_type{name, kind}` + optional `organization`).
- **Missing-credential pre-flight.** Before import, referenced credentials are
  checked against the target; any that are absent are reported clearly and
  skipped, so the job template still imports without a hard failure.
- **Dependency reporting.** Export and import summaries list the credentials a
  bundle references — i.e. those that must already exist in the target.
- **Job template surveys.** `survey_enabled` and the full `survey_spec` are now
  exported and imported (the survey document is carried verbatim via a new
  `RelatedDoc` registry concept). Password-type survey defaults travel only as
  AWX's `$encrypted$` placeholder, never as real secrets.

### Fixed

- **`awx import` `NoneType` error.** The import payload now always emits the
  `related` block as an object; awxkit iterates `related` during import and
  raised `argument of type 'NoneType' is not iterable` when it was absent.
- **Export-bundle validator false positive.** The validator now reads the
  parallel `natural_keys` array when checking identity, so org-scoped objects
  that carry no top-level `organization` field (e.g. job templates) are no
  longer wrongly flagged as "missing natural-key field(s)".

### Changed

- Tool version bumped to **2.1.0** across the export/import commands.

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

## [2.1.2] - 2026-08-20

- div bugfixing
