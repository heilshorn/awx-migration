# Architektur: AWX Object Export / Import

**Status:** Phase 1 — Architektur freigegeben, Implementierung noch nicht begonnen.
**Datum:** 2026-07-16
**Betrifft:** neue Kommandos `awx_export.py` / `awx_import.py` (einzelne AWX-Objekte).

Dieses Dokument hält die Designentscheidungen fest, damit sie bei der späteren
Implementierung nicht verloren gehen. Es beschreibt **ausschließlich** die
Architektur, keinen Code.

---

## Zweck & Abgrenzung

Das Werkzeug soll einzelne AWX-Objekte exportieren und importieren können —
zusätzlich zum bestehenden Voll-Backup/Restore. Erste Ausbaustufe:

Job Templates · Workflow Job Templates · Inventories · Inventory Sources ·
Projects · Credentials · Execution Environments · Organizations · Teams ·
Notification Templates.

### Zwei klar getrennte Betriebsarten

|                | **Backup / Restore**                       | **Export / Import**                     |
| -------------- | ------------------------------------------ | --------------------------------------- |
| Zweck          | Disaster Recovery, Vollwiederherstellung   | Migration, Versionsverwaltung, Git      |
| Secrets        | **enthalten** (DB-Dump + K8s-Secrets)      | **nicht** enthalten                     |
| Umfang         | gesamte Installation                       | einzelne Objekte / Objekttypen          |
| Format         | `tar.gz` + `manifest.json` (Manifest v1)   | JSON-Dateien + eigenes Format (v1)      |

Das Export-Manifest trägt `contains_secrets: false`; der Import **warnt aktiv**,
dass Credential-Geheimnisse nachgetragen werden müssen.

---

## Harte Leitplanken (nicht verhandelbar)

- **Keine funktionale Änderung an Backup/Restore** und deren Dateiformaten
  (`tar.gz`-Archiv + `manifest.json` bleiben kompatibel).
- **Alles additiv.** Bestehende Module werden nur *genutzt*, nicht verändert.
  `lib/config.py` erhält ausschließlich neue Konstanten.
- **Ein neuer Objekttyp = ein neuer Registry-Eintrag** — ohne Änderung an
  `Exporter` oder `Importer`.
- **Export/Import unabhängig von Backup/Restore testbar** (Naht = `AwxClient`).

---

## 1. Komponentenübersicht

```
┌───────────────────────────────────────────────────────────────┐
│  awx_export.py / awx_import.py           (Orchestrierung, dünn) │
│  argparse · setup_logger · Fehler-Handling · Ablauf             │
│  löst die Objekttyp-Auswahl gegen die Registry auf              │
└───────────────┬───────────────────────────────────────────────┘
                ▼  übergibt list[ObjectType]
┌───────────────────────────────────────────────────────────────┐
│  Exporter / Importer                     (kennt NUR kanonisch)  │
│  arbeitet über die übergebene ObjectType-Liste                  │
│  kennt die globale Registry NICHT                               │
└───────────────┬───────────────────────────────────────────────┘
                ▼
┌───────────────────────────────────────────────────────────────┐
│  export_format.py + lib/migrations/     (unser stabiles Format) │
│  Envelope schreiben/lesen · format_version + schema_version     │
│  Auto-Migration alter Dateien beim Lesen                        │
└───────────────┬───────────────────────────────────────────────┘
                ▼
┌───────────────────────────────────────────────────────────────┐
│  AwxClient (ABC, Fassade)      (EINZIGE Schicht mit AWX-Wissen) │
│  AWX ⇄ CanonicalObject · Whitelist anwenden · Org-Filter        │
│    ├── AwxCliClient   → lib/awx_cli.py  (awx-Binary, Phase 1)    │
│    └── AwxRestClient  → REST-API        (Phase 2, serverseitig)  │
└───────────────────────────────────────────────────────────────┘

Quer genutzt: lib/awx_objects.py (Registry) · lib/awx_connection.py ·
lib/config.py · lib/kubectl.py · lib/logger.py · lib/utils.py
```

---

## 2. Datenfluss

### Export

```
awx_export.py
  ├─ Auswahl (--all / --type ...) gegen OBJECT_TYPES auflösen → list[ObjectType]
  ├─ resolve_connection(kubectl, args) ─────────────► AwxConnection
  ├─ make_client(connection)           ─────────────► AwxCliClient (via ABC)
  └─ Exporter(client, object_types)
        für jedes object_type in der Liste:
          objects = object_type.exporter(client, object_type, org)   # falls Hook
                    else client.export(object_type.key, organization=org)
          │        └─ AwxCliClient: `awx export` → AWX-Dict
          │           → Whitelist(object_type.fields) anwenden
          │           → Referenzen auf natürliche Schlüssel mappen
          │           → lokal nach organization filtern (CLI kann es nicht)
          │           → CanonicalObject          ◄── AWX-Wissen endet HIER
          objects = object_type.post_export(objects, ctx)  # falls Hook
          (object_type.validator je Objekt, falls Hook)
          export_format.write_type_file(dir/<key>.json, object_type.key, objects, meta)
        export_format.write_manifest(dir/manifest.json, meta, per_type_counts)
```

### Import

```
awx_import.py
  ├─ vorhandene Dateien → Objekttypen bestimmen → in Import-Reihenfolge
  │  ordnen (import_order(selected)) → list[ObjectType]
  ├─ resolve_connection / make_client
  └─ Importer(client, object_types)
        für jedes object_type (in Reihenfolge):
          doc = export_format.read_type_file(path)     # ← Auto-Migration alt→CURRENT
          objects = doc.objects   (list[CanonicalObject], nach --name gefiltert)
          (object_type.validator je Objekt, falls Hook → warnings)
          result = object_type.importer(client, object_type, objects, on_conflict)
                   else client.import_objects(object_type.key, objects, on_conflict=...)
                   └─ AwxCliClient: CanonicalObject → AWX-Bundle → `awx import`
                      (skip/fail: vorher client.exists(...) prüfen)
        Post-Import-Phase (nach ALLEN Typen):
          für jedes object_type mit post_import-Hook:
            object_type.post_import(client, ctx)   # z.B. Workflow-Knoten verknüpfen
        Zusammenfassung aus ImportResult (created/updated/skipped/warnings/errors)
```

Der **Übersetzungspfeil AWX ⇄ Canonical liegt vollständig im `AwxClient`**.
`Exporter`, `Importer` und `export_format` berühren nie ein AWX-Dictionary.

---

## 3. Klassenübersicht

### Registry — `lib/awx_objects.py`

```
@dataclass(frozen=True)
class Relation:
    field: str            # kanonisches Feld, z.B. "inventory"
    target_type: str      # z.B. "inventories"
    many: bool = False

@dataclass(frozen=True)
class ObjectType:
    key: str                       # z.B. "job_templates"
    cli_flag: str                  # z.B. "--job_templates"
    natural_key: tuple[str, ...]   # z.B. ("name", "organization")
    org_scoped: bool
    fields: tuple[str, ...]        # WHITELIST der erlaubten kanonischen Felder
    relations: tuple[Relation, ...] = ()
    depends_on: tuple[str, ...] = ()
    # --- optionale Hooks für Sonderfälle (alle default None) ---
    validator: Callable | None = None      # (CanonicalObject) -> list[str]
    exporter:  Callable | None = None       # (client, object_type, org) -> list[CanonicalObject]
    importer:  Callable | None = None       # (client, object_type, objects, on_conflict) -> ImportResult
    post_export: Callable | None = None     # (objects, ctx) -> list[CanonicalObject]
    post_import: Callable | None = None     # (client, ctx) -> None   (Cross-Type-Fixups)

OBJECT_TYPES: dict[str, ObjectType]
def import_order(selected: list[ObjectType]) -> list[ObjectType]   # topolog. aus depends_on
```

Die Hooks sind der Erweiterungspunkt für Sonderlogik **ohne** Änderung an
`Exporter`/`Importer`. Beispiel: `workflow_job_templates` bekommt später
`post_import=fix_workflow_nodes`, statt ein `if type == "workflow"` in den
Importer zu schreiben. `Exporter`/`Importer` rufen generisch „Hook oder
Default" auf — es gibt keine typ-spezifische Verzweigung im Code.

### Kanonisches Modell — `lib/canonical.py`

```
@dataclass(frozen=True)
class CanonicalObject:
    type: str                 # Registry-Key
    fields: dict[str, Any]    # NUR Whitelist-Felder, Referenzen als natürliche Schlüssel
    def identity(self, natural_key) -> tuple: ...
```

Bewusst **ein** generisches Modell statt Klassen wie `JobTemplate`,
`Inventory`, `Project`: Per-Typ-Klassen würden die „nur ein Registry-Eintrag
pro Typ"-Regel brechen und das Whitelist-Schema doppeln.

### Fassade — `lib/awx_client.py`

```
@dataclass
class ImportResult:
    created:  list[str] = []
    updated:  list[str] = []
    skipped:  list[str] = []
    warnings: list[str] = []
    errors:   list[str] = []

class AwxClient(ABC):                        # der einzige Vertrag der oberen Schichten
    def list_organizations(self) -> list[str]: ...
    def export(self, object_type: str, *, organization: str | None = None) -> list[CanonicalObject]: ...
    def import_objects(self, object_type: str, objects: list[CanonicalObject], *, on_conflict: str) -> ImportResult: ...
    def exists(self, object_type: str, identity: tuple) -> bool: ...

class AwxCliClient(AwxClient):  # Phase 1: nutzt AwxCli + Registry; enthält AWX-Übersetzung + lokalen Org-Filter
class AwxRestClient(AwxClient): # Phase 2: serverseitiger Org-Filter
def make_client(connection, kind="cli") -> AwxClient
```

Der Contract ist **generisch** (`export(object_type, …)`), nicht per-Typ. Der
Org-Filter ist Parameter des Contracts (Vorgabe „Filter im Client"), aber ein
neuer Typ braucht trotzdem keine neue Methode (Vorgabe „nur Registry-Eintrag").
Typisierte Aliase wie `export_job_templates(...)` sind optionale Bequemlichkeit,
nicht der Erweiterungsmechanismus.

### Weitere neue Module

| Modul | Inhalt |
| --- | --- |
| `lib/awx_cli.py` | `AwxCli` + `AwxCliError` — reiner `awx`-Binary-Wrapper (`detect()`, `run()`), analog `Kubectl` |
| `lib/awx_connection.py` | `AwxConnection` (dataclass) + `resolve_connection(kubectl, args)` |
| `lib/export_format.py` | `FORMAT_VERSION` · `SCHEMA_VERSION` · `write/read_type_file` · `write/read_manifest` · `ExportFormatError` |
| `lib/migrations/` | Auto-Migration alter Exportdateien (siehe unten) |
| `lib/exporter.py` | `Exporter` + `ExportError` |
| `lib/importer.py` | `Importer` + `ImportError` |

---

## 4. Verantwortlichkeiten & erlaubte Abhängigkeiten

Die strikte Schichtung wird per **Import-Matrix** festgeschrieben und durch
einen Unit-Test (`test_layering`) abgesichert:

| Modul | darf importieren | darf **nicht** |
| --- | --- | --- |
| `awx_export/import.py` | `Exporter/Importer`, `make_client`, `awx_connection`, `awx_objects` (Registry), `kubectl`, `config`, `logger`, `utils` | `awx_cli`, rohe AWX-Daten |
| `exporter.py` / `importer.py` | `export_format`, `canonical`, `AwxClient` (ABC), `ObjectType` (nur der Typ) | `OBJECT_TYPES` (globale Registry), `awx_client`-Konkretion, `awx_cli`, AWX-Dicts |
| `export_format.py` | `migrations`, `canonical`, `utils` | `awx_client`, `awx_cli` |
| `migrations/*` | nur stdlib — reine Dict-Transforms | alles Projektspezifische |
| `awx_client.py` (ABC + `AwxCliClient`) | `awx_cli`, `awx_objects`, `canonical` | `exporter`, `importer`, `export_format` |
| `awx_cli.py` | nur `subprocess`/stdlib | AWX-Semantik |

Kernaussagen:

- **Nur `AwxClient` kennt das AWX-Datenmodell.**
- **`Exporter`/`Importer` kennen die globale Registry nicht** — sie erhalten
  eine `list[ObjectType]` vom Orchestrator und arbeiten nur darüber. Damit
  könnte ein Aufrufer gezielt `Exporter(client, [OBJECT_TYPES["projects"]])`
  bauen.
- **Die Whitelist wird ausschließlich im `AwxClient`** beim Schritt
  AWX→Canonical angewandt. Neue interne AWX-Felder können strukturell nicht
  durchsickern.

---

## 5. Verzeichnisstruktur

```
awx-migration/
├── awx_backup.py            # UNVERÄNDERT
├── awx_restore.py           # UNVERÄNDERT
├── awx_export.py            # NEU – Orchestrator
├── awx_import.py            # NEU – Orchestrator
├── lib/
│   ├── config.py            # nur additive Konstanten (AWX_ADMIN_SECRET, AWX_ADMIN_USER, AWX_SERVICE)
│   ├── (kubectl, secrets, postgres, archive, manifest,
│   │     registry_*, k3s_registry, logger, utils)   # UNVERÄNDERT
│   ├── canonical.py         # NEU – CanonicalObject
│   ├── awx_objects.py       # NEU – Registry (Whitelist, Relations, Hooks, Order)
│   ├── awx_cli.py           # NEU – awx-Binary-Wrapper
│   ├── awx_client.py        # NEU – Fassade (ABC + AwxCliClient + make_client)
│   ├── awx_connection.py    # NEU – Verbindungsauflösung (NodePort)
│   ├── export_format.py     # NEU – stabiles Dateiformat
│   ├── migrations/          # NEU – Formatmigrationen
│   │   ├── __init__.py      #   Runner: migrate_document(doc, kind), STEPS
│   │   ├── migration_001.py #   (Platzhalter — erst bei Formatänderung)
│   │   └── migration_002.py #   (Platzhalter)
│   ├── exporter.py          # NEU
│   └── importer.py          # NEU
├── tests/
│   ├── conftest.py          # FakeAwxClient, FakeKubectl, Fixtures
│   ├── unit/                # siehe Teststrategie
│   └── e2e/                 # opt-in Integrationstests
│       ├── test_export_import_template.py
│       └── test_export_import_inventory.py
└── docs/
    └── export-import-design.md   # dieses Dokument
```

### Dateiformat

**Referenzen ausschließlich über natürliche Schlüssel** — niemals interne IDs:

```
OK      "inventory": "Linux",   "organization": "Default"
FALSCH  "inventory_id": 42,     "organization_id": 1
```

Dies ist verbindlich: Der `AwxClient` löst beim Export jede Referenz
(`relations`) auf den natürlichen Schlüssel des Zielobjekts auf und beim Import
wieder zurück. IDs sind instanz-spezifisch und dürfen die Datei nie erreichen.

`job_templates.json` (Beispiel):

```json
{
  "format_version": 1,
  "schema_version": 1,
  "kind": "type_file",
  "object_type": "job_templates",
  "tool_version": "0.2.0",
  "awx_version": "24.6.1",
  "organization": "Default",
  "exported_at": "2026-07-16T14:30:22Z",
  "count": 18,
  "objects": [
    { "name": "Deploy", "organization": "Default", "project": "Infra",
      "inventory": "Linux", "playbook": "deploy.yml" }
  ]
}
```

`manifest.json` trägt `"kind": "manifest"`, `"contains_secrets": false` und die
Typ-Übersicht (`object_types: {job_templates: {count, file}}`).

**`format_version` vs. `schema_version`** — bewusst getrennt:

- `format_version`: Version des **Datei-Envelopes** (welche Top-Level-Schlüssel
  existieren, wie Objekte abgelegt sind). Steuert die Migrationskette in
  `lib/migrations/`.
- `schema_version`: Version des **kanonischen Objekt-Schemas** (welche
  Whitelist-Felder ein Typ hat). Kann sich unabhängig vom Envelope ändern.

Beispiel: `format_version = 1`, aber `schema_version = 2` — der Rahmen ist
gleich geblieben, nur die Felder eines Objekttyps haben sich weiterentwickelt.
Das vereinfacht Migrationen. `schema_version` ist optional nutzbar.

---

## 6. Migrationsschicht — `lib/migrations/`

```
# __init__.py
CURRENT_FORMAT = FORMAT_VERSION
STEPS: dict[int, Callable]   # {1: migration_001.upgrade, 2: migration_002.upgrade, ...}

def migrate_document(doc: dict, kind: str) -> dict:
    v = doc.get("format_version", 1)
    while v < CURRENT_FORMAT:
        doc = STEPS[v](doc, kind)   # kind ∈ {"manifest", "type_file"}
        v += 1
    return doc
```

- Nummerierte Module `migration_001.py`, `migration_002.py`, … (jeweils
  `upgrade(doc, kind) -> dict`). Beim Einlesen von z.B. v2 laufen automatisch
  001 → 002 → 003 → … bis `CURRENT_FORMAT`. Das skaliert über beliebig viele
  Versionssprünge.
- `export_format.read_type_file` / `read_manifest` rufen `migrate_document`
  **transparent** auf. Das Schreiben nutzt immer die aktuelle Version.
- Heute: `FORMAT_VERSION = 1`, `STEPS` leer → Passthrough. Die Naht existiert,
  damit Altexporte später ohne Änderung an `Exporter`/`Importer` migriert
  werden können.

---

## 7. CLI-Optionen

**Export**

```
awx_export.py [--all | --type TYPE ...] [--name NAME]
    --output DIR         # Default: awx-export-YYYYMMDD-HHMMSS/
    --organization ORG   # ORG oder "ls" (listet Organisationen)
    --namespace NS       # Default: awx
    --awx-host URL       # Default: aus NodePort abgeleitet
    --awx-username USER  # Default: admin
    --awx-password PASS  # Default: aus Secret awx-admin-password
    --awx-token TOKEN    # Alternative zu user/pass
    --insecure           # TLS-Verify aus
    --verbose
```

**Import**

```
awx_import.py PATH                       # Export-Verzeichnis oder einzelne JSON-Datei
    --type TYPE ...                      # nur diese Typen aus dem Bundle
    --name NAME                          # nur dieses Objekt
    --on-conflict {update,skip,fail}     # Default: update
    --namespace / --awx-host / --awx-username / --awx-password / --awx-token / --insecure
    --verbose
```

- `--dry-run` ist **nicht** Teil von Phase 1 (Nutzen gering, hängt von der
  awx-CLI-Version ab; später nachrüstbar).
- **Verbindung: NodePort** (`kubectl.node_ip()` + Service) als Primärweg —
  konsistent mit dem Restore-Registry-Pfad. `kubectl port-forward` höchstens
  später als Fallback, wenn ein Cluster keinen NodePort bereitstellt.

### Namenskonflikte

Natürlicher Schlüssel, bei org-gebundenen Typen `(name, organization)` — zwei
Organisationen dürfen je ein „Demo"-Inventory haben; das ist kein Konflikt.

| Policy | Verhalten |
| --- | --- |
| `update` (Default) | vorhandenes Objekt aktualisieren, sonst neu anlegen |
| `skip` | nur anlegen wenn fehlend; Bestehendes unangetastet (Existenz-Vorabprüfung) |
| `fail` | Abbruch, wenn ein Name schon existiert |

---

## 8. Teststrategie

**Naht = Fassade.** Unit-Tests injizieren `FakeAwxClient` (liefert vorgefertigte
`CanonicalObject`s, protokolliert `import_objects`) → kein AWX, kein Cluster,
kein Subprozess. Export/Import sind damit unabhängig von Backup/Restore testbar.

### Unit-Tests (`tests/unit/`)

| Test | prüft |
| --- | --- |
| `test_export_format` | Round-trip write→read; `format_version`/`schema_version`; Zähler; Ablehnung unbekannter Version |
| `test_migrations` | v1→CURRENT Passthrough; simulierte 001/002-Kette in Reihenfolge |
| `test_awx_objects` | Registry-Integrität; `depends_on` azyklisch; **Dummy-Typ fließt durch Export+Import ohne Änderung an exporter/importer** (Beweis Erweiterbarkeit) |
| `test_exporter` | export_all/type/object; eine Datei pro Typ; Manifest korrekt; `contains_secrets=false`; Hooks (`post_export`, `exporter`) greifen |
| `test_importer` | Bundle-Aufbau; `--type`/`--name`-Filter; `on_conflict` update/skip/fail; Import-Reihenfolge; `post_import`-Phase; Credential-Warnung; `ImportResult`-Aggregation |
| `test_awx_cli_client` | `AwxCli.run` gemockt: korrekte `awx export`-Args; **Whitelist greift** (nicht gelistetes Feld fehlt); **Referenz→natürlicher Schlüssel** (kein `*_id`); lokaler Org-Filter |
| `test_awx_connection` | `resolve_connection` mit `FakeKubectl` (NodePort-Ableitung, `--awx-host`, Token vs. User/Pass) |
| `test_layering` | statische Prüfung der Import-Matrix (kein verbotener Import) |

### Roundtrip-Test (der wichtigste Test)

```
Export ──► Import ──► Export ──► diff
```

Sind der erste und der zweite Export identisch, ist der gesamte Datenpfad
(Whitelist, Referenz-Mapping, Normalisierung, Serialisierung) korrekt. Läuft mit
`FakeAwxClient`, der einen In-Memory-AWX-Zustand hält — deterministisch, ohne
echtes AWX.

### Integrationstests (`tests/e2e/`, opt-in)

| Test | prüft |
| --- | --- |
| `test_export_import_template.py` | echtes `awx`-CLI gegen reale AWX: Job Template exportieren → importieren → Existenz/Whitelist-Felder verifizieren |
| `test_export_import_inventory.py` | dito für Inventory inkl. Org-Zuordnung |

Opt-in via Marker `@pytest.mark.e2e`, übersprungen solange `AWX_E2E_HOST` nicht
gesetzt ist → CI ohne AWX bleibt grün.

---

## 9. Begründung der zentralen Designentscheidungen

- **`CanonicalObject` statt Per-Typ-Klassen:** hält die Registry als einzigen
  Erweiterungspunkt und vermeidet doppelte Schemata (Feldliste **und**
  Whitelist). Optionaler `validator`-Hook deckt strengere Fälle additiv ab.
- **Whitelist statt Blacklist:** eine deklarative Positivliste pro Typ ist
  robuster — neue interne AWX-Felder wandern nie automatisch mit. Durchsetzung
  an genau einer Stelle (AWX→Canonical im Client).
- **Referenzen nur über natürliche Schlüssel:** IDs sind instanz-spezifisch und
  bei Migration zwischen Clustern wertlos; Namen sind portabel und git-lesbar.
- **Optionale Hooks in der Registry:** lösen Sonderfälle (z.B.
  Workflow-Knoten-Verknüpfung) ohne typ-spezifische `if`-Zweige in
  `Exporter`/`Importer`.
- **`Exporter`/`Importer` erhalten eine `list[ObjectType]`:** sie kennen die
  globale Registry nicht → eine Schicht sauberer und flexibel testbar.
- **Fassade `AwxClient`:** kapselt das gesamte AWX-Wissen. Wechsel CLI → REST
  betrifft nur diese Schicht; obere Schichten bleiben unverändert. Grund:
  awxkit-Versionen verhalten sich inkonsistent, das Werkzeug muss davon
  unabhängig bleiben.
- **Getrennte `format_version` / `schema_version`:** erlaubt unabhängige
  Weiterentwicklung von Datei-Envelope und Objekt-Schema und vereinfacht
  spätere Migrationen.
- **Kein Eingriff in Backup/Restore:** kein bestehendes Modul wird modifiziert;
  das Export-Format ist vom Backup-`Manifest` vollständig getrennt.
```
