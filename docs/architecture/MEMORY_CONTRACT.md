# SOLVIO MEMORY CONTRACT

**Status:** **PRODUKTIV, V2** (Adaptive Memory V1, 2026-08-26). Framework-neutral.
**Zweck:** Definiert *was* SOLVIOs Gedächtnis kann, unabhängig davon *womit* es implementiert wird.
**Verwandte Contracts:** [`TRUST_BOUNDARY.md`](TRUST_BOUNDARY.md), [`DEEP_RUNTIME_CONTRACT.md`](DEEP_RUNTIME_CONTRACT.md)
**Referenz-Interface:** `src/solvio/contracts/memory.py` — **wird vom Core importiert.**
**Entscheidung zu V2:** [ADR-0021](../decisions/ADR-0021-lernen-erreicht-nie-user-direct.md)

---

## 1. Warum dieser Contract zuerst kommt

SOLVIO darf **nicht** semantisch bedeuten „das Memory-Backend, das wir gewählt haben". Das Gedächtnis ist die eigentliche Architektur-Grundentscheidung — nicht die Frage „OpenClaw ja/nein". Wenn der Zugriff auf Erinnerungen hinter einem stabilen Interface liegt, kann das Backend ausgetauscht werden, ohne den Core, die Voice-Pipeline oder die Tool-Schicht anzufassen.

Der Benchmark (STEP 19C) testet OpenClaw **gegen dieses Interface**, nicht im luftleeren Raum. Egal wie die Entscheidung ausfällt, bleibt die Memory-Schicht austauschbar.

## 2. Designprinzipien (angelehnt an die bestehende Architektur)

Die bestehende `solvio-core`-Architektur hat wiederverwendbare Muster, an die dieser Contract andockt — es wird **keine zweite Architektur erfunden**:

| Bestehendes Muster (real im Code) | Übernahme im Memory-Contract |
|---|---|
| `RiskLevel(IntEnum)` 1/2/3 in `tools/base.py` | `forget()`/`supersede()`/`purge()` sind mutierend → Confirmation-Gate-fähig |
| `ToolResult.as_dict()` — serialisierbare Dicts, `human_message` für Sprache | `MemoryRecord` ist serialisierbar; `content` ist sprachtauglich |
| `Tool(Protocol)` + `@runtime_checkable` | `MemoryStore(Protocol)` als reines Interface |
| `PendingActionManager` — Core erzeugt Bestätigung, Modell nie | Löschen/Überschreiben/Purge von Memory folgt demselben Bestätigungsweg |
| Alles `async`, minimal, auditierbar, `from __future__ import annotations` | Contract ist async, ohne schwere Abhängigkeiten |
| Codex `provenance`-Denke (Herkunft, Sandbox, Snapshot) | `provenance`-Kette als First-Class-Feld |

Erfüllt wird dieser Contract heute von `src/solvio/memory/store.py` (`SolvioMemory`, stdlib-SQLite, zwei physisch getrennte Datenbanken). Ein anderes Backend kann ihn ebenso erfüllen — der Core importiert ausschließlich `solvio.contracts.memory.MemoryStore`.

## 3. Memory-Typen

Neun Typen. Bewusst nicht mehr — jeder zusätzliche Typ muss sich seine Existenz verdienen. Geprüft und **verworfen**: `TASK` (gehört in die Deep-Runtime-Task-State, nicht ins Gedächtnis), `SECRET` (Secrets werden **nie** als Memory-Wert gespeichert, nur Referenzen über die `sensitivity`-Klasse `SECRET_REFERENCE`), `LOCATION`/`DEVICE` (fallen unter `SEMANTIC`/`PROJECT`).

| Typ | Definition | Beispiel | Lebensdauer |
|---|---|---|---|
| `WORKING` | Kurzzeit-Kontext des aktuellen Gesprächs | „Wir reden gerade über Projekt X" | Session / Minuten |
| `USER` | Fakten über den primären Nutzer (Beispielnutzer) | „Beispielnutzer ist Rechtshänder" | Dauerhaft |
| `PEOPLE` | Fakten über andere Personen | „Beispielkontakt ist Beispielnutzers Steuerberater" | Dauerhaft |
| `EPISODIC` | Was ist wann passiert (Ereignisse) | „Gestern 15:00 Media Player im Wohnzimmer getestet" | Dauerhaft, alterungsfähig |
| `SEMANTIC` | Allgemeines Wissen, das SOLVIO gelernt hat | „Der XVF3800 unterdrückt Double-Talk bei niedrigem `PP_DTSENSITIVE`" | Dauerhaft |
| `PREFERENCE` | Präferenzen und Gewohnheiten | „Beispielnutzer bevorzugt kurze gesprochene Antworten" | Dauerhaft, supersession-typisch |
| `RULE` | Vom Nutzer gesetzte dauerhafte Regeln/Policies | „Bei Codex-Modify immer nachfragen" | Dauerhaft, hohe Wichtigkeit |
| `PROJECT` | Wissen zu laufenden Vorhaben | „SOLVIO Wake-Word steht bei V2, V3 offen" | Bis Projektende |
| `STANDING_INTENT` | Dauerhafte Absicht mit Trigger (Deklaration) | „Wenn sich bei Projekt X etwas Wichtiges ändert, sag Bescheid" | Bis erfüllt/abgelaufen |

**Abgrenzung `PREFERENCE` vs. `USER`:** `USER` = stabile Fakten (Name, Rolle). `PREFERENCE` = wandelbare Vorlieben, für die Supersession der Normalfall ist.

**`STANDING_INTENT` — Doppelnatur:** Der Record ist die *dauerhafte Deklaration* im Gedächtnis. Die *Auswertung* der Trigger und die Ausführung liegen in der Deep-Runtime (siehe [`DEEP_RUNTIME_CONTRACT.md`](DEEP_RUNTIME_CONTRACT.md)). Das Gedächtnis hält die Wahrheit „diese Absicht existiert und ist aktiv"; die Runtime handelt darauf.

## 4. Provenance & Source Types — der Vertrauens-Anker

**Grundregel:** Nicht jede Information verdient dieselbe Vertrauensstufe. „Beispielnutzer hat es direkt gesagt" ist etwas völlig anderes als „eine E-Mail behauptet es". Deshalb ist Herkunft ein **Pflichtfeld**, kein Metadatum.

Sieben Source-Types. Jeder mappt auf eine Trust-Klasse aus [`TRUST_BOUNDARY.md`](TRUST_BOUNDARY.md):

| `source_type` | Bedeutung | Trust-Klasse (Default) | Kann Autorität tragen? |
|---|---|---|---|
| `user_direct` | Beispielnutzer hat es direkt gesagt (Voice-Transkript) | `USER_DIRECT` | **Ja** |
| `home_assistant` | Ein HA-Tool lieferte einen Messwert/Zustand | `LOCAL_TRUSTED_TOOL` | Nein |
| `codex_result` | Codex-Agent lieferte eine Ausgabe | `AGENT_GENERATED` | Nein |
| `solvio_inference` | SOLVIO hat es selbst abgeleitet | `AGENT_GENERATED` | Nein |
| `system_observation` | SOLVIOs eigene Laufzeitbeobachtung (Metrik, Event) | `SYSTEM_TRUSTED` | Nein¹ |
| `gmail_message` | Inhalt aus einer E-Mail | `UNTRUSTED_EMAIL` | **Nie** |
| `web_page` | Inhalt aus dem Web | `UNTRUSTED_WEB` | **Nie** |

¹ `system_observation` ist vertrauenswürdig als Tatsache über SOLVIO selbst, autorisiert aber keine Nutzeraktionen.

**`provenance` als Kette:** Neben dem unmittelbaren `source_type` hält jeder Record eine `provenance`-Liste — die Ableitungskette. Ein `solvio_inference`-Record über „Beispielnutzer mag Espresso" zeigt in seiner Provenance auf die `episodic`/`user_direct`-Records, aus denen er abgeleitet wurde. So ist jederzeit auditierbar, ob ein „Fakt" letztlich auf einer Nutzeraussage oder auf einer unvertrauten Quelle beruht.

`get_provenance(id)` muss diese Kette vollständig zurückgeben. Der Prompt-Injection-Schutz (Benchmark Test 3 + Test 6) hängt daran: **Keine vom Agenten oder aus einer E-Mail abgeleitete Information darf je als „vom Nutzer gesagt" erscheinen.**

## 5. MemoryRecord

Neutrales, serialisierbares Record. Alle Felder aus der Spezifikation:

| Feld | Typ | Semantik |
|---|---|---|
| `id` | `str` | Stabile, eindeutige ID (Backend-vergeben, opak) |
| `memory_type` | `MemoryType` | Kategorie (§3) |
| `content` | `str` | Der Inhalt in natürlicher Sprache (sprachtauglich) |
| `subject` | `str` | Worüber/über wen — Schlüssel für „aktuelle Wahrheit" (z. B. `person:contact_example`, `pref:coffee`) |
| `source` | `str` | Konkrete Quelle (z. B. `voice:2026-08-18T15:00`, `gmail:msgid-abc`, `ha:media_player.wohnzimmer`) |
| `source_type` | `SourceType` | Herkunftsklasse (§4) |
| `created_at` | `datetime` | Wann gespeichert |
| `updated_at` | `datetime` | Letzte Änderung am Record |
| `valid_from` | `datetime \| None` | Ab wann der Fakt gilt (Realzeit, nicht Speicherzeit) |
| `valid_until` | `datetime \| None` | Bis wann gültig; `None` = offen |
| `confidence` | `float` | 0.0–1.0, wie sicher der Inhalt stimmt |
| `importance` | `float` | 0.0–1.0, wie wichtig fürs Behalten (steuert Konsolidierung/Vergessen) |
| `trust_level` | `TrustLevel` | Vertrauen in die **Herkunft** (§4, §5.1) |
| `sensitivity` | `Sensitivity` | Schutzbedarf des **Inhalts** (§5.1) — orthogonal zu `trust_level` |
| `retention_policy` | `RetentionPolicy` | Aufbewahrungs-/Löschregel (§5.2) |
| `provenance` | `list[ProvenanceEntry]` | Ableitungskette (§4) |
| `supersedes` | `str \| None` | ID des Records, den dieser ersetzt |
| `superseded_by` | `str \| None` | ID des Records, der diesen ersetzt hat (`None` = aktuell) |
| `tags` | `list[str]` | Freie Schlagworte |
| `relations` | `list[Relation]` | Typisierte Kanten zu anderen Records (`about`, `caused_by`, `contradicts`, `refines`) |
| `metadata` | `dict[str, Any]` | Backend-/erweiterungsspezifisch, ohne Contract-Bedeutung |

`ProvenanceEntry`: `{source_type, source, trust_level, at, note}`.
`Relation`: `{kind, target_id}`.

### 5.1 `trust_level` vs. `sensitivity` — zwei Achsen, nie vermischen

Das ist eine der wichtigsten Klarstellungen dieses Contracts:

- **`trust_level` = Vertrauen in die HERKUNFT.** Wer/was hat es gesagt? Darf diese Quelle eine Aktion autorisieren? (→ `TRUST_BOUNDARY.md`)
- **`sensitivity` = Schutzbedarf des INHALTS.** Wie geheim/persönlich ist die Aussage? Wer darf sie sehen, wohin darf sie fließen, darf sie geloggt werden?

Die beiden sind **unabhängig** und dürfen nicht in ein Feld gemischt werden. Beispiele für das Kreuzprodukt:

| Beispiel | `trust_level` | `sensitivity` |
|---|---|---|
| „Die Erde hat einen Mond" (aus dem Web) | `UNTRUSTED_WEB` (niedrig) | `PUBLIC` (niedrig) |
| „Beispielnutzer hat Diagnose Y" (Beispielnutzer sagt es selbst) | `USER_DIRECT` (hoch) | `SENSITIVE` (hoch) |
| „HA-Token liegt in Vault-Slot `ha_token`" | `LOCAL_TRUSTED_TOOL` | `SECRET_REFERENCE` |

**Sensitivity-Klassen (schlank, 4):**

| `sensitivity` | Bedeutung | Handhabung |
|---|---|---|
| `PUBLIC` | unkritisch | frei nutzbar/loggbar |
| `PERSONAL` | personenbezogen, alltäglich | nicht an Dritte/externe Ziele ohne Nutzerakt |
| `SENSITIVE` | besonders schützenswert (Gesundheit, Finanzen, Beziehungen) | minimaler Kontext, nie in Fremd-Egress, restriktives Logging |
| `SECRET_REFERENCE` | **verweist** auf ein extern verwaltetes Secret | `content` enthält **nur** die Referenz, nie das Secret; nie in Prompt/Log ausgeben |

**Secrets-Regel (verschärft):** `content` enthält **niemals** rohe Secrets (Keys, Passwörter, Tokens). Ein Secret wird ausschließlich als `SECRET_REFERENCE` modelliert, der auf ein extern verwaltetes Geheimnis zeigt (z. B. `.env`/Vault-Slot-Name). Durchsetzung liegt beim Trust-Layer; `sensitivity` steuert zusätzlich Egress und Logging.

### 5.2 `retention_policy`

Schlanke Aufbewahrungsregel pro Record. `RetentionPolicy`: `{mode, ttl_days, purge_on_expiry}` mit
`mode ∈ {default, audit_only, ttl}`.

- `default` — normale Behandlung; `forget`/`purge` nur auf Anweisung.
- `audit_only` — bereits „vergessen" für aktiven Recall, aber als Audit-Historie aufbewahrt (typisch nach `forget()`).
- `ttl` — läuft nach `ttl_days` ab; bei `purge_on_expiry = True` wird bei Ablauf **hart gepurged** statt nur vergessen.

## 6. Zeitmodell — aktuelle Wahrheit vs. Historie

Der zentrale Fehler, den ein Gedächtnis machen kann, ist **zwei widersprüchliche Fakten gleichwertig gleichzeitig zu behaupten**.

Beispiel:
- `01.01.`: „Beispielnutzer trinkt Espresso." → Record A
- `01.07.`: „Beispielnutzer bevorzugt jetzt Cappuccino." → Record B, `B.supersedes = A`, `A.superseded_by = B`

**Aktuelle Wahrheit** = Menge der Records mit
`superseded_by is None` **und** `valid_from <= now` **und** (`valid_until is None` oder `now < valid_until`).

Auf „Was trinkt Beispielnutzer aktuell?" → **Cappuccino** (Record B).
Auf „Was trank Beispielnutzer früher?" → **Espresso** (Record A, via `history()`).

`recall()` liefert **nur** aktuelle Wahrheit. Historie ist nur über `history()`/`search(include_superseded=True)` erreichbar. Nie vermischt.

## 7. Operationen

Alle async. `MemoryStore(Protocol)` in `src/solvio/contracts/memory.py`.

| Operation | Zweck | Semantik / Vor- & Nachbedingungen |
|---|---|---|
| `remember(record)` | Neue Erinnerung speichern | Legt neuen Record an, vergibt `id`. Ändert nichts Bestehendes. |
| `get(id)` | Exakter Abruf per ID | Genau ein Record oder `None`. Keine Interpretation. |
| `recall(query, ...)` | Antwort-Pfad: aktuelle Wahrheit zu einer Frage | Liefert **nur** aktuelle, nicht-supersedierte, nicht-vergessene Records. Findet nichts → leere Liste (nie geraten). |
| `search(query, ...)` | Breite Suche, gerankt | Optional `include_superseded`. Für Recherche/Exploration, nicht für Faktenabruf. Liefert **nie** gepurgte Records. |
| `update(id, changes)` | Denselben Fakt am selben Record ändern | Korrektur/Anreicherung ohne Historienbruch. `updated_at` steigt. **Kein** neuer Record. |
| `supersede(old_id, new_record)` | Fakt ändert sich, alter bleibt historisch | Legt `new_record` an, setzt `supersedes`/`superseded_by`. Alter Record bleibt abrufbar. |
| `forget(id, reason)` | Aus aktivem Recall entfernen/deaktivieren | Mutierend. Deaktiviert für `recall`/`search`; Audit-/Historien-Info **darf** gemäß `retention_policy` bestehen bleiben. Reversibel (Soft). |
| `purge(id, reason)` | **Endgültige** personenbezogene Löschung | Mutierend, **irreversibel**. Entfernt Inhalt **und** semantischen Index/Embeddings/abgeleitete Suchrepräsentationen, soweit technisch kontrollierbar. Hinterlässt einen inhaltslosen Tombstone (§7.1). |
| `history(subject)` | Vollständige Zeitreihe zu einem Subjekt | Alle Versionen inkl. supersedierter/vergessener, chronologisch. Gepurgte erscheinen **nicht** (nur als inhaltsloser Tombstone). |
| `consolidate(scope)` | „Dreaming" — zusammenführen ohne Herkunftsverlust | Siehe §8. Muss Tombstones respektieren. |
| `list_related(id)` | Verknüpfte Records | Folgt `relations`-Kanten. Keine gepurgten. |
| `get_provenance(id)` | Herkunftskette abrufen | Vollständige `provenance` + supersession-Kette. Basis für Trust-Audit. |
| `list_tombstones()` | Purge-Registry lesen | Liste **inhaltsloser** Tombstones (Hash + Zeit + Grund). Für purge-aware Restore. |

**Klare Trennung der mutierenden Operationen:**
- `update` = *derselbe Fakt*, nur präziser („Beispielkontakt ist Steuerberater" → „…bei KPMG"). Keine Historie nötig.
- `supersede` = *der Fakt hat sich geändert* („Espresso" → „Cappuccino"). Historie bleibt erhalten.
- `forget` = *aus dem aktiven Gedächtnis nehmen*. `recall`/`search` finden es nicht mehr; Audit-Historie **darf** bleiben (Policy). Reversibel.
- `purge` = *endgültig löschen*, inkl. Index/Embeddings. Darf durch **nichts** (Dreaming, Restore, Neustart) wieder aktiv werden. Irreversibel.

### 7.1 `purge()`, Tombstones, Managed Restore und Backup-Retention

`purge()` ist die datenschutzkritische Operation. Ihre Garantie ist bewusst **präzise** formuliert und verspricht **nichts technisch Unmögliches** über beliebige historische Offline-Backups.

**Was `purge()` garantiert:**
- entfernt personenbezogenen Inhalt aus dem **aktiven** Memory-Backend,
- entfernt die **kontrollierbaren** semantischen/Vektor-Indizes,
- entfernt die **kontrollierbaren** abgeleiteten Retrieval-Repräsentationen,
- verhindert Wiederverwendung durch normalen `recall`/`consolidate` (Dreaming),
- erzeugt einen **inhaltslosen** Purge-Tombstone `{subject_hash, purged_at, reason}` (Einweg-Hash von `id`/`subject`, keine personenbezogenen Daten).

**Managed Restore (Pflicht):** Ein von SOLVIO **kontrollierter** Restore MUSS die Purge-Tombstones (`list_tombstones()`) beachten und darf gepurgte Daten **nicht** wieder aktivieren. Nach einem Managed Restore darf ein gepurgter Fakt **nie** wieder als aktive Erinnerung erscheinen.

**Historische / Offline / Immutable Backups:** Bereits existierende Offline- oder unveränderliche Backups können physisch **noch** Daten enthalten, bis ihre Retention/Expiry abläuft. Das ist eine Eigenschaft von Backups, keine Schwäche von `purge()`:

> **active purge != guaranteed physical erasure from every historical backup**

**Backup-Retention (sauber definiert):**
- **Managed Backups** (von SOLVIO verwaltet) tragen eine **begrenzte Retention** und werden beim Restore **tombstone-aware** eingespielt → gepurgte Daten werden nie reaktiviert und altern innerhalb des Retention-Fensters physisch aus.
- **Offline/Immutable Snapshots** unterliegen ihrer **eigenen** Retention/Expiry; ihre physische Löschung ist nicht Teil der `purge()`-Laufzeitgarantie, sondern der Backup-Retention-Policy. Empfehlung: Managed-Backup-Retention bewusst begrenzen, damit gepurgte Inhalte planbar physisch auslaufen.

**Dreaming respektiert Tombstones:** `consolidate()` darf einen gepurgten Fakt nicht wiedererzeugen.

**Capability Gap:** Unterstützt ein Backend keinen tombstone-aware **Managed Restore**, wird das **klar als Capability Gap dokumentiert** (Benchmark Test 9 Teil C) — kein stiller Durchlauf; blockiert die Memory-Akzeptanz.

Dies ist **Contract**, keine Backend-Implementierung.

## 8. Konsolidierung / „Dreaming" — Regeln und Grenzen

Konsolidierung darf Dubletten zusammenführen, alte Episoden verdichten und Muster erkennen. Sie unterliegt aber **harten Leitplanken**:

**Erlaubt:**
- Exakte/nahe Dubletten zu einem Record zusammenführen (Provenance beider bleibt in der Kette).
- Alte, unwichtige `EPISODIC`-Records verdichten (Zusammenfassung + Originale referenziert).
- Aus mehreren Beobachtungen einen `SEMANTIC`/`solvio_inference`-Record ableiten — **markiert als Inferenz**, mit Provenance auf die Quellen.

**Verboten (HARD FAIL im Benchmark, Test 4 / Test 9):**
- Aus einer Vermutung einen dauerhaften **persönlichen Fakt** (`USER`/`PREFERENCE`) erzeugen, der als `user_direct` erscheint.
- Provenance kappen oder Trust-Level „hochstufen".
- Aktuelle Wahrheit verändern, ohne sauberes `supersede`.
- Einen **gepurgten** Fakt wiedererzeugen (Tombstones sind zu respektieren).

**Pflichten:**
- Jede Konsolidierung ist **umkehrbar** (Rollback). Test 4 und Test 8 verlangen das ausdrücklich.
- Die *abgeleitete* Präferenz trägt `source_type = solvio_inference`, `trust_level = AGENT_GENERATED` — nie `user_direct`.

## 9. Trust-Integration

Jeder Record trägt `trust_level` **und** `sensitivity`. Die Kernregel des [`TRUST_BOUNDARY.md`](TRUST_BOUNDARY.md) gilt im Gedächtnis unmittelbar:

> **Unvertrauter Inhalt kann Information liefern, aber niemals Autorität.**

Konsequenzen fürs Memory:
- Eine E-Mail (`UNTRUSTED_EMAIL`) darf einen `RULE`- oder `STANDING_INTENT`-Record **nicht** selbst erzeugen. Nur eine echte `user_direct`-Entscheidung kann das.
- `recall()`-Ergebnisse, die in einen Aktions-Prompt fließen, müssen `trust_level` **und** `sensitivity` mitführen, damit die Deep-Runtime privilegierte Aktionen aus unvertrauten Fakten blockiert und `SENSITIVE`/`SECRET_REFERENCE`-Inhalte nicht in Fremd-Egress geraten.
- `forget()`/`purge()` auf Zuruf einer E-Mail sind ausgeschlossen — beide sind mutierend und brauchen `USER_DIRECT` + Bestätigung.

## 10. Backend-Neutralität

Ein Backend (OpenClaw-Adapter, native Implementierung, Letta-Adapter) erfüllt diesen Contract, wenn es:
1. `MemoryStore(Protocol)` vollständig implementiert,
2. das Zeitmodell (§6) korrekt umsetzt (aktuelle Wahrheit ≠ Historie),
3. `provenance`, `trust_level` und `sensitivity` verlustfrei speichert und über `get_provenance()` zurückgibt,
4. Konsolidierung mit Rollback (§8) unterstützt,
5. `forget()`/`purge()` mit Tombstone- und purge-aware-Restore-Semantik (§7.1) umsetzt,
6. Managed Backup/Restore verlustfrei **und** tombstone-respektierend beim Restore umsetzt (Benchmark Test 8 + Test 9); `purge()` verspricht dabei keine physische Löschung aus beliebigen historischen Offline-Backups (§7.1).

Der Core importiert **immer nur** `solvio.contracts.memory.MemoryStore`, nie ein konkretes Backend direkt. Backend-Auswahl passiert an genau einer Stelle (späterer `build_memory(settings)`-Factory analog zu `build_dispatcher`).

## 11. Akzeptanz

Ein Memory-Backend gilt erst dann als geeignet, wenn die Benchmark-Tests **1–4, 6, 8 und 9** bestehen (siehe [`../benchmarks/OPENCLAW_ACCEPTANCE_TESTS.md`](../benchmarks/OPENCLAW_ACCEPTANCE_TESTS.md), Decision Matrix). Recall, Temporal, Provenance, Dreaming, Prompt-Injection, Backup und Forget/Purge sind nicht verhandelbar.

## 12. Nicht-Ziele (bewusst offen gelassen)

- Konkrete Speicher-Technologie (Vektor-DB, SQLite, Graph) — Backend-Sache.
- Embedding-/Ranking-Modell — Backend-Sache.
- Konkreter Lösch-Mechanismus für Embeddings/Index — Backend-Sache (nur das *Ergebnis* ist Contract).
- Ob Konsolidierung nachts/im Hintergrund läuft — Ablauf-Sache der Deep-Runtime.
- Multi-User / Speaker-abhängige Sichten — späterer Ausbau, aber `subject`/`trust_level`/`sensitivity` sind dafür vorbereitet.

---

# V2 — Adaptive Memory (2026-08-26)

Alle Zusagen von V1 gelten unverändert weiter. Was folgt, ist **additiv**:
`memory.sqlite3` hat kein neues Feld, keine Umdeutung, keinen Backfill.
Begründung und verworfene Alternativen:
[ADR-0021](../decisions/ADR-0021-lernen-erreicht-nie-user-direct.md).

## 13. Der Kandidatenraum

Ein **Kandidat** ist eine maschinell vorgeschlagene, noch nicht adoptierte
Aussage. Kandidaten sind **kein Gedächtnis**: `recall()`, `search()`, `get()`,
`history()`, `active_records()` und jede abgeleitete Sicht (Index, Compiler,
Projektion) geben niemals Kandidaten zurück. Sie liegen in einem eigenen
Speicher (`candidates.sqlite3`) mit eigener Löschsemantik — hartes Löschen ist
erlaubt, die Purge-/Tombstone-Garantien aus §7.1 gelten für sie **nicht**, weil
sie nie Wahrheit waren.

Sechs Zustände: `GATHERING`, `ASK_PENDING`, `CONTESTED`, `ADOPTED`, `DECLINED`,
`EXPIRED`. Die letzten drei sind terminal; es gibt keinen Wiedereintritt.

## 14. Herkunfts-Exklusivität — die Kerninvariante

> Records mit `source_type=user_direct` entstehen ausschließlich aus einem
> **turn-gebundenen Nutzerakt**: dem Merk-Mandat, einer Korrektur oder einer
> per Freigabeweg autorisierten Geräteentscheidung.

> **Kein automatischer Übergang erzeugt, verändert, supersediert, vergisst oder
> purged je einen `user_direct`-Record.** Automatik schreibt ausschließlich
> `source_type=solvio_inference` mit `trust_level=AGENT_GENERATED` — auch dann,
> wenn sämtliche Evidenz aus `user_direct`-Äußerungen besteht (§7-Taintregel
> aus `TRUST_BOUNDARY.md`: ein Agenten-Durchlauf hebt nicht auf).

Automatik darf genau zwei Dinge: einen Kandidaten gemäß deterministischer,
auditierbarer Policy als `solvio_inference`-Record adoptieren — und einen
`solvio_inference`-Record durch einen neueren ablösen, wenn eine direkte
Nutzeraussage ihm widerspricht.

## 15. Quellen-Gate

Ein Kandidat entsteht ausschließlich aus dem **finalisierten Turn des
authentifizierten Besitzers** über einen zugelassenen Kanal. Assistententext,
Werkzeugergebnisse, E-Mail-, Web- und Dokumentinhalte sowie Ausgaben anderer
Agenten sind **nicht gefiltert, sondern nicht verdrahtet** — es gibt keinen
Code-Pfad von ihnen zu einem Kandidaten.

## 16. Sensitivität und Geheimnisse

Aussagen der Klasse `SENSITIVE` werden **nie automatisch adoptiert**; sie
erzeugen höchstens eine Rückfrage. Inhalte in Zugangsdaten-Gestalt erzeugen
**nicht einmal einen Kandidaten**. Die Einstufung eines Modells kann eine
Einstufung nur **verschärfen, nie senken**; maßgeblich ist das Maximum aus
deterministischem Scan, Core-Lexikon und Modell-Label.

`SECRET_REFERENCE` entsteht adaptiv nie.

## 17. Evidenz = Provenienz

Wiederholte Beobachtung desselben Sachverhalts erzeugt **keinen zweiten
Record**, sondern verlängert die Provenienzkette des bestehenden.
Provenienzeinträge enthalten Bezeichner und normalisierte Kurzformen
(≤160 Zeichen) — **niemals Transkriptkörper, niemals Audio, niemals
Modell-Gedankengänge**. Zeitstempel, Kennungen und Herkunft stempelt der Core;
ein Modell kann keinen Provenienzeintrag formulieren.

**Unabhängigkeit** einer Beobachtung verlangt andere `conversation_id` **und**
anderen Tag. Wiederholung innerhalb einer Sitzung ist eine Beobachtung, nicht
mehrere — sonst ließe sich Gewissheit durch bloßes Wiederholen herstellen.

## 18. Neue Operation: `reinforce(id, entry)`

Append-only Provenienzeintrag plus `updated_at`. Ändert weder Inhalt noch
Herkunftsklasse noch Vertrauensstufe. **Nur gegen `solvio_inference`** — eine
Implementierung MUSS sonst werfen, nicht still nichts tun.

Warum es sie gibt: `update()` lässt die Provenienz per Feld-Whitelist bewusst
nicht zu, und `supersede()` für bloße Verstärkung würde die Historie fluten.

Das Protokoll hat damit **14** Operationen.

## 19. Unterdrückung

Ein abgelehnter Vorschlag und ein vergessenes gelerntes Wissen hinterlassen
eine **Unterdrückung** auf ihrem Dedup-Schlüssel. Sie wird geprüft, **bevor**
ein Kandidat entsteht. Eine Ablehnung, die beim nächsten Vorkommen erneut
gefragt wird, verletzt diesen Contract.

## 20. `confidence` — präzisiert, nicht geändert

Eine grobe **Ordinalprojektion** der Evidenzlage, keine Wahrscheinlichkeit:
`1.0` explizit/bestätigt, `0.75` adoptiert aus eigener Aussage, `0.6` adoptiert
aus Inferenz. Sie ändert sich nur durch Ereignisse, **nie durch Zeitablauf**.
Es gibt keinen numerischen Zerfall — erfundener Zweifel ist so unehrlich wie
erfundene Gewissheit. Maßgeblich ist die Provenienzkette.

## 21. Transparenzpflicht

Auf „Woher weißt du das?" muss die Antwort aus **gespeicherter Provenienz**
ableitbar sein: „ausdrücklich gesagt", „aus n Beobachtungen abgeleitet", „da
bin ich nicht sicher". Ein offener Kandidat wird **nie** als Wissen
präsentiert. Erfundene Herkunftsauskünfte sind ein Contract-Bruch.

## 22. Vergessens-Durchgriff in Ansichten

`forget()`/`purge()` müssen abgeleitete menschenlesbare Ansichten erreichen.
Das Vault-Archiv hält für entfernte Records **keinen Inhalt** vor — auch keinen
Titel und keinen sprechenden Dateinamen. Es bleibt die Kennung, ein Zeitpunkt
und ein Satz darüber, dass hier etwas war. Historie ist Sache des Cores
(`history()`, Ledger), nicht kopierbarer Dateien.

## 23. Aktuelle Wahrheit hat genau eine Definition

`active_records()` und `active_ids()` wenden dasselbe Zeitfenster an wie
`recall()` (§6). Zwei Sichten auf „aktuelle Wahrheit" sind eine zu viel — die
Abweichung war DEBT-0093 und ist geschlossen.

## 24. Mutierende Operationen am Gerät

`forget`, `supersede`/`correct`, `purge` sowie das Bestätigen und Ablehnen
eines Kandidaten sind vom iPhone aus **einzeln** auszuführen — seit
Approval Policy V2 + SOLVIO Presence V2 über den attestierten Schreibweg
`/v1/memory/mutation`: je Mutation eine App-Attest-Assertion über das
kanonische Binding (inklusive `payload_sha256`), und jede Mutation läuft
durch den CapabilityRouter als `TRUSTED_INTERACTIVE_APP`, wo die Matrix aus
Herkunft und Aktionsklasse entscheidet. Was die Matrix als
`CRITICAL`/`VERY_CRITICAL` einstuft, kostet weiterhin Face ID. Es gibt
keinen Schreibweg am Gateway oder an der Matrix vorbei und keine
gebündelte Sitzung. Details: `docs/releases/SOLVIO_PRESENCE_IPHONE_EXPERIENCE_V2.md`.
