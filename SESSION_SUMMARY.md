# Session Summary – AWX Migration → Management Tool (Version 2.0)

Dieses Dokument fasst den Arbeitsstand der Export-/Import-Entwicklung zusammen.
Es ergänzt `NEXT_STEPS.md` (Kurzstatus) um Kontext und Begründungen.

## Ausgangslage

Das Projekt war ein Backup-/Restore-Werkzeug für AWX auf Kubernetes
(`awx_backup.py`, `awx_restore.py`, `lib/`). Ziel dieser Sitzungsreihe: das Tool
additiv um **Export und Import einzelner AWX-Objekte** erweitern
(Job Templates, Workflow Job Templates, Inventories, Inventory Sources,
Projects, Credentials, Execution Environments, Organizations, Teams,
Notification Templates), ohne Backup/Restore anzufassen.

## Architekturentscheidungen

- **Strikte Schichtung** – CLI → Exporter/Importer → AwxClient → AwxCliClient.
  Nur `AwxCliClient` kennt natives AWX-JSON; alle oberen Schichten arbeiten
  ausschließlich mit `CanonicalObject`. Durchgesetzt über eine Import-Matrix.
- **Austauschbare Client-Fassade** – `AwxClient` (ABC) mit `AwxCliClient`
  (Phase 1, `awx`-CLI/awxkit). Ein späterer `AwxRestClient` ist ohne Änderung
  an Exporter/Importer möglich. Grund: awxkit-Versionen verhalten sich
  inkonsistent; das Tool muss davon unabhängig bleiben.
- **Kanonisches Modell statt Typ-Klassen** – ein generisches
  `CanonicalObject(type, fields)` statt `JobTemplate`/`Project`/… . Hält die
  Registry als einzigen Erweiterungspunkt.
- **Registry als einziger Erweiterungspunkt** – ein neuer Objekttyp = **ein**
  `ObjectType`-Eintrag in `lib/awx_objects.py` (Whitelist, natural_key,
  relations, depends_on + optionale Hooks). Exporter/Importer bleiben
  unverändert.
- **Whitelist statt Blacklist** – pro Typ eine explizite Feld-Whitelist;
  interne AWX-Felder (`id`, `url`, `summary_fields`, Timestamps …) können nie
  durchsickern.
- **Referenzen ausschließlich über natürliche Schlüssel** – z. B.
  `organization = "Default"`, niemals `organization_id`. Portabel und
  git-lesbar.
- **Eigenes, stabiles Exportformat** – versioniertes Envelope (`format_version`
  + `schema_version`, `kind`, Metadaten, `objects`) plus `manifest.json`;
  vollständig getrennt vom Backup-Manifest. Eine `lib/migrations/`-Schicht
  (nummerierte Schritte + Runner) erlaubt spätere Formatmigrationen.
- **Zwei Betriebsarten klar getrennt** – Backup/Restore = enthält Secrets,
  Disaster Recovery. Export/Import = **keine** Secrets, Migration/Versionierung
  (`contains_secrets: false` im Manifest; Import warnt aktiv).
- **Schlanke gemeinsame CLI-Schicht** – `lib/cli_common.py` bündelt
  `build_connection`, `default_output_directory`, `list_organizations`,
  `print_export_summary`, `print_import_summary`, `COMMON_CLI_ERRORS`. Keine
  Fachlogik, kein `sys.exit`.
- **Nicht-invasive Integration** – kein einziger Eingriff in Backup/Restore;
  `lib/config.py` blieb unverändert (AWX-Defaults liegen lokal in
  `lib/awx_connection.py`).

## Neu hinzugekommene Module

Fundament:
- `lib/canonical.py` – `CanonicalObject`
- `lib/awx_objects.py` – `ObjectType`, `Relation`, `OBJECT_TYPES`, `import_order`
- `lib/export_format.py` – stabiles Datei-/Manifestformat
- `lib/migrations/` – `__init__.py` (Runner + `MigrationError`), `migration_001.py`

AWX-Kommunikation:
- `lib/awx_connection.py` – `AwxConnection`, `resolve_connection` (NodePort-Host,
  Credential-Priorität Token → User/Pass → Secret)
- `lib/awx_cli.py` – `AwxCli` (Binary-Wrapper, keine AWX-Semantik)
- `lib/awx_client.py` – `AwxClient` (ABC) + `AwxCliClient` (einzige Schicht mit
  AWX-JSON: `export`, `import_objects`, `exists`, `list_organizations`),
  `ImportResult`, `make_client`

Fachlogik:
- `lib/exporter.py` – `Exporter`, `ExportSummary`, `ExportError`
- `lib/importer.py` – `Importer`, `ImportSummary`, `ImportError`
- `lib/export_validator.py` – `ExportValidator`, `ValidationResult`,
  `ExportValidationError`

CLI:
- `lib/cli_common.py` – gemeinsame CLI-Helfer
- `awx_export.py` – Export-CLI
- `awx_import.py` – Import-CLI (nutzt `cli_common` produktiv)

Doku/Tests:
- `docs/export-import-design.md` – Architekturdokument
- `tests/` – Unit-Tests (conftest + `tests/unit/…`)

## Teststand

- **186 Unit-Tests erfolgreich** (`pytest`; von ursprünglich 173, +13 durch die
  Adapter-/Format-Änderungen der E2E-Phase), `ruff` sauber (neue Module),
  `py_compile` fehlerfrei. Voller Lauf: `186 passed, 2 skipped` (die 2 Skips
  sind die opt-in E2E-Tests).
- Alle Unit-Tests laufen **ohne echtes AWX und ohne Cluster** – die
  Client-Fassade wird per Fake/Mock ersetzt; das ist die zentrale Test-Naht.

## E2E-Test-Harness (neu, opt-in)

- **E2E-Harness implementiert** – `tests/e2e/` plus `pytest.ini` (registriert
  den `e2e`-Marker). Additiv; keine Bestandsdatei verändert.
- **Opt-in über `pytest -m e2e`** bzw. `pytest tests/e2e`. Automatischer Skip,
  solange `AWX_E2E_HOST` oder das `awx`-Binary fehlt (auch fehlende Credentials
  → Skip). CI ohne AWX bleibt damit grün.
- **Verbindung aus Env** (kein Cluster/kubectl nötig): `AWX_E2E_HOST` +
  `AWX_E2E_TOKEN` **oder** `AWX_E2E_USERNAME`/`AWX_E2E_PASSWORD`, optional
  `AWX_E2E_INSECURE`; für den Template-Test zusätzlich
  `AWX_E2E_PROJECT_SCM_URL` / `AWX_E2E_PLAYBOOK`.
- **Provisionierung und Cleanup der Testdaten implementiert** – via `awx`-CLI
  (erlaubtes Scaffolding). Objekte tragen das Prefix `awxmig-e2e-`, die Org ist
  pro Lauf eindeutig (`uuid`) → parallele Läufe kollidieren nicht. Cleanup ist
  **Best-Effort** und lässt einen Testlauf nie zusätzlich fehlschlagen; Teardown
  in umgekehrter Abhängigkeitsreihenfolge.
- **Roundtrip-Tests für Inventory und Job Template** vorhanden
  (`test_export_import_inventory.py`, `test_export_import_template.py`).
- **Der Validator ist Bestandteil der E2E-Pipeline**:
  Export → Validate → Import → Export → Validate → Roundtrip-Diff. Die
  „Import→Validate"-Grenze deckt `Importer.import_path` durch seine interne
  Validierung ab (keine redundante Re-Validierung des unveränderten Bundles).
  Die eigentlichen Schritte laufen ausschließlich über die Bibliothek
  (`Exporter`, `Importer`, `ExportValidator`, `AwxClient`).
- **E2E gegen reale AWX 24.6.1 erfolgreich** – beide Roundtrips grün
  (`tests/e2e`: `2 passed`), Inventory inkl. Org-Zuordnung und Job Template inkl.
  Referenzen. Verbindung per Token/User-Pass gegen einen NodePort-Host.

### Beim E2E-Lauf gefundene und behobene awxkit-Befunde

Die E2E-Tests haben mehrere echte awxkit-Eigenheiten aufgedeckt, die auch den
produktiven Betrieb betroffen hätten (nicht nur die Tests):

- **ANSI-Farbcodes in der CLI-Ausgabe** – `awx … -f json` umhüllt die Ausgabe je
  nach Build mit ANSI-Codes, was `json.loads` bricht. Im Binary-Wrapper
  `lib/awx_cli.py` werden ANSI-Sequenzen aus stdout **und** stderr entfernt.
- **Export-Flag `--inventory`** – awxkit kennt die Ressource nur im Singular; der
  Plural `--inventories` wird ignoriert und exportiert die ganze DB. Registry
  korrigiert (`cli_flag="--inventory"`, `awx_key="inventory"`).
- **Referenz-Adapter statt awxkit-Format intern** – Referenzen sind kanonisch
  AWX-agnostisch (Name bzw. `{name, organization}`); der Adapter im
  `AwxCliClient` baut daraus beim Import die awxkit-natural-keys (mit `type` und
  verschachtelter Org) und reduziert sie beim Export wieder.
- **`natural_key` als Identitätsmetadatum** – von `fields` getrennt am
  `CanonicalObject`, im Format als paralleles `natural_keys`-Array persistiert;
  Quelle für `identity`/`exists`/Org-Filter (löst JT ohne Body-`organization`).
- **Asset-`natural_key` beim Import** – `awx import` verlangt je Asset ein
  `natural_key`; der Adapter erzeugt es.
- **Betrieb (kein Code): Proxy im Execution Environment** – Projekt-Updates
  klonen aus dem EE-Container; hinter einem Proxy muss AWX den Proxy im
  Job-Environment kennen (`AWX_TASK_ENV` bzw. Settings → Job Settings →
  „Extra Environment Variables"). Als Voraussetzung im E2E-conftest dokumentiert.

## Wichtiger Hinweis: nichts committet

Der gesamte v2.0-Stand liegt derzeit **uncommitted** im Arbeitsbaum (alle neuen
Dateien sind in `git status` als untracked sichtbar). Es wurde bewusst noch kein
Commit erstellt. Backup/Restore und alle Bestandsdateien sind unverändert
(`git diff HEAD` darauf ist leer).

## Version 2.0 – funktional abgeschlossen und gegen reale AWX validiert

Die vollständige Export-/Import-Pipeline (Fundament → Adapter →
Exporter/Importer → Validator → CLI) ist funktional fertig, unit- **und**
E2E-getestet (reale AWX 24.6.1, beide Roundtrips grün). Ausstehend sind nur noch
**Dokumentation und Release**.

## Nächster Schritt: Restarbeiten bis Release

Die Refactoring-Sperre ist mit dem grünen E2E-Lauf aufgehoben. Reihenfolge:
`awx_export.py` auf `cli_common` umstellen (reines Refactoring) → README ergänzen
→ Changelog → Release 2.0. Details siehe `NEXT_STEPS.md`.
