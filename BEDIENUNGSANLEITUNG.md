# Archiva Bedienungsanleitung

> Arbeitsstand: lebendes Dokument. Diese Anleitung soll mit dem Produkt mitwachsen und bei neuen Features direkt erweitert werden.

## 1. Wofür Archiva gedacht ist

Archiva ist ein ECM für strukturierte Dokumentablage mit:
- **Admin-Bereich** für Definitionen und Datenmodell
- **App-Bereich** für tägliche Nutzung und Dokumenterfassung
- **Workflow Designer** für Prozesslogik, Schritte und Transitionen

---

## 2. Oberflächen im Überblick

### `/ui/admin`
Für die **Definitionsebene**:
- Cabinettypen anlegen
- Registertypen anlegen
- Dokumenttypen anlegen
- Metadatenfelder definieren
- Identity-/Zuweisungslogik vorbereiten

### `/ui/app`
Für die **tägliche Nutzung**:
- Dokumente hochladen
- Metadaten erfassen
- Dokumente durchsuchen
- Dokumentdetails ansehen

### `/ui/workflow-designer`
Für die **Workflow-Definition**:
- Workflows anlegen
- Schritte anlegen, bearbeiten, löschen
- Reihenfolge grafisch sortieren
- Transitionen anlegen, bearbeiten, löschen
- Rücksprünge und Schleifen sichtbar machen
- Workflows duplizieren
- neue Versionen eines Workflows erzeugen

---

## 3. Typischer Arbeitsablauf

### Schritt 1: Struktur im Admin anlegen
Im Admin-Bereich zuerst definieren:
1. Cabinettyp
2. Registertyp
3. Dokumenttyp
4. Metadatenfelder

Beispiel:
- Cabinettyp: `ERB`
- Registertyp: `Rechnungen`
- Dokumenttyp: `Eingangsrechnung`
- Metadatenfelder: Rechnungsnummer, Datum, Betrag

### Schritt 2: Dokumente in der App erfassen
Im App-Bereich:
1. Dokumenttyp wählen
2. Datei hochladen
3. Metadaten ausfüllen
4. speichern

### Schritt 3: Workflows definieren
Im Workflow Designer:
1. Workflow anlegen
2. Schritte anlegen
3. Reihenfolge festlegen
4. Transitionen zwischen Schritten definieren
5. optional Rücksprünge/Schleifen modellieren

---

## 4. Workflow Designer – Bedienung

## 4.1 Workflow anlegen
Links im Bereich **Workflow anlegen**:
- Name vergeben
- optional Beschreibung eintragen
- aktiv/inaktiv festlegen
- speichern

## 4.2 Schritt anlegen
Innerhalb eines ausgewählten Workflows:
- Name
- Step Key
- Reihenfolge
- Frist in Tagen
- Zuweisung
- Beschreibung

Dann **Schritt speichern**.

## 4.3 Schritte grafisch sortieren
Im Designer oben können Schritte per Drag-and-Drop in der Reihenfolge verändert werden.
Danach **Grafische Reihenfolge speichern**.

## 4.4 Schritt bearbeiten
Ausgewählten Schritt öffnen und Felder anpassen.

## 4.5 Schritt löschen
Ein Schritt kann nur gelöscht werden, wenn **keine eingehenden oder ausgehenden Transitionen** mehr auf ihm liegen.

Wenn noch Transitionen existieren, blockiert Archiva das Löschen absichtlich.

## 4.6 Transition anlegen
Für einen ausgewählten Schritt:
- Label vergeben
- Zielschritt wählen
- optional als **Standardübergang** markieren

## 4.7 Transition bearbeiten oder löschen
Bestehende Transitionen können direkt im Workflow Designer geändert oder entfernt werden.

---

## 5. Rücksprünge und Schleifen

Schleifen werden in Archiva **über normale Transitionen** modelliert.

Beispiele:
- `A -> B -> A` = Rücksprung / Schleife
- `A -> B -> C -> A` = längere Schleife

### Aktuelle Regeln
Nicht erlaubt sind:
- Übergang auf denselben Schritt (`A -> A`)
- leeres Label
- doppelte Transition mit gleichem Label und gleichem Ziel vom selben Schritt
- mehr als ein Standardübergang pro Schritt

### Sichtbarkeit im Designer
Rücksprünge und Schleifen werden im Designer markiert:
- **Rücksprung**
- **Schleife**
- **Standard**

Zusätzlich gibt es Hinweise, wenn:
- mehrere Ausgänge existieren
- ein Schritt Teil eines Schleifenpfads ist

---

## 6. Workflow duplizieren und versionieren

## 6.1 Workflow duplizieren
Button: **Workflow duplizieren**

Ergebnis:
- neuer Workflow mit kopierten Schritten
- kopierte Transitionen
- Name wird als Kopie angelegt, z. B. `Rechnungseingang (Kopie)`
- Kopie wird inaktiv angelegt

## 6.2 Neue Version anlegen
Button: **Neue Version anlegen**

Ergebnis:
- neue Version des bestehenden Workflows
- Schritte und Transitionen werden kopiert
- Name z. B. `Rechnungseingang (v2)`
- neue Version wird inaktiv angelegt

### Empfehlung
- **Duplizieren** für Varianten/Experimente
- **Versionieren** für kontrollierte Weiterentwicklung desselben Prozesses

---

## 7. Suche und Dokumenterfassung

Im App-Bereich können Dokumente mit Metadaten erfasst werden.
Archiva validiert Metadaten serverseitig anhand des definierten Dokumenttyps.

Der Suchstand ist hybrid:
- bestehende Volltext-Suche ist vorhanden
- Sucharchitektur wird noch weiter konsolidiert

---

## 8. Bekannte praktische Hinweise

- Nach Code-Änderungen kann es nötig sein, **Archiva neu zu starten**, damit neue UI-Routen wirklich live sind.
- Der Workflow Designer entwickelt sich aktiv weiter; die Anleitung sollte deshalb bei jeder größeren Funktion ergänzt werden.
- Die Reihenfolge der Schritte ist aktuell weiter wichtig, auch wenn Transitionen inzwischen zusätzlich sichtbar sind.

---

## 9. Vorschlag für künftige Erweiterungen dieser Anleitung

Sinnvolle nächste Kapitel:
- Admin-Bereich im Detail
- App-Bereich im Detail
- Benutzer- und Rollenlogik
- Dokument-Lifecycle
- typische Beispiel-Workflows
- Troubleshooting / häufige Fehler
- Release-Änderungen pro Version

---

## 10. Änderungslog der Anleitung

### 2026-04-24
Erste erweiterbare Bedienungsanleitung angelegt mit Fokus auf:
- Oberflächen
- Grundablauf
- Workflow Designer
- Rücksprünge / Schleifen
- Duplizieren / Versionieren
