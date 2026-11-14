# Schweizer Buchhaltung: Bestandsaufnahme und Verbesserung

Stand: 14. November 2026, Swiss-App 1.34.5. Diese Abnahme erweitert 1.34.4
(MWST-Historie, automatische Ziffernzuordnung und eCH-0217 2.0.0).
Keine Bankübermittlung, erneute MWST-Einreichung oder Änderung bestehender
Buchungen ist Teil dieses Releases.

## Was konkret verbessert wurde

- Schweizer Einzelzahlungs-, Lohn- und Zahlungsvorschlagsexporte verwenden
  einen gemeinsamen pain.001.001.09-Serializer und obligatorisch das lokale
  Schweizer CH03-Schema. Die alte optionale XSD-Einstellung schaltet diese
  Schweizer Validierung nicht ab. Andere Länder behalten ihren bisherigen Weg.
- Beträge werden dezimal und in Empfängerwährung geprüft. Keine stille Rundung,
  abgeschnittenen Adressen oder unvollständigen Zahlungsdateien. XML-Escaping,
  vollständige strukturierte Orts-/Landangaben, IBAN-Prüfsumme, QR-Referenz,
  RF-Referenz und erforderliche BIC werden vor dem Export geprüft.
- SEPA setzt EUR und IBAN-Präfixe im EPC-Geltungsbereich voraus. Die tatsächliche
  Bankteilnahme und bankspezifische Annahme bleiben separat zu bestätigen.
- Alle ausgewählten Einzelzahlungen werden vor der ersten Buchung geprüft.
  Ein späterer Buchungsfehler rollt die gesamte Gruppe inklusive Datei-Datensätzen
  zurück. Sortierte Zeilensperren und die Oberfläche verhindern Doppelaufrufe.
- Die bestätigte Exportaktion bucht ausschließlich die explizit ausgewählten
  Entwürfe. Jede Zahlung erhält die identische geprüfte Datei als privaten Anhang.
  Der Download selbst erteilt keinen Auftrag an eine Bank.
- Lohnexporte berücksichtigen ausschließlich die Mitarbeiter und eingereichten
  Lohnabrechnungen des konkreten Payroll Entry und Zeitraums. Andere Lohnläufe,
  Entwürfe, fehlende Mitarbeiter und negative Beträge werden nicht still vermischt.
- Lohnläufe und Schweizer Zahlungsvorschläge behalten ein privates Original mit
  derselben Bank-Meldungskennung. Ein erneuter Download erzeugt keine neue
  Zahlungsdatei. Veränderte Quelldaten erfordern Prüfung statt eines zweiten Exports.
- Die Entwurfsliste ist nicht länger auf die Frappe-Standardseite mit 20 Treffern
  beschränkt. Auswahl bleibt auf 1000 Zahlungen pro Export begrenzt.
- Der gemeinsame Adressabruf verwendet parametrisierte Abfragen und prüft die
  Leseberechtigung des zugehörigen Dokuments.

## Bestand und tatsächlich noch offene Abnahmen

Das lesende Laufzeitinventar vom 14.11.2026 erfasst zwei aktive, zugeordnete
Firmenbankkonten (CHF/EUR), eine aktivierte EBICS-H005-Verbindung und aktivierte
Synchronisation. Es zeigt keine empfangenen ebicsStatement-Dokumente und keine
BankTransactionRule-Dokumente. Aktivierte Einstellungen beweisen keinen
erfolgreichen Bankempfang.

1. Bankanbindung: echten camt-Empfang, Quittung und Übergabe an den vorhandenen
   Abgleich mit der Bank nachweisen. Keine automatischen Bankaktionen durch
   diese Bestandsaufnahme. Zahlungsversand, Bank-/VEU-Freigabe und pain.002
   bleiben die gesondert freizugebende Anschlussstufe des Banking-Programms.
2. Bankregeln: Regeln nur mit bestätigten Geschäftsfällen und repräsentativen
   Originalauszügen einrichten; ohne solche Evidenz keine Buchungslogik erfinden.
3. Lohn: SALA-Kennzeichnung ist kein Nachweis bankseitiger Vertraulichkeit.
   CND/NOA, Bankannahme, Swissdec/ELM und Jahreslohnabschluss sind gesonderte
   fachliche bzw. bankseitige Abnahmen, keine behauptete Zertifizierung.
4. Buchhaltungsabschluss: zusätzliche Fälle wie Fremdwährungsbewertung,
   Import-/Bezugssteuer, gemischte Vorsteuer und Abschlussbuchungen fachlich
   mit dem Treuhänder prüfen. Die automatische Zuordnung bucht nicht rückwirkend
   historische Belege um und erfindet keine fehlenden Steuergrundlagen.
5. MWST: Q2/Q3 und Originaldateien bleiben unverändert und als vom Benutzer
   eingereicht dokumentiert. XML-Download bedeutet weiterhin nicht automatische
   Übermittlung oder eine neue ESTV-Einreichung.

## Prüfbasis

- Python-Regressionen einschließlich MWST, Zahlungsabgleich, QR, Lohn und Rechte.
- Schweizer XML-Fälle mit Dezimalbeträgen, QR-/RF-Referenzen, mehreren Konten,
  Währungen, Auslands-BIC, strukturierten Adressen und unveränderten Originalen.
- Browserlogik: Doppelbetätigung, leerer Export und Freigabe nach Fehlern.
- Vollständiges unverändertes Quellmanifest und Python-Paketkonsistenz im Image.
- Isolierte Wiederherstellung der vollständigen Produktionssicherung vor Freigabe.
  Der native Zahlungstest muss ungültige Gruppen, eine fehlgeschlagene zweite
  Buchung und erfolgreiche native Buchung samt privaten Originalen prüfen und
  abschließend vollständig zurückrollen. Bestehende Finanzdaten werden gehasht.
- Nach Veröffentlichung: tatsächliches Image, App-Version, MWST-Originale und
  Finanzdatenintegrität kontrollieren. Belege und Kontodaten bleiben außerhalb Git.

## Verbindliche externe Referenzen

- SIX Swiss Payment Standards / Überweisungen 2026, pain.001.001.09 und CH03-XSD.
- EPC409-09 v8.0, SEPA-Geltungsbereich (geprüft am 14.11.2026).
- ESTV eCH-0217 Version 2.0.0, Import und anschließende Einreichung im ESTV-Portal.

Die konkreten Build-, Sicherungs-, Restore- und Live-Abnahmebelege gehören zum
jeweiligen Releaseverzeichnis. Diese Bestandsaufnahme behauptet keine Prüfung
jedes denkbaren Schweizer Geschäftsfalls und keine universelle Bankannahme.
