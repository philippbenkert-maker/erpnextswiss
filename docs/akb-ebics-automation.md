# AKB EBICS: sicherer Tagesabruf und beleggestützte automatische Buchung

Der Scheduler-Einstieg `erpnextswiss.erpnextswiss.ebics.sync` verwendet ab dieser
Version `ebics_automation`. Auch der manuelle H005-Abruf delegiert an diesen
abgesicherten Pfad. Legacy-Importer dürfen nicht direkt als Tagesjob laufen.

## Aktivierung

1. Bankseitige Freigabe, HPB und Schluesselaktivierung am konkreten Teilnehmer
   pruefen. Der Fingerabdruck der Bank-Authentifizierungs- und
   Verschluesselungsschluessel soll unabhaengig bestaetigt werden; eine bewusste
   Ausnahme ist gesondert zu dokumentieren.
2. In der **Site-Konfiguration**, nie im Repository, die exakten ERP-Bankkonten
   binden: `ebics_auto_accounts = {"version": 1, "bindings": {"<Connection-Name>":
   ["<CHF-Bankkonto>", "<EUR-Bankkonto>"]}}`. Fehlende, doppelte, deaktivierte
   oder fremde Konten blockieren den Abruf. Kein Konto wird aus einer Bankdatei
   als berechtigtes Ziel abgeleitet.
3. Ein Test auf dem isolierten Test-Site muss Migration, Download-Archiv,
   Bankquittung, Wiederanlauf, doppelte Transaktionen und mindestens je eine
   passende und unpassende Bankzeile abdecken. Die Tests duerfen keine echten
   Bank- oder Finanzdaten schreiben.
4. Produktionsbackup der Datenbank und des persistenten `sites`-Volumes pruefen.
   Erst danach `enable_sync` fuer die aktivierte H005/CH-Verbindung mit
   `statement_btf_version=08` setzen. Ein kontrollierter Einzelabruf und die
   Kontrolle der Buchungsbelege gehen dem unbeaufsichtigten Tageslauf voraus.

## Empfangs- und Buchungsgrenzen

- BTD/EOP/CH/ZIP/camt.053.001.04 oder .08 entsprechend der Verbindung:
  maximal 14 aktuell bereitstehende Pakete pro Lauf, ohne Datumsbereich.
  Datei-Hashes verhindern die erneute Verarbeitung identischer Auszüge.
- Vom Python-SDK gelieferte XML-Inhalte werden als UTF-8 in einem
  `EBICS Download`-Datensatz gespeichert. Das JSON-Envelope und sein SHA-256
  werden nach einem ERP-Commit rueckgelesen. Erst dann geht die EBICS-Quittung
  an die Bank. Der SDK-seitig entpackte XML-Inhalt ist erhalten; dessen urspruengliche
  Bytekodierung und das urspruengliche
  Bank-ZIP ist **nicht** Teil dieses Legacy-SDK-Pfads. Der gesonderte PHP-Gateway
  bleibt fuer den bytegenauen Original-ZIP-Empfang das langfristige Ziel.
- Ein offener Bank-Receipt wird vor einem neuen Download erneut quittiert.
  Scheitert der Receipt oder ist dessen Zustand unklar, erfolgt kein zweiter
  automatischer Abruf. Datensaetze mit `ack_state=Pending` erfordern Sichtung.
- Vor der Quittung werden **alle** XML-Dateien gegen das vorhandene camt-Schema
  und die explizit gebundenen Konten geprueft. Ungueltige oder uebergrosse
  Dateien werden nicht positiv quittiert.
- Eine Zahlung wird nur fuer einen einzelnen, gebuchten, nicht stornierten
  Bankeintrag mit stimmiger Auszugsbilanz, einem Kandidaten, einer eingereichten
  Rechnung derselben Firma/Partei/Waehrung und exakt gleichem offenen Betrag
  gebucht, oder mit dem unten beschriebenen vertraglichen Skonto.
  Die Bankreferenz und Rechnungsreferenz muessen nachweisbar passen.
  Einzeltransaktionsreferenzen aus validiertem XML und die dokumentierte
  Gruppenreferenz mit Suffix `-1` werden bei genau einer Einzelposition erkannt.
  Eine Kontonummer allein ist keine eindeutige Zahlungsreferenz.
  Es gibt keinen pauschalen Kurs-/Rundungs-Differenzausgleich. Sammelbuchungen,
  Spesen, Lohn, mehrdeutige Treffer und alle Fehler bleiben zur Pruefung.
- Es wird **kein** pain.001 versandt und kein Zahlungsvorschlag freigegeben.

## Vertragliches Skonto und Wiederprüfung

Für Ausgangsrechnungen wird Skonto nur erkannt, wenn die gesamte Rechnung
noch offen ist, genau eine unverbrauchte Zahlungsbedingung vorliegt und das
Bankbuchungsdatum zwischen Rechnungsdatum und Skontofrist liegt. Prozent- und
Festbeträge werden auf Rappen gerundet; der Bankbetrag muss exakt dem
resultierenden Zahlbetrag entsprechen. Ein ähnlich grosser Abzug reicht nicht.
Fremdwährungen, Teilzahlungen, Gutschriften und Lieferantenskonto bleiben in
dieser Ausbaustufe zur Prüfung.

Voraussetzung zum Buchen: `Company.default_discount_account` ist ein aktives
Ertragskonto derselben Firma, Firmen- und Rechnungswährung stimmen überein und
die Site-Konfiguration enthält ausdrücklich `ebics_auto_discount_tax_split: true`.
ERPNext erstellt die native Zahlungsreferenz und teilt den Skonto auf Erlös und
die Steuerkonten der Originalrechnung auf. Allenfalls höchstens zwei Rappen
Differenz zwischen gerundeten Teilbeträgen werden auf das konfigurierte
Rundungskonto gebucht. Die globale Einstellung für manuelle Zahlungen wird nicht
verändert. Ohne gültige Kontierung wird nichts gebucht.

Bestätigte Archive im Status `Review` werden im nächsten Lauf erneut gegen
den aktuellen Rechnungsstand geprüft (zusammen mit `Pending` maximal 100
Archive pro Lauf). Bereits gebuchte Zahlungen bleiben über ihre Referenz
gesperrt. Bereits eingereichte Zahlungen werden nur dann als erledigt gewertet, wenn
Bankkonto, Betrag, Datum und Währung exakt übereinstimmen; ein vorhandener
Entwurf genügt nicht. Falls ein früherer Kontoauszug und der aktuelle
EBICS-Abruf unterschiedliche Referenzen liefern, wird zusätzlich die aus dem
validierten `O/...`-Feld gewonnene E-Banking-Nummer mit der ausdrücklich als
`E-Banking` bezeichneten Nummer in den Zahlungsbemerkungen abgeglichen. Dafür
müssen genau ein Empfänger, genau eine eingereichte Zahlung und genau eine
vollständig ausgeglichene Rechnung derselben Firma, Partei und Währung
nachgewiesen sein. Die Rechnungsreferenz muss auch im aktuellen Banktext stehen.
Mehrdeutige oder nur ähnlich aussehende Bewegungen bleiben zur Prüfung.
Buchungszähler bleiben bei Wiederholung kumulativ erhalten.

Die native Abnahme `scripts/test_ebics_matching_restore.py` darf ausschliesslich
auf der internen Wegwerf-Site `kt-platform-test.local` ausgeführt werden. Sie
prüft echte Einreichung, offene Posten, Skonto-/Steueraufteilung, ausgeglichene
Hauptbuchzeilen und Wiederholbarkeit; sämtliche Testbuchungen werden zurückgerollt.

## Betrieb und Ruecknahme

`EBICS Download` zeigt Empfangs-, Quittungs- und Buchungsstatus sowie die Anzahl
automatischer Buchungen und Prueffaelle. Fehlerlogs enthalten nur Fehlertypen,
keine Banknutzdaten. Bei Problemen zuerst `enable_sync=0` setzen. Archivierte
Bankdaten und bereits gebuchte `Payment Entry` bleiben bestehen und duerfen
nicht durch ein Code-Rollback automatisch geloescht oder storniert werden.
Unquittierte Downloads und unklare Buchungsbelege sind fachlich einzeln zu
pruefen, bevor der Tageslauf wieder aktiviert wird.
