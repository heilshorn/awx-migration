Dieses Repository enthält Tools zum Sichern und Wiederherstellen von AWX-Installationen  sowie zum Exportieren und Importieren einzelner AWX-Objekte für Migrationszwecke und zur Versionskontrolle.
Die Tools wurden lauffähig getestet under der AWX-Version 24.6.1.

-----

Backup und Restore

Diese Funktion ist gedacht, Um die gesamte AWX-Datenbank zu sichern und wiederherzustellen, bzw. auf einem anderen Host zu übertragen.

Um die entsprechenden Befehle im Ordner awx-migration auszuführen, müssen zuerst die Pythonskripte in dem Ordner ausführbar gemacht werden (chmod +x *py im Ordner).

Die Programme python, pip (python3-pip) und skopeo müssen installiert sein.

Das Modul PyYAML aus der Datei requirements.txt muss installiert werden (pip install -r requirements.txt).

Ein komplettes Backup kann mit folgendem Befehl erstellt werden:

awx_backup.py --registry-namespace registry

Dann wird eine tar.gz-Datei mit aktuellem Datum und Uhrzeit erstellt. Diese kann man auf einem anderen Host übertragen, um dort die AWX-Daten zu restoren oder einfach als Sicherung verwahren.


Für den Restore gibt es folgende Syntax:

awx_restore.py from/to optional bei --restore-registry

Bsp:

awx_restore.py --registry-from "10.6.207.31:30500" --registry-to "192.168.121.185:30500" --restore-registry awx-backup-20260713-143748.tar.gz

-----

AWXKit

AWXKit ist die offizielle Python-Bibliothek und das zugrundeliegende Framework für das Command Line Interface (CLI) von Ansible AWX.

Also man hat damit die Möglichkeit von der Console diverse AWX-Befehle einzugeben, um damit AWX zu administrieren.

Bsp:

awx ping

pingt mir AWX den eigenen AWX-Host, dient i.d.R. dazu, um zu testen ob awx funktioniert.

awx config

zeigt die awx-cli-Konfiguration an

awx users list

Auflistung sämtlicher User mit etlichen Angaben, wie Erstellungsdatum, letzter Login, etc.

awx job list

Auflistung der Jobs in AWX mit den entsprechenden Infos. Jedoch nur eine Seite, alle mit awx job list –all

Die Installation von AWXKit ist Vorraussetzung für den Ex- Und Import von AWX-Objekten, da dort AWX-Befehle genutzt werden.

-----

Ex- und Import von AWX-Objekten

Mit den entsprechenden Ex- und Import-Befehlen, die mit dem Git-Repo https://github.com/heilshorn/awx-migration mitgeliefert werden,

kann man einzelne AWX-Objekte, wie Job-Templates, Projekte, Inventories, sichern, um sie nach einer Änderung später wiederherzustellen oder um sie auf einen anderen AWX-Host zu übertragen.

Bsp. für einen Export:

awx_export.py --type job_templates --name "Ping"

Hier wird das Job-Template mit dem Namen Ping exportiert. Anschließend wird ein Ordner mit den entsprechenden Dateien erstellt. Diesen Ordner kann man dann später für ein Restore benutzen.

Die genaue Syntax kann man sich anzeigen lassen mit awx_export.py -h

Bsp. für einen Restore:

awx_import.py awx-export-20260827-133211

awx-export-xxxx ist hier der Ordnernamen, der durch den vorherigen Export erzeugt wurde, auch hier man wieder die Syntax mit awx_import.py -h

-----

AWX-Review

Bei AWX-Review handelt es sich um en Utility, um sich auf der Console des AWX-Hosts entsprechende Informationen über Jobs ausgeben zu lassen. Schwerpunkt dieses Utilities ist die Anzeige von Änderung bei einem Host, die

ein entsprechender AWX-Job ausgelöst hat oder auslösen würde. Will man sich nur die Änderungen anzeigen lassen, die ein Job auslösen würde, muss ein Template erstellt werden mit dem Job-Type "Check" und der Auswahl

"Show Changes".

Das Utility wird bereitgestellt über das Git-Repo https://github.com/heilshorn/awx-review

Nach dem Klonen des Repos muss noch im Ordner das Modul awx-review installiert werden mit

pip install .

folgende 3 Befehle stehen anschließen zu Verfügung:

  
  

**awx-review job jobnr** (Die Jobnr. ist aush der AWX-Oberfläche zu entnehmen):

allg. Infos über den Job, wie Name. Status und wann er ausgeführt wurde.

  
  

**awx-review changes jobnr**

sehr ausführliche Ausgabe, welche Änderungen ein Job bei den einzelnen Hosts verursacht hat oder verursachen würde.

  
  

**awx-review summary jobnr**

statistische Ausgabe, wieviele Änderungen insgesamt bei den Hosts, mit einzelner Auflistung der Hosts, vorgenommen wurden. Daneben werden noch die Änderungen durch die einzelnen Module aufgelistet und auch entsprechende

Failures wenn vorhanden.
