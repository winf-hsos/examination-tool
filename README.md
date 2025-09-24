# Examination Tool

Ein leichtgewichtiges Web-Tool zur Unterstützung mündlicher Prüfungen. Es kombiniert die Verwaltung von Aufgaben, Studierenden und Kategorien mit einer automatischen Zuweisung zufälliger Aufgaben unter Berücksichtigung von Abhängigkeiten.

## Funktionsumfang

- **Studierendenverwaltung**: Import einer vorab bekannten Liste von Studierenden aus einer XLSX-Datei. Paarprüfungen werden automatisch über die Spalte `Partner` erkannt und als gemeinsame Gruppe abgelegt.
- **Aufgabenbank**: Aufgaben lassen sich in Markdown (inkl. LaTeX via MathJax) verfassen. Jede Aufgabe besitzt eine Ober- und Unterkategorie sowie optionale Hinweise und Lösungen. Abhängigkeiten zwischen Aufgaben werden berücksichtigt.
- **Kategorien**: Konfiguriere, wie viele Aufgaben pro Kategorie während einer Prüfung gezogen werden sollen.
- **Prüfungssitzungen**: Starte mit einem Klick eine Prüfung für eine oder zwei Personen. Der Dozenten-Bildschirm zeigt Aufgaben inklusive Hinweise und Lösungen an; parallel steht eine Studierendenansicht ohne vertrauliche Informationen zur Verfügung. Die Studierendenansicht aktualisiert sich automatisch.

## Installation

1. Optional: Virtuelle Umgebung anlegen

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
   ```

2. Abhängigkeiten installieren

   ```bash
   pip install -r requirements.txt
   ```

3. Datenbanktabellen werden beim ersten Start automatisch erstellt (SQLite `examination.db`).

## Anwendung starten

```bash
uvicorn app.main:app --reload
```

Anschließend steht die Anwendung unter <http://127.0.0.1:8000> zur Verfügung.

## Datenimport

- Erwartete XLSX-Spalten (Groß-/Kleinschreibung egal):
  - `Name` (Pflicht)
  - `Partner` (optional, Name der zweiten Person in einer Paarprüfung)
  - `Group` (optional, individueller Anzeigename der Gruppe)
- Jede Gruppe wird nur einmal angelegt, auch wenn mehrere Zeilen denselben Partner enthalten.

## Tests ausführen

```bash
pytest
```

## Hinweis zu Markdown und Formeln

Markdown-Inhalte werden serverseitig in HTML gerendert und via MathJax im Browser dargestellt. Verwende `\( ... \)` oder `$$ ... $$` für mathematische Formeln.

## Lizenz

Dieses Projekt dient als Referenzimplementierung und kann frei erweitert oder an eigene Anforderungen angepasst werden.
