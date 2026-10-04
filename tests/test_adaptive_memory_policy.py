"""Die deterministischen Wachen — was SOLVIO NICHT lernt, und warum.

Diese Datei prueft `memory/adaptive/policy.py`: die eine Stelle, an der ohne
Modellbeteiligung entschieden wird. Sie ist die wichtigste Suite dieses
Milestones, denn sie beantwortet die Frage, an der die Ein-Aeusserungs-Regel
haengt: hat der authentifizierte Besitzer das gerade UEBER SICH und in der
GEGENWART behauptet?

DIE ASYMMETRIE, die hier bewiesen wird: ein Risiko-Flag des Modells blockiert
verlaesslich; ein Unbedenklich-Urteil genuegt NIE. Mehrere Zusicherungen setzen
das Modell absichtlich auf `about="self"` und `flags=[]`, waehrend der Turntext
etwas anderes sagt — und verlangen, dass der Core es trotzdem faengt.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

from solvio.contracts.memory import MemoryType, Sensitivity  # noqa: E402
from solvio.memory.adaptive import policy as P  # noqa: E402

enforce_assertions()


def _turn(text: str, *, channel: str = "voice_iphone",
          role: str = "user") -> P.OwnerTurn:
    return P.OwnerTurn(text=text, channel=channel, role=role,
                       conversation_id="c1", message_id="m1")


def _proposal(**kw) -> P.Proposal:
    base = dict(statement="Bevorzugt kurze Antworten.", kind=P.STATED,
                memory_type=MemoryType.PREFERENCE, subject="pref:laenge",
                about="self", sensitivity=Sensitivity.PERSONAL,
                flags=frozenset())
    base.update(kw)
    return P.Proposal(**base)


def _decide(text: str, *, conversations: int = 0, suppressed: bool = False,
            secret: bool = False, conflict=None, **kw) -> P.Decision:
    return P.decide(_proposal(**kw), _turn(text),
                    evidence_conversations=conversations, suppressed=suppressed,
                    secret_hit=secret, conflict=conflict)


# =====================================================================
# Das Quellen-Gate
# =====================================================================

def t_a_trusted_endpoint_is_not_a_verified_speaker() -> None:
    """Die Unterscheidung, an der dieser Milestone haengt.

    Ein registrierter, attestierter Satellit beweist, dass DIESES GERAET
    spricht — nicht, dass der Mensch spricht, dem das Gedaechtnis gehoert.
    Gemessen am 2026-08-26: ein laufendes Video wurde als Selbstaussage des
    Besitzers gelernt.
    """
    require_equal(P.class_of("voice_iphone"),
                  P.SourceClass.VERIFIED_DEVICE_INTERACTIVE, "iPhone falsch")
    require_equal(P.class_of("voice_satellite"), P.SourceClass.ROOM_MICROPHONE,
                  "der Satellit gilt als belegter Sprecher")
    require_equal(P.AUTO_LEARNING_CLASSES,
                  frozenset({P.SourceClass.VERIFIED_DEVICE_INTERACTIVE,
                             P.SourceClass.VERIFIED_ACCOUNT_INTERACTIVE}),
                  "unerwartete lernfaehige Herkunftsklasse")
    require(not P.may_auto_learn("voice_satellite"),
            "aus dem Raummikrofon wird automatisch gelernt")
    require(P.may_auto_learn("voice_iphone"),
            "vom Telefon wird nicht gelernt")


def t_an_unknown_channel_is_external_and_fails_closed() -> None:
    """Wer einen Endpunkt anhaengt und die Klasse vergisst, bekommt die enge."""
    for channel in ("web_page", "gmail", "hermes", "codex", "browser",
                    "home_assistant", "doctor", "portal", "owner_chat", ""):
        require_equal(P.class_of(channel), P.SourceClass.EXTERNAL, channel)
        require(not P.may_auto_learn(channel), f"{channel!r} darf lernen")


def t_the_pi_is_refused_with_its_own_reason() -> None:
    """`speaker_unverified` ist nicht dasselbe wie `channel_not_eligible`.

    Der Unterschied gehoert ins Protokoll und in jede spaetere Fehlersuche:
    das eine ist ein Kanal, den es nicht geben darf, das andere ein Kanal, der
    stimmt — mit einem Sprecher, der nicht belegt ist.
    """
    ok, reason = _turn("Ich mag Kaffee.", channel="voice_satellite").is_eligible()
    require(not ok, "der Satellit kam durch")
    require_equal(reason, "speaker_unverified", reason)
    ok, reason = _turn("Ich mag Kaffee.", channel="web_page").is_eligible()
    require(not ok, "eine Webseite kam durch")
    require_equal(reason, "channel_not_eligible", reason)


def t_only_the_user_role_is_eligible() -> None:
    """Assistententext ist keine Eingabe. Sonst wuerde Halluzination Gedaechtnis."""
    for role in ("assistant", "system", "tool", "function", ""):
        ok, reason = _turn("Ich mag Kaffee.", role=role).is_eligible()
        require(not ok, f"Rolle {role!r} kam durch")
        require_equal(reason, "not_owner_role", role)


def t_there_is_exactly_one_source_of_truth_for_eligibility() -> None:
    """Eine zweite Liste waere eine zweite Wahrheit.

    Es gab kurz eine abgeleitete `ELIGIBLE_CHANNELS`. Die eigene Zusicherung
    gegen tote Wachenlisten hat sie gefunden: berechnet, aber von niemandem
    gelesen. Zulassung faellt jetzt an genau einer Stelle — `class_of()` gegen
    `AUTO_LEARNING_CLASSES`.
    """
    require(not hasattr(P, "ELIGIBLE_CHANNELS"),
            "es gibt wieder eine zweite Zulassungsliste")
    lernfaehig = {c for c in P.CHANNEL_CLASS if P.may_auto_learn(c)}
    require_equal(lernfaehig, {"voice_iphone", "task_iphone", "task_dashboard", "voice_browser",
                             "chat_iphone", "chat_dashboard"},
                  f"die Zulassung hat sich veraendert: {lernfaehig}")
    require_equal(P.class_of("chat_iphone"), P.SourceClass.VERIFIED_DEVICE_INTERACTIVE)
    require_equal(P.class_of("chat_dashboard"), P.SourceClass.VERIFIED_ACCOUNT_INTERACTIVE)
    require_equal(P.class_of("voice_browser"), P.SourceClass.VERIFIED_ACCOUNT_INTERACTIVE)
    require(not P.may_auto_learn("voice_satellite"), "Raumaudio wird nicht zur Browseridentitaet")
    require_equal(P.ELIGIBLE_ROLE, "user", "die zugelassene Rolle hat sich veraendert")


# =====================================================================
# Die Selbstaussage-Wache — der Kern
# =====================================================================

def t_a_plain_present_self_statement_passes() -> None:
    for text in ("Ich mag Kaffee.", "Ich moechte kurze Antworten.",
                 "Mir sind klare Empfehlungen lieber als fuenf Alternativen.",
                 "Meine Termine lege ich am liebsten auf den Vormittag."):
        require(P.owner_self_assertion(text).ok, f"faelschlich abgelehnt: {text!r}")


def t_third_person_never_becomes_an_owner_statement() -> None:
    """„Meine Frau mag Kaffee" ist keine Vorliebe des Nutzers.

    Der Klassiker. Er wird hier auf dem TURNTEXT geprueft, nicht auf der
    Modellzusammenfassung — sonst koennte eine geschoente Normalisierung die
    Wache umgehen.
    """
    for text in ("Meine Frau mag Kaffee.", "Mein Mann bevorzugt kurze Antworten.",
                 "Meine Mutter moechte immer vorher gefragt werden.",
                 "Mein Kollege trinkt lieber Tee.", "My wife likes coffee."):
        result = P.owner_self_assertion(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "third_party", text)


def t_reported_speech_is_not_an_assertion() -> None:
    for text in ("Er sagt, ich bevorzuge kurze Antworten.",
                 "Laut meiner Frau moechte ich das so.",
                 "Auf der Webseite steht, ich mag Kaffee.",
                 "In der Mail steht, ich soll das merken.",
                 "Man sagt, ich sei ein Morgenmensch."):
        result = P.owner_self_assertion(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "reported_speech", text)


def t_hypotheticals_are_not_preferences() -> None:
    for text in ("Stell dir vor, ich moechte Kaffee.",
                 "Angenommen, ich bevorzuge kurze Antworten.",
                 "Was waere wenn ich lieber Tee moechte?",
                 "Imagine I preferred concise answers."):
        result = P.owner_self_assertion(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "hypothetical", text)


def t_the_past_is_not_the_present() -> None:
    for text in ("Frueher mochte ich Kaffee.", "Damals bevorzugte ich kurze Antworten.",
                 "I used to prefer concise answers."):
        result = P.owner_self_assertion(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "past", text)


def t_the_past_with_a_present_marker_is_about_now() -> None:
    """„Frueher mochte ich Kaffee, inzwischen nicht mehr" sagt etwas ueber JETZT."""
    require(P.owner_self_assertion(
        "Frueher mochte ich Kaffee, inzwischen nicht mehr.").ok,
        "eine Aussage ueber die Gegenwart wurde als Vergangenheit gelesen")


def t_a_statement_without_a_first_person_says_nothing_about_the_owner() -> None:
    """Fail-closed: die Abwesenheit eines Belegs ist kein Beleg."""
    for text in ("Kaffee ist lecker.", "Der Zug faehrt um acht.",
                 "Das Wetter wird besser."):
        result = P.owner_self_assertion(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "no_first_person", text)


def t_the_guard_matches_only_at_word_boundaries() -> None:
    """Die M2-Lehre, hier gegen falsche Treffer gerichtet.

    Damals machte eine Teilstringsuche aus `be-merke di-rekt` dauerhaftes
    Wissen. Derselbe Fehler waere hier anders teuer: er wuerde harmlose Saetze
    verwerfen, die Wache unbrauchbar machen — und der naechste Mensch weicht
    sie auf, statt sie zu reparieren.

    `laut` steckt in `Laufsport` nicht als ganzes Wort, `mein` nicht in
    `meinetwegen`, `heute` nicht in `heutig`.
    """
    require(P.owner_self_assertion("Ich mag Laufsport und Gartenarbeit.").ok,
            "ein Teilstring hat die Zitat-Wache ausgeloest")
    require(P.owner_self_assertion("Ich komme meinetwegen zeitig.").ok,
            "ein Teilstring hat die Dritte-Person-Wache ausgeloest")
    require(P.is_useful("Ich bevorzuge heutige Termine nicht.").ok,
            "ein Teilstring hat die Transient-Wache ausgeloest")


def t_every_guard_list_is_actually_consulted() -> None:
    """Eine stumm geloeschte Wache muss auffallen — auch ohne passenden Fall.

    Gemessen: ein abgebrochener Mutationslauf hinterliess die Datei mutiert,
    der naechste Lauf nahm diesen Stand als Ausgangspunkt, und die
    Zitat-Pruefung war real aus `owner_self_assertion` verschwunden. Der
    Verhaltenstest hat sie gefangen — aber nur, weil zufaellig ein passender
    Satz in der Liste stand. Diese Zusicherung braucht keinen Zufall: sie
    prueft, dass JEDE Wachenliste im Code auch gelesen wird.
    """
    import ast
    import inspect

    source = inspect.getsource(P)
    tree = ast.parse(source)
    guard = next(node for node in tree.body
                 if isinstance(node, ast.FunctionDef)
                 and node.name == "owner_self_assertion")
    consulted = {n.id for n in ast.walk(guard)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    for name in ("HYPOTHETICAL", "REPORTED", "THIRD_PARTY", "SARCASM", "PAST",
                 "FIRST_PERSON", "SUPERSESSION"):
        require(name in consulted,
                f"die Wache {name} ist definiert, wird aber nie gelesen")

    # Und keine Wachenliste im Modul darf verwaisen.
    declared = {n.targets[0].id for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id.isupper()}
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    require_equal(sorted(declared - used), [],
                  "eine Wachenliste wird nirgends benutzt")


# =====================================================================
# Sensitivitaet
# =====================================================================

def t_the_core_recognises_sensitive_domains_without_the_model() -> None:
    """Ein Modell kann verschaerfen, nie senken — also muss der Core es koennen."""
    for text in ("Ich habe Diabetes.", "Meine Diagnose ist gestellt.",
                 "Mein Gehalt reicht nicht.", "Ich habe Schulden.",
                 "Mein Anwalt raet mir dazu.", "Die Scheidung laeuft.",
                 "Ich nehme Medikamente.", "Ich war beim Psychiater."):
        require_equal(P.lexical_sensitivity(text), Sensitivity.SENSITIVE, text)


def t_opinions_about_religion_and_politics_are_sensitive_by_topic() -> None:
    """Nach dem WORT zu suchen war zu wenig — gemessen im ersten Produktivlauf.

    Das Lexikon enthielt „religion", „glaube" und „politisch", und trotzdem
    wurden Meinungen UEBER Religion und Politik still adoptiert: ein Satz ueber
    den Islam enthaelt das Wort „Religion" nicht. Die Architektur nennt genau
    diese Klasse (§6.4: „Religion, Politik — im Zweifel immer die hoehere
    Stufe").

    Die Saetze hier sind KEINE Erfindung. Es sind die, die am 2026-08-26
    zwischen 02:00 und 02:06 in den produktiven Speicher gelaufen sind.
    """
    for text in (
        "Der Besitzer findet die Naivitaet gegenueber dem Islam unertraeglich.",
        "Der Besitzer haelt es fuer wichtig, offen und oeffentlich ueber Zahlen "
        "und Demokratie zu diskutieren.",
        "Der Besitzer weiss, dass es 114 Suren im Koran gibt.",
        "Der Besitzer kennt die verschiedenen Gruppen im Islam.",
        "Der Besitzer erlebt eine persoenliche Challenge bezueglich Toleranz.",
        "Der Besitzer haelt Toleranz fuer eine Frucht des Westens.",
    ):
        require_equal(P.lexical_sensitivity(text), Sensitivity.SENSITIVE,
                      f"still adoptierbar: {text[:60]!r}")


def t_harmless_preferences_stay_harmless() -> None:
    """Die Gegenprobe: das breitere Lexikon darf nicht alles einsammeln.

    Ein Lexikon, das jede Vorliebe zur Rueckfrage macht, ist kein Schutz —
    es ist ein Postfach voller Fragen, das niemand mehr liest.
    """
    for text in ("Bevorzugt eine klare Empfehlung statt fuenf Alternativen.",
                 "Bevorzugt Termine am Vormittag.",
                 "Mag Kaffee.",
                 "Das Projekt heisst SOLVIO und laeuft auf einem Mac mini.",
                 "Moechte vor Aenderungen gefragt werden.",
                 "Der Besitzer liebt alle Menschen."):
        require_equal(P.lexical_sensitivity(text), Sensitivity.PERSONAL,
                      f"faelschlich als schutzbeduerftig: {text[:60]!r}")


def t_a_model_label_can_only_tighten_never_loosen() -> None:
    require_equal(P.strictest(Sensitivity.SENSITIVE, Sensitivity.PUBLIC),
                  Sensitivity.SENSITIVE, "das Modell hat die Stufe gesenkt")
    require_equal(P.strictest(Sensitivity.PERSONAL, Sensitivity.SENSITIVE),
                  Sensitivity.SENSITIVE, "die Verschaerfung kam nicht an")


def t_sensitive_content_is_never_adopted_silently() -> None:
    """Auch OHNE Modell-Label: das Core-Lexikon allein loest die Rueckfrage aus."""
    decision = _decide("Ich habe seit einem Jahr Diabetes.",
                       statement="Hat Diabetes.",
                       memory_type=MemoryType.USER,
                       sensitivity=Sensitivity.PERSONAL)   # Modell sagt harmlos
    require_equal(decision.action, P.ASK, str(decision))
    require_equal(decision.ask_reason, "sensitive", str(decision))


# =====================================================================
# Nuetzlichkeit
# =====================================================================

def t_transient_statements_do_not_become_durable_memory() -> None:
    for text in ("Ich esse heute Pizza.", "Ich bin gerade unterwegs.",
                 "Ich habe im Moment keine Zeit."):
        result = P.is_useful(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "transient", text)


def t_filler_is_not_knowledge() -> None:
    for text in ("Ja genau.", "Ok danke.", "Hallo alles klar"):
        require(not P.is_useful(text).ok, f"durchgelassen: {text!r}")


def t_statements_about_operating_solvio_are_not_knowledge() -> None:
    """„Der Besitzer hat freigegeben" ist ein Knopfdruck, kein Wissen.

    Live gemessen: nach „Ich habe freigegeben" stand genau dieser Satz als
    dauerhafter Eintrag im Gedaechtnis. Er hat jede Wache passiert — erste
    Person, Gegenwart, nicht fluechtig, nicht sensibel — weil der
    Nuetzlichkeitstest nach Zeichen und Fuellwoertern sucht, nicht nach
    Bedeutung.
    """
    for text in ("Der Besitzer hat freigegeben.",
                 "Ich habe das bestaetigt.",
                 "Er hat mit Face ID freigegeben.",
                 "Der Besitzer hat zugestimmt.",
                 "Ich habe geantwortet."):
        result = P.is_useful(text)
        require(not result.ok, f"durchgelassen: {text!r}")
        require_equal(result.reason, "interaction_meta", text)


def t_the_interaction_filter_stays_narrow() -> None:
    """Eine breitere Regel wuerde echte Aussagen mitverwerfen.

    Sie schlaegt nur an, wenn NICHTS uebrig bleibt ausser Rahmenwoertern und
    einem Bedienungsverb. Steht noch Inhalt im Satz, kommt er durch.
    """
    for text in ("Hat seiner Frau gesagt, dass er Kaffee mag.",
                 "Moechte vor Aenderungen gefragt werden.",
                 "Bevorzugt kurze Antworten.",
                 "Bevorzugt Termine am Vormittag.",
                 "Hat den Vertrag bestaetigt bekommen und will ihn kuendigen."):
        require(P.is_useful(text).ok, f"faelschlich verworfen: {text!r}")


def t_a_short_preference_is_still_useful() -> None:
    """Eine Laengenhuerde, die „Mag Kaffee." verwirft, ist kein Nuetzlichkeitstest.

    Gefunden im ersten Rauchtest: die urspruengliche Grenze von 12 Zeichen
    verwarf ausgerechnet Fall 1 der Architektur.
    """
    require(P.is_useful("Mag Kaffee.").ok, "eine brauchbare Vorliebe fiel durch")
    require(P.is_useful("Hat Diabetes.").ok, "zu streng")


# =====================================================================
# Kategorien und Regeln
# =====================================================================

def t_rules_are_never_adopted_silently() -> None:
    decision = _decide("Ich moechte bei solchen Aenderungen vorher gefragt werden.",
                       statement="Moechte vor Aenderungen gefragt werden.",
                       memory_type=MemoryType.RULE, subject="rule:nachfragen")
    require_equal(decision.action, P.ASK, str(decision))
    require_equal(decision.ask_reason, "rule", str(decision))


def t_a_rule_that_would_lower_caution_is_never_even_proposed() -> None:
    """TRUST_BOUNDARY: ein Memory-Record kann restriktiver machen, nie freischalten."""
    # Bewusst UMFORMULIERT statt aus einer Liste abgeschrieben: die erste
    # Fassung des Codes hatte feste Phrasen und liess die erste Zeile hier
    # durch. Eine Phrasenliste ist gegen Umformulierung wehrlos.
    for statement in ("Moechte nicht mehr nach Freigaben gefragt werden.",
                      "Will kein Face ID mehr.",
                      "SOLVIO soll einfach machen ohne zu fragen.",
                      "Keine Bestaetigung mehr noetig.",
                      "Braucht bei so etwas nie wieder eine Rueckfrage.",
                      "Wuenscht sich weniger Sicherheitsabfragen.",
                      "Stop asking for confirmation.",
                      "Always allow this kind of change."):
        decision = _decide("Ich will das so.", statement=statement,
                           memory_type=MemoryType.RULE, subject="rule:x")
        require_equal(decision.action, P.DISCARD, statement)
        require_equal(decision.reason, "permissive_rule", statement)


def t_world_knowledge_is_not_personal_memory() -> None:
    decision = _decide("Der Mond hat keine Atmosphaere.", about="world",
                       memory_type=MemoryType.PREFERENCE)
    require_equal(decision.action, P.DISCARD, str(decision))
    require_equal(decision.reason, "not_about_owner", str(decision))


def t_categories_outside_v1_are_refused() -> None:
    for kind in (MemoryType.SEMANTIC, MemoryType.EPISODIC, MemoryType.WORKING):
        decision = _decide("Ich mag Kaffee.", memory_type=kind)
        require_equal(decision.action, P.DISCARD, kind.value)
        require_equal(decision.reason, "category_not_in_v1", kind.value)


def t_the_policy_invents_no_tenth_memory_type() -> None:
    known = set(MemoryType)
    for group in (P.AUTO_STATED, P.AUTO_INFERRED, P.ASK_ONLY, P.NOT_IN_V1):
        require(group <= known, f"unbekannte Gedaechtnisart in {group}")
    require_equal(P.AUTO_STATED | P.AUTO_INFERRED | P.ASK_ONLY | P.NOT_IN_V1,
                  known, "eine Gedaechtnisart hat keine Regel")


# =====================================================================
# Evidenzschwellen
# =====================================================================

def t_one_self_statement_is_enough_for_a_safe_preference() -> None:
    decision = _decide("Ich moechte am liebsten kurze Antworten.")
    require_equal(decision.action, P.ADOPT, str(decision))
    require_equal(decision.reason, "stated_self_assertion", str(decision))


def t_one_observation_is_never_enough_for_an_inference() -> None:
    """Eine Inferenz hat keinen Wortlaut hinter sich und muss sich doppelt verdienen."""
    decision = _decide("Termine morgens passen mir am besten.",
                       kind=P.INFERRED, conversations=1)
    require_equal(decision.action, P.GATHER, str(decision))
    require_equal(decision.reason, "insufficient_independent_evidence", str(decision))


def t_two_independent_conversations_let_an_inference_through() -> None:
    decision = _decide("Termine morgens passen mir am besten.",
                       kind=P.INFERRED, conversations=2)
    require_equal(decision.action, P.ADOPT, str(decision))


def t_the_inferred_threshold_is_two_and_says_so() -> None:
    require_equal(P.INFERRED_MIN_CONVERSATIONS, 2, "die Schwelle hat sich geaendert")


# =====================================================================
# Widerspruch
# =====================================================================

def t_a_users_word_supersedes_a_machine_guess() -> None:
    decision = _decide("Inzwischen mag ich Kaffee.",
                       conflict={"memory_id": "abc", "user_direct": False})
    require_equal(decision.action, P.ADOPT, str(decision))
    require_equal(decision.reason, "supersedes_learned", str(decision))
    require_equal(decision.contested_memory_id, "abc", str(decision))


def t_a_users_word_never_silently_overwrites_the_users_own_word() -> None:
    """Nutzerwort gegen Nutzerwort: es wird gefragt, nicht ueberschrieben."""
    decision = _decide("Inzwischen mag ich Kaffee.",
                       conflict={"memory_id": "abc", "user_direct": True})
    require_equal(decision.action, P.CONTEST, str(decision))
    require_equal(decision.ask_reason, "contradiction", str(decision))


def t_an_inference_never_supersedes_anything_automatically() -> None:
    decision = _decide("Inzwischen mag ich Kaffee.", kind=P.INFERRED,
                       conversations=5,
                       conflict={"memory_id": "abc", "user_direct": False})
    require_equal(decision.action, P.CONTEST, str(decision))


def t_a_difference_is_not_a_contradiction() -> None:
    """„mag Kaffee" und „mag Tee" koexistieren — ohne Abloesungsmarker kein Streit."""
    require(not P.has_supersession_marker("Ich mag auch Tee."),
            "eine blosse Ergaenzung gilt als Widerspruch")
    for text in ("Ich mag inzwischen Tee.", "Ich trinke nicht mehr Kaffee.",
                 "Ich moechte jetzt lieber Tee."):
        require(P.has_supersession_marker(text), f"Abloesung nicht erkannt: {text!r}")


# =====================================================================
# Verwerfen ohne Rueckstand
# =====================================================================

def t_credential_shaped_content_is_discarded_before_anything_else() -> None:
    decision = _decide("Mein Schluessel ist geheim.", secret=True)
    require_equal(decision.action, P.DISCARD, str(decision))
    require_equal(decision.reason, "secret_shaped", str(decision))
    require(not decision.creates_candidate, "ein Zugangsdatum wurde Kandidat")


def t_a_suppressed_thought_is_not_proposed_again() -> None:
    decision = _decide("Ich mag Kaffee.", suppressed=True)
    require_equal(decision.action, P.DISCARD, str(decision))
    require_equal(decision.reason, "suppressed", str(decision))


def t_every_risk_flag_of_the_model_blocks_reliably() -> None:
    for flag in P.BLOCKING_FLAGS:
        decision = _decide("Ich mag Kaffee.", flags=frozenset({flag}))
        require_equal(decision.action, P.DISCARD, flag)
        require_equal(decision.reason, "model_flag", flag)


def t_the_model_may_stop_us_but_never_wave_us_through() -> None:
    """Die Asymmetrie, auf der die Sicherheitsrechnung beruht.

    Das Modell behauptet hier `about="self"`, `flags=[]`, `sensitivity=public` —
    also durchweg unbedenklich. Der Turntext sagt etwas anderes, und der Core
    entscheidet nach dem Turntext.
    """
    decision = _decide("Meine Frau mag Kaffee.", about="self",
                       flags=frozenset(), sensitivity=Sensitivity.PUBLIC)
    require_equal(decision.action, P.DISCARD, str(decision))
    require_equal(decision.reason, "third_party", str(decision))

    decision = _decide("Stell dir vor, ich moechte Kaffee.", about="self",
                       flags=frozenset(), sensitivity=Sensitivity.PUBLIC)
    require_equal(decision.action, P.DISCARD, str(decision))
    require_equal(decision.reason, "hypothetical", str(decision))


def t_security_reasons_come_before_usefulness() -> None:
    """Ein richtiges Ergebnis mit falscher Begruendung misst nichts.

    Gefunden im Rauchtest: „Meine Frau mag Kaffee" fiel mit `too_short` heraus.
    Eine geaenderte Laengengrenze haette daraus stillschweigend eine Vorliebe
    des Nutzers gemacht.
    """
    decision = _decide("Meine Frau mag Tee.", statement="Mag Tee.")
    require_equal(decision.reason, "third_party",
                  f"aus dem falschen Grund abgelehnt: {decision.reason}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
