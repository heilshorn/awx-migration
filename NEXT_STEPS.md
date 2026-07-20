Version 2.0 – aktueller Stand
=============================

Status
------
✔ Backup/Restore abgeschlossen (1.x)
✔ Export implementiert
✔ Import implementiert
✔ Validator implementiert
✔ CLI implementiert
✔ cli_common implementiert
✔ 186 Unit-Tests erfolgreich (zuvor 173; +13 durch die E2E-Phase)
✔ E2E-Test-Harness implementiert (opt-in)
✔ E2E gegen reale AWX 24.6.1 erfolgreich (Inventory + Job Template, 2 passed)
✔ awx_export.py auf cli_common umgestellt (reines Refactoring, verhaltensneutral)

Noch NICHT erledigt
-------------------
1. README ergänzen
2. Changelog
3. Release 2.0

Wichtig
-------
E2E-Lauf ist grün → Refactorings sind jetzt erlaubt.
Backup/Restore bleibt unangetastet; alle Änderungen additiv.
