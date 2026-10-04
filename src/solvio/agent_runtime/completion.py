"""Ist das Owner-Ziel erfuellt? In V1A fast nie — und das ist die Antwort.

## Wie dieses Modul dreimal falsch war

**Erste Fassung.** Antwortlaenge, geforderte Quellenzahl und eine Liste von
Wirkungsverben. Gemessen am 5.9.2026: ein absichtlich unpassender Text von 40
Zeichen erfuellte zwei echte Owner-Ziele.

**Zweite Fassung (FIX 1).** Statt einer Verbliste eine „Abdeckungsregel": das
Ziel in Teilforderungen zerlegen und jede wiedererkennen. Ich hielt sie fuer
fail-closed. Sie war es nicht. Der Chief Architect brauchte drei Saetze:

* „Finde mir … einen Termin beim Reifenhändler, kümmere dich darum." — ein
  KOMMA statt „und", also ein einziges Stueck, in dem irgendwo `Finde` steht;
* „Finde mir … einen Termin beim Reifenhändler." — ein Satz, ein Worttreffer;
* „Wie teuer sind die beiden Angebote? Vergleiche sie anhand ihrer Quellen." —
  das Wort `Quellen` erklaerte die VERGLEICHSforderung fuer verstanden.

**Die Lehre ist nicht „das Trennzeichen fehlte".** Jede dieser Regeln stellt
dieselbe Frage — kommt ein bestimmtes Wort vor? — und ein Wort im ZIEL sagt
nichts ueber das ERGEBNIS. Auch die syntaktische Gestalt einer URL beweist
keine Relevanz. Die Gattung traegt nicht, und eine vierte Fassung derselben
Gattung waere nur die naechste, die jemand mit einem Komma aushebelt.

## Vierte Fassung: ein Informationsvertrag, aber ohne Sprachanalyse im Core

Die dritte Fassung (unten) war sicher und zu streng: sie ersetzte die
Nichtanerkennung FALSCHER Ergebnisse durch die Nichtanerkennung ALLER. Dazu kam
eine Owner-Grenze, die ein Urteil erbat und keines verarbeiten konnte.

Jetzt gibt es einen zweiten Vertrag — den **Informationsvertrag**. Er beruht
nicht auf Worten im Ziel, sondern auf drei getrennten Dingen:

1. **Gebundene Anforderungen** (`requirements.py`): die Auslegung des
   Auftragstexts, einmal gebunden, vom Core an den Wortlaut gedigestet.
2. **Ein unveraenderlicher Ergebnis-Snapshot**: genau das Material, ueber das
   geurteilt wurde, mit eigenem sha256.
3. **Ein Bewertungsurteil**: der Vorschlag eines Modells, welche Anforderung
   durch WELCHE Zeile des Snapshots gedeckt ist — und ob am Originaltext
   gemessen etwas fehlt.

Der Core prueft alles Nachrechenbare selbst: Kennungen, Disjunktheit,
Aufloesbarkeit jeder Referenz gegen den Snapshot, Belegzahl, Snapshot-Aktualitaet,
Unsicherheiten, offene Handlungen und die Ausfuehrungslage. Das Modell liefert
Inhaltsbewertung — Information, keine Freigabe.

**Was das ausdruecklich nicht ist:** ein Beweis. Die Bewertung kann irren. Sie
ist gegen ERFINDUNG abgesichert (jede Referenz muss im Snapshot stehen) und
gegen AUTORITAETSGEWINN (offene Handlungen und die Ausfuehrungswahrheit
blockieren unabhaengig davon), nicht gegen ein Fehlurteil ueber Inhalte.

## Dritte Fassung: gar keine Sprachanalyse mehr

Erfuellung wird in V1A nur noch dort behauptet, wo der Core sie **selbst
hergestellt und geprueft** hat. Das ist genau ein Fall:

> **`build`-Scope mit geerntetem Arbeitsergebnis.** Der Core hat die
> Arbeitskopie angelegt, der Builder hat darin commitet, die Ernte hat den Baum
> geprueft und den Zweig geholt. `branch_ref` benennt ihn. Kein Text, keine
> Behauptung eines Executors — ein Ding, das da ist.

Fuer jedes natuerlichsprachliche Rechercheziel lautet die Antwort
`no_supported_fulfilment_contract`. Das heisst ausdruecklich **nicht**
„gescheitert": das Rechercheergebnis bleibt ein verfuegbares Ergebnis, es wird
aufbewahrt, gemeldet und abgelegt. Nur die Aussage „dein Ziel ist damit
erledigt" faellt weg, weil dieser Code sie nicht belegen kann.

Was hier bewusst NICHT gebaut wird: ein Modell, das sein eigenes Ergebnis
abnimmt. Ein Modell darf Kriterien oder eine Bewertung VORSCHLAGEN — es darf
sich daraus keine Handlungsrechte ableiten, und ein allgemeiner
Wahrheitsbeweis natuerlicher Sprache wird hier nicht verlangt.

## Warum das Modul trotzdem bleibt

Weil es die EINE Stelle ist, an der diese Entscheidung faellt. Die vorzeitige
Vollendung und der Abschluss am Planende fragen dieselbe Funktion — nicht
dieselbe Regel zweimal geschrieben. Genau daran ist FIX 1 gescheitert: die
Pruefung hing an der Abkuerzung, und der regulaere Weg ans Planende hatte
keine.
"""
from __future__ import annotations

from dataclasses import dataclass

from solvio.agent_runtime.store import SCOPE_BUILD

#: Die Erfuellungsvertraege, die V1A kennt. Wer einen dritten aufnimmt, trifft
#: eine Architekturentscheidung und merkt es an dieser Zeile.
BUILD_WORK_PRODUCT = "build_work_product"
INFORMATION = "information_request"
CONTRACTS = frozenset({BUILD_WORK_PRODUCT, INFORMATION})

#: Die Gruende, bei denen eine ZUORDNUNGS-Nachfrage zulaessig ist (DEBT-0232).
#:
#: Gemeinsam ist ihnen: das Urteil ist formal gueltig, und es sagt inhaltlich
#: nichts Falsches — nur die Zuordnung von Anforderung zu Beleg traegt nicht.
#: Genau daran scheiterte im echten Modelllauf eine inhaltlich richtige
#: Antwort: einmal, weil der Beleg in Anfuehrungszeichen stand, einmal, weil
#: nur die Befundzeile und keine Quelle genannt war.
#:
#: **Was hier ausdruecklich NICHT steht, ist die eigentliche Zusage.**
#: `requirements_incomplete`, `assessment_uncertain`, `requirement_not_answered`,
#: `further_work_required`, `open_external_action` und `requirement_unclear`
#: sind INHALTLICHE Aussagen des Modells — eine Luecke oder eine Unsicherheit.
#: Sie ein zweites Mal zu fragen hiesse, auf ein anderes Urteil zu hoffen, und
#: das waere keine Reparatur, sondern ein zweiter Versuch. Ebensowenig stehen
#: die `verdict_*`-Gruende hier: ein Urteil, das zur falschen Aufgabe oder zum
#: falschen Snapshot gehoert, ist nicht schlecht zugeordnet, sondern fremd.
ATTRIBUTION_REASONS = frozenset({
    "not_enough_sources",
    "requirement_without_evidence",
    "evidence_not_in_snapshot",
})


def _json_unescaped(text: str) -> str | None:
    """Ein Beleg in JSON-Escape-Form (`\"`, `\\`) — sonst None."""
    if "\\" not in text:
        return None
    return text.replace('\\"', '"').replace("\\\\", "\\")


def attribution_only(verdict: "Verdict", judgement: object) -> bool:
    """Traegt der Fehlschlag AUSSCHLIESSLICH an der Belegzuordnung?

    **Der Grund allein beweist das nicht**, und daran ist die erste Fassung
    gescheitert. `information()` prueft die Zuordnung VOR
    `weiterarbeit_noetig`; ein Urteil, das beides sagt — schlecht zugeordnet
    UND „da fehlt noch Arbeit" —, kommt deshalb als `not_enough_sources`
    zurueck. Wer nur den Grund ansieht, haelt eine ausdrueckliche
    Weiterarbeitsmeldung fuer einen Formfehler und fragt nach. Gemessen: der
    Lauf endete danach SUCCEEDED, obwohl das Modell gesagt hatte, dass Arbeit
    fehlt.

    Der zurueckgegebene Grund ist der ERSTE, der greift, nicht der einzige.
    Gefragt wird hier deshalb am URTEIL selbst, und zwar positiv: sagt es
    inhaltlich ueberhaupt nichts, was gegen einen Abschluss spricht?

    Jede inhaltliche Aussage des Modells sperrt die Nachfrage — `offen`,
    `fehlend`, `unsicher` und `weiterarbeit_noetig`. Die drei Listen liegen
    zwar frueher in der Pruefreihenfolge und wuerden heute ohnehin gewinnen;
    sie stehen hier trotzdem, weil diese Zusage nicht von einer Reihenfolge
    abhaengen soll, die jemand spaeter umstellt.
    """
    from solvio.agent_runtime import requirements as RQ

    if verdict.satisfied or verdict.reason not in ATTRIBUTION_REASONS:
        return False
    try:
        geprueft = RQ.validate_judgement(judgement)
    except RQ.JudgementInvalid:
        # Ein ungueltiges Urteil ist kein Zuordnungsfehler, sondern ein
        # Formfehler — dafuer gibt es die andere Nachfrage.
        return False
    if geprueft["weiterarbeit_noetig"]:
        return False
    return not any(geprueft[feld] for feld in RQ.JUDGEMENT_LISTS)


@dataclass(frozen=True)
class Verdict:
    """Das Urteil. `reason` ist eine geschlossene Vokabel, kein Satz."""

    satisfied: bool
    reason: str
    #: Der Vertrag, unter dem die Erfuellung belegt ist. Leer bei `satisfied=False`.
    contract: str = ""
    #: Die Evidence, auf die sich der Vertrag stuetzt — beim Bau der
    #: Zweigverweis, beim Informationsvertrag der Snapshot-Hash.
    evidence: str = ""
    #: Was noch fehlt. Fuer die Meldung an den Menschen, nicht fuer die Logik.
    open_points: tuple = ()


def information(*, bound: dict, judgement: dict, snapshot: dict,
                snapshot_digest: str, requirements_digest: str,
                task_id: str, run_id: str,
                verified_effects: dict | None = None,
                file_evidence: frozenset | set | None = None) -> Verdict:
    """Der Informationsvertrag. **Jede Bedingung hier ist nachrechenbar.**

    Das Modell hat vorgeschlagen; ab hier wird nur noch geprueft. Faellt eine
    einzige Bedingung, gibt es keinen automatischen Abschluss — und der Grund
    steht in der geschlossenen Vokabel, damit die Meldung ihn nennen kann.
    """
    from solvio.agent_runtime import requirements as RQ

    # Beleg → die Anforderung, FUER die der Core ihn gemessen hat. Eine flache
    # Menge genuegte nicht: sie machte jeden Beleg fuer jede Handlung
    # verwendbar, und ein Modell haette mit dem Beleg der einen Handlung die
    # andere abhaken koennen.
    belege_je_handlung = dict(verified_effects or {})

    if not isinstance(judgement, dict) or judgement.get("v") != RQ.VERSION:
        return Verdict(False, "verdict_unreadable")
    # **Vollstaendig, bevor irgendetwas zaehlt.** Dieselbe Pruefung fuer ein
    # frisch gelesenes und ein persistiertes Urteil — ein Satz ist nicht
    # dadurch gueltig, dass er in der Datenbank steht.
    try:
        geprueft = RQ.validate_judgement(judgement)
    except RQ.JudgementInvalid:
        return Verdict(False, "verdict_unreadable")
    # 1) Die Bindung. Ein Urteil gehoert zu GENAU dieser Aufgabe, diesem Lauf,
    #    diesem Anforderungssatz und diesem Snapshot — sonst zu gar nichts.
    if judgement.get("task_id") != task_id or judgement.get("run_id") != run_id:
        return Verdict(False, "verdict_foreign_run")
    if judgement.get("anforderungen_digest") != requirements_digest:
        return Verdict(False, "verdict_stale_requirements")
    if judgement.get("snapshot") != snapshot_digest:
        return Verdict(False, "verdict_stale_snapshot")

    # 2) Die Abdeckungspruefung am ORIGINALTEXT. Sie ist der Grund, warum eine
    #    ausgelassene Buchung nicht durchrutscht: die erste Auslegung kann
    #    unvollstaendig gewesen sein, und dann sagt das hier — nicht die leere
    #    Handlungsliste.
    fehlend = geprueft["fehlend"]
    unsicher = geprueft["unsicher"]
    if fehlend:
        return Verdict(False, "requirements_incomplete",
                       open_points=tuple(fehlend[:RQ.MAX_ITEMS]))
    if unsicher:
        return Verdict(False, "assessment_uncertain",
                       open_points=tuple(unsicher[:RQ.MAX_ITEMS]))

    # 3) Ungeklaerte Forderungen. Handlungen kommen weiter unten — sie
    #    brauchen die Zuordnung, die erst in Schritt 4 entsteht.
    if bound[RQ.UNCLEAR]:
        return Verdict(False, "requirement_unclear",
                       open_points=tuple(e["text"] for e in bound[RQ.UNCLEAR]))

    # 4) Die Zuordnung Anforderung → Belege. Eine globale Liste genuegt nicht:
    #    sie sagt nicht, WELCHE Forderung wodurch gedeckt ist.
    gedeckt = geprueft["beantwortet"]
    offen = set(geprueft["offen"])
    bekannt = RQ.requirement_ids(bound)
    zuordnung: dict[str, list[str]] = {}
    for eintrag in gedeckt:
        kennung = eintrag["id"]
        if kennung not in bekannt:
            # Eine Kennung, die es nicht gibt. Das Urteil darf Forderungen
            # weder erfinden noch umbenennen.
            return Verdict(False, "verdict_unknown_requirement")
        if kennung in zuordnung:
            return Verdict(False, "verdict_duplicate_requirement")
        zuordnung[kennung] = eintrag["belege"]
    for kennung in offen:
        if kennung not in bekannt:
            return Verdict(False, "verdict_unknown_requirement")
    if offen & set(zuordnung):
        return Verdict(False, "verdict_contradicts_itself")

    # 5) Vollstaendigkeit: JEDE Auskunftsforderung muss gedeckt sein.
    noch_offen = [e["text"] for e in bound[RQ.ASK] if e["id"] not in zuordnung]
    if noch_offen:
        return Verdict(False, "requirement_not_answered",
                       open_points=tuple(noch_offen[:RQ.MAX_ITEMS]))
    if offen:
        return Verdict(False, "requirement_not_answered")

    # 5b) **Handlungen: gedeckt NUR durch einen vom CORE verifizierten
    #     Ausfuehrungsbeleg.**
    #
    #     Bis hierher scheiterte jeder Auftrag mit einer Handlung sofort an
    #     `open_external_action` — sicher, aber auch dann, wenn die Handlung
    #     tatsaechlich ausgefuehrt und nachgemessen war. Gemessen an einem
    #     echten Lauf: die Notiz stand in der Datei, und SOLVIO konnte den
    #     Auftrag trotzdem nicht als erfuellt melden.
    #
    #     Die Schranke faellt NICHT. Sie bekommt genau eine Ausnahme, und die
    #     Beweislast liegt beim Core, nicht beim Modell:
    #
    #     * Wird eine Handlung gar nicht zugeordnet, bleibt es bei
    #       `open_external_action` — unveraendert.
    #     * Wird sie zugeordnet, aber nur mit einer Rechercheszeile belegt,
    #       ist das `action_not_verified`. Ein Modell kann eine Handlung nicht
    #       herbeireden.
    #     * Nur ein Beleg aus `verified_effects` traegt. Die stellt der Core
    #       aus dem zusammen, was er SELBST nachgelesen hat — nie das Modell.
    #     * Und der Beleg muss zu GENAU DIESER Handlung gehoeren. Er traegt die
    #       Anforderungskennung, die der Plan dem ausfuehrenden Schritt gab.
    #       Ohne diese Bindung deckte bei zwei Handlungen der eine gemessene
    #       Effekt auch die zweite, ungetane — das Modell muesste nur beide
    #       Male denselben Beleg nennen.
    #
    #     Ein gemischter Auftrag gelingt damit erst, wenn JEDE Handlung so
    #     belegt ist; eine einzige offene laesst ihn scheitern.
    #     * Eine DATEI-Handlung braucht unter ihren Belegen den Dateibeleg des
    #       Core (`file_evidence`): seit ein Beobachtungsbeleg auch fuer ein
    #       Dateikriterium im Katalog steht (Bewerter verlangte fuer gebuendelte
    #       Pruefpflichten einen zugeordneten Ausfuehrungsbeleg — Anlaeufe p/q/v/w),
    #       darf eine beobachtete Ausfuehrung allein nie eine Datei beweisen.
    for eintrag in bound[RQ.ACTION]:
        kennung = eintrag["id"]
        if kennung not in zuordnung:
            return Verdict(False, "open_external_action",
                           open_points=(eintrag["text"],))
        if not any(belege_je_handlung.get(beleg) == kennung
                   for beleg in zuordnung[kennung]):
            return Verdict(False, "action_not_verified",
                           open_points=(eintrag["text"],))
        if (eintrag.get("effect") == "file" and file_evidence is not None
                and not any(beleg in file_evidence and belege_je_handlung.get(beleg) == kennung
                            for beleg in zuordnung[kennung])):
            return Verdict(False, "action_not_verified",
                           open_points=(eintrag["text"],))

    # 6) Jede Referenz muss im SNAPSHOT stehen — nicht im fluechtigen Kontext.
    #    Woertlich; die eine zugelassene Abweichung ist die JSON-Escape-Form
    #    (`\"` fuer `"`, `\\` fuer `\`), in der ein Modell einen zitierten
    #    Beleg aus dem JSON-Material zurueckgibt (gemessen 19.09.2026, Anlauf r:
    #    ein inhaltlich vollstaendiges Urteil fiel an genau zwei Backslashes).
    #    Das ist Zitierform, keine andere Aussage — ein erfundener Beleg bleibt
    #    ein erfundener Beleg.
    vorhanden = RQ.snapshot_references(snapshot)
    verwendet: set[str] = set()
    for kennung, belege in zuordnung.items():
        if not belege:
            return Verdict(False, "requirement_without_evidence")
        for beleg in belege:
            if beleg in vorhanden:
                verwendet.add(beleg)
                continue
            entschluesselt = _json_unescaped(beleg)
            if entschluesselt is None or entschluesselt not in vorhanden:
                return Verdict(False, "evidence_not_in_snapshot")
            verwendet.add(entschluesselt)

    # 7) Die geforderte Belegzahl, gezaehlt an QUELLEN des Snapshots.
    quellen = {str(q).strip() for q in (snapshot.get("quellen") or [])
               if str(q).strip()}
    if len(verwendet & quellen) < int(bound["belege"]["mindestens"]):
        return Verdict(False, "not_enough_sources")

    if geprueft["weiterarbeit_noetig"]:
        return Verdict(False, "further_work_required")

    return Verdict(True, "goal_met", contract=INFORMATION,
                   evidence=snapshot_digest)


def evaluate(*, scope: str, work_product: str = "") -> Verdict:
    """**Die eine Erfuellungsentscheidung.** Beide Naehte laufen hier durch.

    Es gibt bewusst KEINEN `goal`-Parameter. Das ist die ganze Reparatur: der
    Zieltext hat auf diese Entscheidung keinen Einfluss mehr, also kann auch
    keine Formulierung sie mehr aushebeln.
    """
    if scope != SCOPE_BUILD:
        # Jedes natuerliche Rechercheziel. Das Ergebnis bleibt verfuegbar; nur
        # die Erfuellungsaussage faellt weg, weil sie hier nicht belegbar ist.
        return Verdict(False, "no_supported_fulfilment_contract")
    if not str(work_product or "").strip():
        return Verdict(False, "no_work_product")
    return Verdict(True, "goal_met", contract=BUILD_WORK_PRODUCT,
                   evidence=str(work_product).strip())


__all__ = ["BUILD_WORK_PRODUCT", "CONTRACTS", "Verdict", "evaluate"]
