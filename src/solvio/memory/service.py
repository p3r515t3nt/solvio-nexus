"""M2 — die dünne Schicht, die SOLVIOs Gedächtnis an den lebenden Assistenten bindet.

WAS HIER *NICHT* PASSIERT

Kein neues Datenmodell, keine zweite Datenbank, kein Parallelschema. Alles Dauerhafte
liegt weiterhin in `SolvioMemory` (memory.sqlite3), der Semantik-Index in
`SemanticIndex` (semantic_index.sqlite3), die Löschwahrheit im Privacy-Ledger
(privacy_ledger.sqlite3). Diese Schicht fügt nur zusammen, was das Fundament schon kann:
Validierung, Provenienz, Entdopplung, Supersession, begrenzte Ausgabe.

DER UNTERSCHIED ZU M1

`conversations.sqlite3` hält den wörtlichen jüngsten Verlauf. Das hier ist ausgewähltes
dauerhaftes Wissen. Eine Nachricht im Gesprächsverlauf ist NICHT automatisch Gedächtnis;
Gedächtnis entsteht nur über den ausdrücklichen Schreibweg in diesem Modul.

VERTRAUEN

Geschrieben wird ausschließlich aus einer direkten Äußerung des lokalen Besitzers
(`TrustLevel.USER_DIRECT`). Assistententext, Werkzeugergebnisse, Webseiten oder
Modellschlüsse erzeugen hier nichts — dafür fehlt bis auf Weiteres die
TrustContext-Integration, und ohne sie wäre jede Automatik eine Einladung.

Abgerufenes Gedächtnis ist INFORMATION, nie Autorität: es geht als Werkzeugergebnis
zurück ins Gespräch, niemals in die System-Anweisung.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from solvio.contracts.memory import (MemoryRecord, MemoryType, ProvenanceEntry,
                                     Sensitivity)
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.intent import MemoryIntent, looks_like_secret
from solvio.logging_setup import get_logger
from solvio.memory.semantic import SemanticMemory

log = get_logger("memory")

DEFAULT_TOP_K = 3
DEFAULT_RESULT_CHARS = 600
# Relevanzschranken sind PROVIDERABHAENGIG. Kosinuswerte verschiedener Modelle liegen
# nicht auf derselben Skala, und eine Zahl von einem Provider auf den anderen zu
# uebertragen war der Fehler der ersten Fassung.
#
# Gemessen auf dem deutschen Korpus (30 Fakten, 26 relevante und 26 unbeteiligte Fragen,
# letztere ueber die je 6 besten Kandidaten = 156 Paare):
#
#   Qwen3-Embedding-0.6B   relevant  cos min 0.536, Median 0.704
#                          unbeteiligt cos max 0.535, Median 0.250
#                          -> cos >= 0.50: 26/26 erkannt, 1/156 durchgelassen
#
# Bei Qwen ist der LEXIKALISCHE Pfad das Leck, nicht die Hilfe: echte Paraphrasen liegen
# dort teils bei 0.043, unbeteiligte Fragen aber bis 0.205. Deshalb dient er dort nur noch
# als Stuetze mit hoher Schwelle. Der Hashing-Provider hat keine echte Semantik; dort ist
# es umgekehrt und die lexikalische Naehe traegt die Entscheidung.
class RetrievalBounds:
    """Was ein Treffer mindestens erreichen muss, je nach Provider."""

    def __init__(self, min_cosine: float, min_lexical: float, source: str) -> None:
        self.min_cosine = min_cosine
        self.min_lexical = min_lexical
        self.source = source

    def accepts(self, cosine: float, lexical: float) -> bool:
        return cosine >= self.min_cosine or lexical >= self.min_lexical


_BOUNDS = {
    "qwen-local": RetrievalBounds(0.50, 0.55, "gemessen an 26/26 und 1/156"),
    "deterministic": RetrievalBounds(0.35, 0.12, "gemessen am Hashing-Korpus"),
}
_DEFAULT_BOUNDS = RetrievalBounds(0.50, 0.55, "Vorgabe fuer unbekannte Provider")


def bounds_for(provider_id: str) -> RetrievalBounds:
    return _BOUNDS.get(provider_id, _DEFAULT_BOUNDS)


# Supersession entscheidet ueber ZERSTOERUNG und laeuft deshalb NUR ueber Wortueberlappung.
# Die Zeichen-n-Gramme sind fuer die Abruf-Robustheit bei deutschen Komposita da; sie als
# Loeschkriterium zu nehmen war der Fehler: deutsche Saetze mit gleichem Satzrahmen
# ueberschritten die Schwelle routinemaessig, und 14 harmlose Korrektursaetze zerstoerten
# in einer Messung 9 von 12 gespeicherten Fakten.
SUPERSEDE_MIN = 0.55
# Der beste Kandidat muss den zweitbesten deutlich schlagen. Sonst ist unklar, WAS
# korrigiert werden soll — und im Zweifel wird nichts zerstoert, sondern nachgefragt.
SUPERSEDE_MARGIN = 0.15
# Zweite Stufe fuer AUSDRUECKLICHE Korrekturen, die anders formuliert sind als der alte
# Fakt. Wortueberlappung allein findet sie nicht: "Der Vertrag endet erst im April" teilt
# mit "der Vertrag laeuft bis Ende Maerz" nur 0.125 — und ohne diese Stufe entstand ein
# ZWEITER aktiver Fakt, den der Abruf sogar HINTER den veralteten stellte.
#
# Gemessen auf deutschem Bestand mit Qwen:
#   Vertrag/April      Ziel cos 0.714, naechster 0.299  -> Abstand 0.415
#   Anna/Bosch         Ziel cos 0.525, naechster 0.256  -> Abstand 0.269
#   Termin/Mittwoch    FALSCHER Kandidat oben (0.433 gegen 0.398, Abstand 0.035)
# Der dritte Fall ist genau der, in dem geraten toedlich waere — die Abstandsforderung
# faengt ihn ab und fuehrt zur Rueckfrage statt zur Loeschung.
SUPERSEDE_SEMANTIC_MIN = 0.50
SUPERSEDE_SEMANTIC_MARGIN = 0.15
# Ab hier sind Kandidaten plausibel genug, dass ein Ratespiel gefaehrlich waere: eine
# ausdrueckliche Korrektur legt dann KEINEN zweiten Fakt an, sondern fragt nach.
SUPERSEDE_AMBIGUITY_FLOOR = 0.40

_WORD = re.compile(r"[\w\-]+", re.UNICODE)
# Funktionswoerter zaehlen nicht als Inhalt. Die Verben standen frueher NICHT hier, und
# genau sie trugen die Falsch-Positiven: "Wie viele Bundeslaender HAT Oesterreich?" traf
# "mein Fahrrad HAT die Rahmennummer WBK4471" allein ueber das geteilte "hat".
_STOPWORDS = frozenset("""
der die das dem den des ein eine einen einem eines und oder aber ist sind war waren
im in am an auf fuer für mit von zu zum zur ich mein meine meinem meinen du dein deine
dass das es sich nicht auch noch schon nur sehr mehr als wie so dann wenn weil
hat habe haben hatte hatten heisst heissen heisse steht stehen stand liegt liegen lag
muss muessen musste kann koennen konnte will wollen wollte wird werden wurde wurden
gibt geben gab macht machen machte geht gehen ging kommt kommen kam bin bist seid
welche welcher welches was wer wo wann warum wieviel viele viel etwas jemand man
""".split())


class MemoryUnavailable(RuntimeError):
    """Das Gedächtnis konnte seine Zusage nicht halten. Nie stillschweigend schlucken."""


def memory_base_dir() -> str:
    """Verzeichnis der Gedächtnisdateien — neben dem Gesprächsspeicher, getrennt vom
    Sicherheitszustand unter ~/.solvio-approvals."""
    state = os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))
    return os.path.join(state, "memory")


def select_local_provider():
    """Ausschließlich LOKALE Einbettungen — nie die OpenAI-Embedding-API.

    Ist `sentence-transformers` vorhanden, wird das lokale Qwen3-Modell genutzt. Sonst
    der deterministische Hashing-Provider: ebenfalls vollständig lokal und netzfrei,
    aber rein lexikalisch — echte Synonym-Nähe kann er nicht. Welcher Weg läuft, steht
    in `health()`; geraten wird nichts.
    """
    if os.environ.get("SOLVIO_EMBEDDING", "").strip().lower() == "hashing":
        return HashingEmbeddingProvider()
    # find_spec statt import: das blosse Pruefen der Verfuegbarkeit darf den Torch-Stack
    # nicht hochziehen. Gemessen kostete `import sentence_transformers` +354 MB RSS beim
    # Core-Start — noch bevor ein Modell geladen war — und haette damit die Faulheit des
    # Providers ausgehebelt, der bewusst erst beim ersten Embed laedt.
    if importlib.util.find_spec("sentence_transformers") is None:
        return HashingEmbeddingProvider()
    from solvio.memory.embedding import QwenLocalEmbeddingProvider
    return QwenLocalEmbeddingProvider()


def _tokens(text: str) -> set[str]:
    """Inhaltswörter für ÄHNLICHKEIT. Zahlen bleiben drin, auch einstellige — sie tragen
    oft genau die Information, um die es geht ("Aurora startet im Quartal 1")."""
    out = set()
    for word in _WORD.findall(text or ""):
        low = word.lower()
        if low in _STOPWORDS:
            continue
        if len(low) > 1 or any(c.isdigit() for c in low):
            out.add(low)
    return out


def _expand(tokens: set[str]) -> set[str]:
    """Bindestrich-Zusammensetzungen auch in ihren Teilen fuehren.

    Gesprochenes wird mal als "Langzeit-Testwort", mal als "Langzeit Testwort"
    verschriftet. Ohne diese Aufspaltung sind das drei verschiedene Tokens.
    """
    out = set(tokens)
    for token in tokens:
        if "-" in token:
            out.update(part for part in token.split("-") if len(part) > 2)
    return out


def _ngrams(text: str, n: int = 4) -> set[str]:
    """Zeichen-n-Gramme ueber den zusammengezogenen Text.

    Deutsche Komposita verschmelzen in der Transkription oft ohne Trenner:
    "Langzeittestwort" gegen "Langzeit-Testwort". Auf Wortebene ist das kein Treffer,
    auf Zeichenebene teilen sie fast alles.
    """
    flat = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    if len(flat) < n:
        return {flat} if flat else set()
    return {flat[i:i + n] for i in range(len(flat) - n + 1)}


def _covers(query: str, content: str) -> bool:
    """Steckt die ganze Anfrage als UNTERSCHEIDBARE Woerter im Eintrag?

    Ein exakter Bezeichner ("WBK4471") oder ein seltenes Kompositum ("Rahmennummer") ist
    ein eindeutiger Treffer, auch wenn die Ueberlappung mit dem vollen Satz klein bleibt.
    Alltagswoerter zaehlen dafuer nicht — sonst traefe "hat" auf den halben Bestand.
    """
    q = _tokens(query)
    if not q:
        return False
    distinctive = {t for t in q if len(t) >= 6 or any(c.isdigit() for c in t)}
    if not distinctive or distinctive != q:
        return False
    return distinctive <= _expand(_tokens(content))


def _word_similarity(a: str, b: str) -> float:
    """Nur Wortueberlappung — die Grundlage jeder ZERSTOERENDEN Entscheidung."""
    ta, tb = _expand(_tokens(a)), _expand(_tokens(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _similarity(a: str, b: str) -> float:
    """Lexikalische Naehe: Woerter UND Zeichen-n-Gramme, der bessere Wert gewinnt.

    Reproduziert, bevor die n-Gramme dazukamen: die Aeusserung wurde als
    "mein Langzeittestwort ist Bernstein84" transkribiert, die spaetere Frage als
    "Was ist mein Langzeit-Testwort?" — Wort-Jaccard 0.000, kein Treffer. Ein
    Gedaechtnis, das nur bei identischer Verschriftung erinnert, ist keins.
    """
    ta, tb = _expand(_tokens(a)), _expand(_tokens(b))
    word = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    ga, gb = _ngrams(a), _ngrams(b)
    char = len(ga & gb) / len(ga | gb) if ga and gb else 0.0
    return max(word, char)


def _dedup_key(text: str) -> str:
    """Schlüssel für "wurde derselbe Satz schon einmal gesagt?".

    Bewusst VERLUSTFREI bis auf Groß-/Kleinschreibung, Satzzeichen und Leerraum:
    Reihenfolge und jedes Wort bleiben erhalten.

    Reproduziert, bevor das hier stand: die frühere Fassung verglich SORTIERTE
    Inhaltswörter und verwarf dabei einstellige Zahlen. "Notiz Nummer 1 betrifft
    Thema 1" und "Notiz Nummer 0 betrifft Thema 0" ergaben denselben Schlüssel — von
    50 verschiedenen Fakten kamen nur 41 an, neun wurden als Dubletten verschluckt.
    Ein falsch erkanntes Duplikat verliert Wissen; ein übersehenes kostet nur einen
    zweiten Eintrag.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s\-]", " ", (text or "").lower())).strip()


def _subject_of(text: str) -> str:
    """Grober Gruppierungsschlüssel für `history(subject)`. Keine Semantik, nur Stabilität."""
    words = [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOPWORDS]
    return " ".join(words[:3]) or (text or "")[:40]


@dataclass
class RememberResult:
    ok: bool
    action: str                     # created | duplicate | superseded | ambiguous | refused | failed
    memory_id: str = ""
    superseded_id: str = ""
    reason: str = ""
    content: str = ""
    # Dauerhaft gespeichert heisst nicht automatisch semantisch auffindbar. Faellt die
    # Einbettung aus, liegt der Eintrag im kanonischen Speicher und wird lexikalisch
    # gefunden — aber der semantische Arm ist blind fuer ihn. Das wird gemeldet, nicht
    # als voller Erfolg ausgegeben.
    indexed: bool = True


@dataclass
class MemoryHit:
    memory_id: str
    content: str
    memory_type: str
    created_at: str
    relevance: int                  # 1 = bester Treffer
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryService:
    """Die einzige Stelle, über die der lebende Assistent Gedächtnis schreibt oder liest."""

    #: Wird nach jeder kanonischen Aenderung gerufen. Gesetzt beim Zusammenbau.
    on_change = None

    def __init__(self, base_dir: str | None = None, provider=None, *,
                 now_fn: Callable[[], float] = time.time) -> None:
        self.base_dir = base_dir or memory_base_dir()
        self.provider = provider or select_local_provider()
        self.now = now_fn
        self.bounds = bounds_for(self.provider.profile.provider_id)
        self.reindex_pending = False
        self.previous_profile = ""
        self.index_incomplete = False
        self.model_load_error = ""
        self._pending_consistency_check = False
        self.semantic: SemanticMemory | None = None
        self.available = False
        self.last_error = ""

    # ----------------------------------------------------------------- Lebenszyklus
    def notify_changed(self) -> None:
        """Melden, dass sich kanonische Wahrheit geaendert hat.

        Der Dienst weiss NICHT, wer darauf reagiert — der Haken wird beim
        Zusammenbau gesetzt (`tools/registry.py`). Das ist kein Umweg, sondern
        die Richtung, die eine bestehende Zusicherung verlangt: kein Modul
        unter `capabilities/` darf `solvio.knowledge` importieren, damit eine
        gefaelschte Wissensdatei keinen Aufrufer im Freigabepfad hat. Ein
        Freigabe-Handler, der nach der Aenderung selbst kompiliert, haette
        genau diesen Import mitgebracht.
        """
        hook = self.on_change
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - eine Ansicht darf ausfallen
            log.info("memory.change_hook_failed", kind=type(exc).__name__)

    def open(self) -> "MemoryService":
        """Synchron: nichts hieran ist asynchron, und der Aufrufer soll nicht so tun."""
        os.makedirs(self.base_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.base_dir, 0o700)
        except OSError:
            pass
        try:
            self.semantic = SemanticMemory(self.base_dir, self.provider)
            self._harden_index_perms()
            self._note_profile_change()
            self.available = True
            self._pending_consistency_check = True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise MemoryUnavailable(self.last_error) from exc
        return self

    def _harden_index_perms(self) -> None:
        """Den Semantik-Index auf 0600 bringen — wie seine Geschwister.

        Beobachtet: `memory.sqlite3` und `privacy_ledger.sqlite3` kommen mit 0600 aus dem
        Fundament, `semantic_index.sqlite3` mit 0644. Im 0700-Verzeichnis ist der
        praktische Unterschied klein, aber der Index traegt aus Gedaechtnisinhalten
        abgeleitete Vektoren, und eine Datei, die anders geschuetzt ist als ihre
        Geschwister, faellt spaeter niemandem mehr auf. Gerichtet wird hier, nicht im
        eingefrorenen Fundament.
        """
        index = os.path.join(self.base_dir, "semantic_index.sqlite3")
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(index + suffix, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    async def _confirm_embedding_alive(self, query: str) -> None:
        """Einmal nachfragen, ob der Provider wirklich einbetten kann.

        Wird nur aufgerufen, wenn der semantische Arm leer blieb, obwohl der Index unter
        dem aktiven Profil Eintraege haelt. Schlaegt es fehl, ist das kein "gesundes
        Gedaechtnis mit lexikalischem Ergebnis", sondern ein ausgefallenes Modell — und
        das gehoert in `health()`.
        """
        index = getattr(self.semantic, "index", None)
        if index is None:
            return
        try:
            if index.count(self.provider.profile.key) == 0:
                return                    # nichts zu finden, kein Hinweis auf einen Ausfall
            await self.provider.embed_queries([query])
        except Exception as exc:  # noqa: BLE001
            self.model_load_error = f"{type(exc).__name__}: {exc}"
            log.error("memory.embedding_unavailable", kind=type(exc).__name__)

    def _is_indexed(self, memory_id: str) -> bool:
        """Liegt fuer diesen Eintrag ein Vektor im AKTIVEN Profil?"""
        index = getattr(self.semantic, "index", None)
        if index is None or not memory_id:
            return False
        try:
            return index.get_entry(memory_id, self.provider.profile.key) is not None
        except Exception:  # noqa: BLE001
            return False

    async def check_index_consistency(self) -> dict:
        """Kanonischer Speicher gegen abgeleiteten Index — er ist die Wahrheit, nicht der Index.

        Deckt drei Faelle ab, die vorher unsichtbar blieben: ein Schreibvorgang, dessen
        Einbettung fehlschlug; ein von aussen geloeschter oder neu angelegter Index; und
        ein Neuaufbau, der mitten drin abgebrochen ist. Alle drei zeigen sich als
        Mengenunterschied und fuehren zum selben, wiederaufnehmbaren Neuaufbau.
        """
        if self.semantic is None:
            return {"consistent": False, "reason": "unavailable"}
        index = getattr(self.semantic, "index", None)
        if index is None:
            return {"consistent": False, "reason": "no_index", "missing": -1}
        try:
            active = await self.semantic.memory.active_ids()
            indexed = index.ids_for_profile(self.provider.profile.key)
        except Exception as exc:  # noqa: BLE001
            return {"consistent": False, "reason": f"{type(exc).__name__}", "missing": -1}
        missing = active - indexed
        stale = indexed - active
        return {"consistent": not missing, "missing": len(missing),
                "stale": len(stale), "active": len(active), "indexed": len(indexed)}

    def _note_profile_change(self) -> None:
        """Hat der Einbettungs-Provider gewechselt, muss der Index neu aufgebaut werden.

        Beobachtet nach dem Wechsel von Hashing auf Qwen: der Index trug weiterhin
        `deterministic:hashing:d256:v1` als AKTIVES Profil, unter dem neuen Profil waren
        NULL Einträge, und jede semantische Suche lief ins Leere — still, ohne Meldung.
        Das Fundament wechselt das aktive Profil absichtlich nicht von selbst; diese
        Entscheidung gehört hierher.

        Der Neuaufbau läuft nicht beim Start (das würde ihn verzögern), sondern beim
        ersten Zugriff — und er wird gemeldet, nicht verschwiegen.
        """
        index = getattr(self.semantic, "index", None)
        if index is None:
            return
        active = index.active_profile()
        wanted = self.provider.profile.key
        if active == wanted:
            self.reindex_pending = False
            return
        self.previous_profile = active or ""
        index.set_active_profile(wanted)
        self.reindex_pending = True

    async def ensure_index(self) -> dict | None:
        """Den Index auf den kanonischen Speicher bringen. Idempotent und wiederaufnehmbar.

        Ausgeloest durch einen Profilwechsel ODER durch fehlende Vektoren — Letzteres
        deckt den geloeschten Index, die fehlgeschlagene Einbettung und den abgebrochenen
        Neuaufbau ab. `rebuild()` ueberspringt unveraenderte Inhalte, ein Abbruch ist
        also beim naechsten Anlauf fortsetzbar.
        """
        if self.semantic is None:
            return None
        if self._pending_consistency_check:
            self._pending_consistency_check = False
            report = await self.check_index_consistency()
            if not report.get("consistent"):
                self.reindex_pending = True
                self.index_incomplete = True
                log.warning("memory.index_incomplete", **{
                    k: v for k, v in report.items() if k != "consistent"})
        if not self.reindex_pending:
            return None
        # Die Absicht wird ERST NACH erfolgreichem Neuaufbau geloescht.
        #
        # Reproduziert: `reindex_pending` wurde vorher auf False gesetzt. Wurde die Task
        # dann abgebrochen, lief CancelledError am gewoehnlichen Exception-Zweig vorbei —
        # und die Wiederholungsabsicht war fuer den Rest des Prozesses verschwunden. Erst
        # ein Neustart machte das Gedaechtnis wieder vollstaendig durchsuchbar.
        try:
            report = await self.semantic.rebuild()
        except asyncio.CancelledError:
            self.index_incomplete = True         # bleibt offen und sichtbar
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.index_incomplete = True
            raise MemoryUnavailable(f"reindex failed: {self.last_error}") from exc
        self.reindex_pending = False
        # `rebuild()` paart Texte und Vektoren nach Position. Liefert der Provider WENIGER
        # Vektoren als angefordert, faellt der Rest lautlos weg und der Bericht meldet
        # trotzdem die volle Zahl. Reproduziert: `indexed: 4` bei `total_in_index: 3`.
        # Der Vergleich gegen den kanonischen Speicher ist die Wahrheit, nicht der Bericht.
        submitted = (report or {}).get("indexed")
        landed = (report or {}).get("total_in_index")
        if submitted is not None and landed is not None and landed < submitted:
            log.error("memory.short_embedding_batch", submitted=submitted, landed=landed)
        after = await self.check_index_consistency()
        self.index_incomplete = not after.get("consistent", False)
        if self.index_incomplete:
            self.reindex_pending = True          # naechster Zugriff setzt fort
            log.warning("memory.index_still_incomplete",
                        **{k: v for k, v in after.items() if k != "consistent"})
        log.info("memory.reindexed", **{k: v for k, v in (report or {}).items()})
        return report

    async def close(self) -> None:
        if self.semantic is not None:
            try:
                await self.semantic.close()
            finally:
                self.semantic = None
                self.available = False

    def _now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.now(), tz=timezone.utc)

    # ----------------------------------------------------------------- Schreiben
    async def remember(self, intent: MemoryIntent, *, conversation_id: str = "",
                       source_message_id: str = "", source_session_id: str = "",
                       source_turn_id: str = "",
                       memory_type: str = "") -> RememberResult:
        """Dauerhaftes Wissen aus einer AUSDRÜCKLICHEN Äußerung des Besitzers anlegen.

        Der Aufrufer muss die Absicht bereits erkannt haben — dieser Dienst legt nichts
        an, weil ein Modell es vorschlägt, sondern weil der Nutzer es gesagt hat.
        """
        if self.semantic is None:
            return RememberResult(False, "failed", reason="memory_unavailable")
        content = (intent.payload or "").strip()
        if not content:
            return RememberResult(False, "refused", reason="empty")
        await self.ensure_index()
        if looks_like_secret(content):
            # Ein Gedächtnis ist der falsche Ort für etwas, das wie ein Schlüssel aussieht.
            return RememberResult(False, "refused", reason="looks_like_secret")

        try:
            candidates = await self.semantic.hybrid_recall(content, limit=5)
        except Exception as exc:  # noqa: BLE001
            candidates = []
            self.last_error = f"{type(exc).__name__}: {exc}"

        # Entdopplung: derselbe Fakt noch einmal ausgesprochen legt keinen zweiten an.
        wanted = _dedup_key(content)
        for rec in candidates:
            if rec.superseded_by is None and _dedup_key(rec.content) == wanted:
                return RememberResult(True, "duplicate", memory_id=rec.id, content=rec.content)

        record = self._build_record(content, intent, conversation_id, source_message_id,
                                    source_session_id, source_turn_id, memory_type)

        scores: dict[str, float] = {}
        if intent.is_correction and self.semantic is not None:
            try:
                scores = dict(await self.semantic.semantic_hits(content, k=32))
            except Exception:  # noqa: BLE001 - ohne Semantik bleibt Stufe 1
                scores = {}
        target, why = (self._supersede_target(content, candidates, scores)
                       if intent.is_correction else (None, "not_a_correction"))
        if why == "ambiguous":
            # Nicht raten. Lieber nachfragen als den falschen Fakt loeschen.
            return RememberResult(False, "ambiguous", reason="ambiguous_correction",
                                  content=content)
        try:
            if target is not None:
                new = await self.semantic.supersede(target.id, record)
                indexed = self._is_indexed(new.id)
                if not indexed:
                    self.index_incomplete = True
                    self.reindex_pending = True
                    log.warning("memory.write_not_indexed", memory_id=new.id,
                                profile=self.provider.profile.key)
                return RememberResult(True, "superseded", memory_id=new.id,
                                      superseded_id=target.id, content=content,
                                      indexed=indexed)
            memory_id = await self.semantic.remember(record)
            indexed = self._is_indexed(memory_id)
            if not indexed:
                # Der Eintrag liegt dauerhaft im kanonischen Speicher, aber ohne Vektor.
                # Das ist kein Datenverlust — es macht ihn nur semantisch unsichtbar, und
                # es muss beim naechsten Zugriff wiederhergestellt werden.
                self.index_incomplete = True
                self.reindex_pending = True
                log.warning("memory.write_not_indexed", memory_id=memory_id,
                            profile=self.provider.profile.key)
            return RememberResult(True, "created", memory_id=memory_id, content=content,
                                  indexed=indexed)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return RememberResult(False, "failed", reason="store_error")

    def _build_record(self, content: str, intent: MemoryIntent, conversation_id: str,
                      message_id: str, session_id: str, turn_id: str,
                      memory_type: str) -> MemoryRecord:
        """Provenienz vollständig, aber ohne den Gesprächskörper zu verdoppeln.

        Gespeichert werden Bezeichner und der Satz, den der Nutzer gesagt hat — kein
        Audio, keine Rohframes, kein zweiter Abzug des Gesprächs.
        """
        now = self._now_dt()
        try:
            kind = MemoryType(memory_type) if memory_type else MemoryType.USER
        except ValueError:
            kind = MemoryType.USER                 # Vorschlag des Modells verworfen
        return MemoryRecord(
            id="", memory_type=kind, content=content, subject=_subject_of(content),
            source=f"conversation:{conversation_id}" if conversation_id else "voice",
            source_type=SourceType.USER_DIRECT,
            created_at=now, updated_at=now,
            trust_level=TrustLevel.USER_DIRECT,
            sensitivity=Sensitivity.PERSONAL,
            confidence=1.0, importance=0.7,
            provenance=[ProvenanceEntry(
                source_type=SourceType.USER_DIRECT,
                source=f"message:{message_id}" if message_id else "voice_turn",
                trust_level=TrustLevel.USER_DIRECT, at=now,
                note=f"explicit intent via {intent.marker!r}")],
            tags=["explicit"],
            metadata={"conversation_id": conversation_id,
                      "source_message_id": message_id,
                      "source_session_id": session_id,
                      "source_turn_id": turn_id,
                      "explicit_intent": True,
                      "intent_kind": intent.kind,
                      "intent_marker": intent.marker})

    @staticmethod
    def _supersede_target(content: str, candidates: list[MemoryRecord],
                          scores: dict[str, float] | None = None,
                          ) -> tuple[MemoryRecord | None, str]:
        """Welchen Eintrag löst diese Korrektur ab — oder keinen, oder ist es unklar?

        Gibt (Ziel, Grund) zurück. Ziel ist None, wenn nichts passt ODER wenn mehrere
        Kandidaten gleich gut passen: dann ist unklar, WAS korrigiert werden soll, und
        raten hiesse einen fremden Fakt zerstoeren.

        Bewertet wird ausschliesslich ueber WORTUEBERLAPPUNG. Die Zeichen-n-Gramme aus
        dem Abruf haben hier nichts zu suchen — sie sollen Schreibvarianten finden, nicht
        entscheiden, was geloescht wird.
        """
        scored = sorted(
            ((_word_similarity(content, rec.content), rec) for rec in candidates
             if rec.superseded_by is None),
            key=lambda pair: pair[0], reverse=True)
        if scored and scored[0][0] >= SUPERSEDE_MIN:
            if len(scored) > 1 and (scored[0][0] - scored[1][0]) < SUPERSEDE_MARGIN:
                return None, "ambiguous"
            return scored[0][1], "match"

        # Zweite Stufe: umformulierte Korrekturen. Nur mit semantischen Werten und nur
        # mit deutlichem Abstand — sonst wird nachgefragt, nicht geraten.
        if not scores:
            return None, "no_match"
        ranked = sorted(((scores.get(rec.id, 0.0), rec) for rec in candidates
                         if rec.superseded_by is None),
                        key=lambda pair: pair[0], reverse=True)
        if not ranked or ranked[0][0] < SUPERSEDE_AMBIGUITY_FLOOR:
            return None, "no_match"                  # nichts, was hierzu passt
        margin = ranked[0][0] - (ranked[1][0] if len(ranked) > 1 else 0.0)
        if ranked[0][0] >= SUPERSEDE_SEMANTIC_MIN and margin >= SUPERSEDE_SEMANTIC_MARGIN:
            return ranked[0][1], "semantic_match"
        return None, "ambiguous"

    # ----------------------------------------------------------------- Lesen
    async def search(self, query: str, *, top_k: int = DEFAULT_TOP_K,
                     max_chars: int = DEFAULT_RESULT_CHARS) -> list[MemoryHit]:
        """Begrenzte, nach Relevanz sortierte Treffer. Niemals der ganze Speicher.

        Superseierte und gelöschte Einträge kommen nicht zurück — das erledigt der
        Active-Truth-Filter des Fundaments, hier wird er nur nicht umgangen.
        """
        if self.semantic is None or not (query or "").strip():
            return []
        await self.ensure_index()
        try:
            records = await self.semantic.hybrid_recall(query, limit=max(top_k, 1) * 4)
            scored = dict(await self.semantic.semantic_hits(query, k=64))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            # Ein fehlgeschlagener Modell-Ladevorgang darf nicht als "gesund" durchgehen.
            self.model_load_error = self.last_error
            log.error("memory.embedding_unavailable", kind=type(exc).__name__)
            raise MemoryUnavailable(self.last_error) from exc
        # M2-11: das Fundament faengt einen Provider-Ausfall ab und faellt still auf FTS
        # zurueck (`_embed_query` gibt dann None). Diese Schicht sieht die Ausnahme also
        # nie — und meldete deshalb "gesund", obwohl das Modell gar nicht laden konnte.
        # Symptom: der semantische Arm ist leer, obwohl das aktive Profil Vektoren haelt.
        # Nur DANN wird einmal nachgefragt; im gesunden Fall kostet das nichts.
        if not scored and not self.model_load_error:
            await self._confirm_embedding_alive(query)
        elif scored:
            # Der semantische Arm hat geliefert — ein frueherer Ladefehler ist damit
            # ueberholt. Ihn stehen zu lassen hiesse, dauerhaft "Modell nicht verfuegbar"
            # zu melden, obwohl es laengst wieder arbeitet.
            self.model_load_error = ""

        hits: list[MemoryHit] = []
        used = 0
        position = 0
        for rec in records:
            if rec.superseded_by is not None:
                continue
            # Relevanzschranke: geteiltes Inhaltswort ODER semantische Naehe. Ohne sie
            # antwortet die Suche auf jede Frage mit dem gesamten Bestand.
            if not (self.bounds.accepts(scored.get(rec.id, 0.0),
                                        _similarity(query, rec.content))
                    or _covers(query, rec.content)):
                # Der lexikalische Arm darf nicht an einer fuer SAETZE kalibrierten
                # Schwelle scheitern: die Anfrage "WBK4471" stand als Kandidat auf
                # Position 0 und wurde trotzdem verworfen, weil ihre Wortueberlappung
                # mit dem vollen Satz nur 0.333 betrug.
                continue
            position += 1
            if used + len(rec.content) > max_chars:
                break                              # Budget: ältestes/schwächstes fällt weg
            hits.append(MemoryHit(
                memory_id=rec.id, content=rec.content,
                memory_type=rec.memory_type.value,
                created_at=rec.created_at.isoformat(),
                relevance=position,
                metadata={"conversation_id": rec.metadata.get("conversation_id", ""),
                          "explicit_intent": bool(rec.metadata.get("explicit_intent")),
                          "state": "active"}))
            used += len(rec.content)
            if len(hits) >= top_k:
                break
        return hits

    # ----------------------------------------------------------------- Löschen
    async def delete(self, memory_id: str, *, reason: str = "user_request") -> bool:
        """Endgültig entfernen — samt Semantik-Index, damit die Suche keine Geister findet."""
        if self.semantic is None:
            raise MemoryUnavailable("memory unavailable")
        return await self.semantic.purge(memory_id, reason=reason)

    async def health(self) -> dict:
        if self.semantic is None:
            return {"available": False, "error": self.last_error or "not open"}
        try:
            info = await self.semantic.health()
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": f"{type(exc).__name__}"}
        info["available"] = True
        info["base_dir"] = self.base_dir
        # Ausdruecklich: laeuft der Produktivpfad oder der Rueckfall? Gemessen liefert der
        # Hashing-Provider auf demselben deutschen Korpus 23/26 Treffer bei 8 Falsch-
        # Positiven, Qwen 26/26 bei 1. "Semantisches Gedaechtnis gesund" zu melden, waehrend
        # in Wahrheit der Rueckfall laeuft, waere eine Luege.
        provider_id = self.provider.profile.provider_id
        degraded = provider_id != "qwen-local"
        info["provider_id"] = provider_id
        info["degraded"] = degraded
        info["retrieval_quality"] = "degraded_lexical" if degraded else "semantic"
        # M2-11: "gesund" ist zu grob. Vorher meldete health `degraded=false`, auch wenn
        # das Modell in Wahrheit gar nicht laden KONNTE — die Verfuegbarkeitspruefung
        # sieht nur das Paket, nicht das Modell. Fuenf unterscheidbare Zustaende:
        loaded = bool(info.get("provider", {}).get("loaded"))
        if self.model_load_error:
            state = "embedding_unavailable"
            # Die alten Felder duerfen dem ausdruecklichen Zustand nicht
            # widersprechen: ein ausgefallenes Modell liefert keine Semantik.
            info["degraded"] = True
            info["retrieval_quality"] = "unavailable"
        elif degraded:
            state = "degraded_fallback"
        elif self.reindex_pending:
            state = "reindex_pending"
        elif self.index_incomplete:
            state = "index_incomplete"
        elif loaded:
            state = "healthy"
        else:
            state = "healthy_not_yet_loaded"     # faul, noch nicht geladen — kein Fehler
        info["state"] = state
        info["model_loaded"] = loaded
        if self.model_load_error:
            info["model_load_error"] = self.model_load_error
        info["index_incomplete"] = self.index_incomplete
        info["bounds"] = {"min_cosine": self.bounds.min_cosine,
                          "min_lexical": self.bounds.min_lexical}
        info["reindex_pending"] = self.reindex_pending
        if self.previous_profile:
            info["previous_profile"] = self.previous_profile
        return info
