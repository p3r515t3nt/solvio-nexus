"""Die unabhaengige Abnahme — gemessen ausserhalb dessen, was der Builder aendern kann.

**Der gemessene Befund, aus dem dieses Modul entstand (DEBT-0234).** Das Gate
des Autopiloten laeuft als `scripts/run_tests.py` **aus dem Arbeitsbereich**
(`evidence.run_gate`, `cwd=repo`). Der Kaefig erlaubt dem Builder Schreiben im
ganzen Arbeitsbereich (`isolation.py`: eine Regel, keine Ausnahme), und der
einzige Filter danach — `builders._scan_staged` — sucht Kredentialgestalt, nicht
geschuetzte Pfade. Der Builder darf also sein eigenes Examen schreiben, und
`test_report.ok` ist genau das Signal, das ueber `READY` entscheidet.

Das ist nicht theoretisch. Im echten Lauf `gap-1f983ef8cf3ed38c` aenderte der
Checkpoint `b5e5358a` die Datei `scripts/run_tests.py` — **verschaerfend**, aber
derselbe Weg haette die vorhandenen Pruefungen loeschen koennen, und nichts im
Pfad haette es bemerkt. Alle drei produktiven Builder-Commits aenderten
ausserdem `tests/test_inventory_baseline.json`; `update_test_baseline.py` sagt
ueber sich selbst „review assurance, not a cryptographic control".

**Warum ein Diff des Gate-Quelltexts nicht genuegt.** Ein Vergleich sagt nur,
DASS jemand das Examen angefasst hat — nie, ob das Ergebnis stimmt. Er ist eine
Sichtbarkeitsmassnahme; wer sie hat, hat noch keine Messung. Deshalb tut dieses
Modul zwei Dinge, und das zweite ist das eigentliche:

1. **Die Pruefinfrastruktur muss unveraendert sein.** Wurde eine bestehende
   Testdatei oder das Gate selbst geaendert oder geloescht, verweigert die
   unabhaengige Abnahme — sie misst dann gar nicht erst. Neue Testdateien sind
   erlaubt, wandern aber NICHT in den Pruefbaum: wer geprueft wird, stellt die
   Pruefung nicht.
2. **Gemessen wird in einem Baum, den der Builder nie beruehrt hat.** Der
   Basis-Commit der ERSTEN Bauphase, vollstaendig aus dem Objektspeicher
   ausgepackt, und darueber nur die geaenderten Nicht-Pruefpfade des Ergebnisses.
   Aussage: **der neue Code besteht die urspruenglichen Tests.**

**Was diese Abnahme ausdruecklich NICHT beweist.** Die Basis-Tests kennen die
neue Faehigkeit nicht — der Lauf zeigt „nichts Altes kaputt", nie „das Neue
funktioniert". Das bleibt Sache der Live-Abnahme und der Kriterien, die ein
Mensch prueft. Und ein Builder, der seine EIGENEN neuen Tests weich schreibt,
faellt hier nicht auf; er gewinnt damit aber auch nichts, weil seine Tests im
unabhaengigen Baum nicht laufen.

Die Basis kommt aus der **Phasentabelle**, nicht aus `milestones.base_commit`:
`driver` ueberschreibt Letzteres in jeder Baurunde mit dem aktuellen
Arbeitsbereichskopf, sodass die Milestone-Basis ab Runde zwei bereits
builder-beeinflusst ist. Die erste Bauphase behaelt die unberuehrte.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile

from solvio.autopilot import evidence as EV
from solvio.logging_setup import get_logger

log = get_logger("autopilot")

#: Die Pruefinfrastruktur. Wer hier schreibt, aendert sein eigenes Examen.
#:
#: Ein Praefix mit `/` meint einen Baum, ein Eintrag ohne eine Datei. Die Liste
#: ist bewusst kurz und benennbar; sie deckt, was `run_tests.py` zum Messen
#: benutzt: die Suiten selbst, den Laeufer, den Arbeiter, das Inventar und die
#: Baseline (die als einzige Kontrolle bemerkt, dass Tests verschwunden sind).
EXAMINATION_PATHS = ("tests/", "scripts/run_tests.py", "scripts/_test_worker.py",
                     "scripts/update_test_baseline.py")

#: Wie viele geaenderte Pfade hoechstens in den Pruefbaum wandern. Ein Bau, der
#: mehr anfasst, ist keiner mehr, den diese Abnahme sinnvoll pruefen kann —
#: dann wird verweigert statt geschoent.
MAX_OVERLAY_PATHS = 400

EVIDENCE_KIND = "independent_gate"


class IndependentError(RuntimeError):
    """Die unabhaengige Abnahme konnte nicht stattfinden. Der Grund ist die
    Aussage — nicht eine Randnotiz."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def is_examination(pfad: str) -> bool:
    """Gehoert dieser Pfad zur Pruefinfrastruktur?"""
    p = pfad.strip().lstrip("./")
    return any(p.startswith(e) if e.endswith("/") else p == e
               for e in EXAMINATION_PATHS)


def original_base(ledger, milestone_id: str) -> str:
    """Der Basis-Commit der ERSTEN Bauphase — die unberuehrte Basis.

    `milestones.base_commit` taugt dafuer nicht: der Treiber setzt es vor jeder
    Baurunde auf den aktuellen Arbeitsbereichskopf, also ab Runde zwei auf
    einen Stand, den der Builder selbst erzeugt hat. Genau diese Verwechslung
    haette die Abnahme leise wertlos gemacht.
    """
    bauphasen = [p for p in ledger.phases(milestone_id, limit=200)
                 if p.get("kind") == "build" and p.get("base_commit")]
    if not bauphasen:
        raise IndependentError("no_base_phase", milestone_id)
    return str(min(bauphasen, key=lambda p: int(p["seq"]))["base_commit"])


def classify(repo: str, base: str, head: str = "HEAD") -> dict:
    """Was sich geaendert hat, getrennt nach Examen und Code.

    `--diff-filter` unterscheidet dabei ausdruecklich: eine HINZUGEFUEGTE
    Testdatei ist erlaubt (sie laeuft nur nicht mit), eine GEAENDERTE oder
    GELOESCHTE nicht. Ohne diese Unterscheidung koennte kein Bau je eine neue
    Faehigkeit mit eigenen Tests liefern.
    """
    def _pfade(filter_: str) -> list[str]:
        proc = EV._git(repo, "diff", "--name-only", f"--diff-filter={filter_}",
                       f"{base}..{head}", check=False)
        return [z.strip() for z in proc.stdout.split("\n") if z.strip()]

    geaendert = _pfade("M")
    geloescht = _pfade("D")
    hinzugefuegt = _pfade("A")
    # **Geaendert und geloescht bleiben getrennt.** Beim Examen zaehlen beide
    # als Eingriff; beim Code nicht: ein geloeschter Pfad darf nicht in die
    # Uebernahmeliste, sonst scheitert `git archive` an einem Pfad, den es im
    # Ergebnis nicht mehr gibt — und die ganze Abnahme faellt aus, statt zu
    # messen. Eine Gegenprobe hat genau das gefunden.
    eingriff = geaendert + geloescht
    return {
        "examination_touched": sorted(p for p in eingriff if is_examination(p)),
        "examination_added": sorted(p for p in hinzugefuegt if is_examination(p)),
        "code_changed": sorted(p for p in geaendert if not is_examination(p)),
        "code_deleted": sorted(p for p in geloescht if not is_examination(p)),
        "code_added": sorted(p for p in hinzugefuegt if not is_examination(p)),
    }


def _auspacken(repo: str, commit: str, ziel: str, pfade: list[str] | None = None) -> None:
    """Einen Commit (oder Teile) in ein Verzeichnis auspacken — ohne Arbeitsbaum.

    `git archive` liest aus dem Objektspeicher. Der Arbeitsbereich des Builders
    wird dabei nicht angefasst und kann das Ergebnis auch nicht beeinflussen.
    """
    args = ["archive", "--format=tar", commit]
    if pfade:
        args += ["--", *pfade]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    from solvio import git_binary as _GB
    archiv = subprocess.run([_GB.resolve(), "-C", repo, *args], env=env,
                            capture_output=True, timeout=300.0)
    if archiv.returncode != 0:
        raise IndependentError(
            "archive_failed", archiv.stderr.decode("utf-8", "replace")[:200])
    entpacken = subprocess.run(["tar", "-x", "-C", ziel], input=archiv.stdout,
                               capture_output=True, timeout=300.0)
    if entpacken.returncode != 0:
        raise IndependentError(
            "extract_failed", entpacken.stderr.decode("utf-8", "replace")[:200])


def build_tree(repo: str, *, base: str, head: str, ziel: str,
               lage: dict | None = None) -> dict:
    """Der Pruefbaum: unberuehrte Basis, darueber nur der neue Nicht-Pruefcode.

    Verweigert, bevor irgendetwas gemessen wird, wenn das Examen angefasst
    wurde. Das ist die fail-closed-Richtung: keine Messung ist ein ehrliches
    Ergebnis, eine Messung mit dem Examen des Geprueften waere keines.
    """
    befund = lage if lage is not None else classify(repo, base, head)
    if befund["examination_touched"]:
        raise IndependentError("examination_modified",
                               ", ".join(befund["examination_touched"][:10]))
    uebernehmen = befund["code_changed"] + befund["code_added"]
    if len(uebernehmen) > MAX_OVERLAY_PATHS:
        raise IndependentError("too_many_paths", str(len(uebernehmen)))

    os.makedirs(ziel, mode=0o700, exist_ok=True)
    _auspacken(repo, base, ziel)

    # Geloeschter Code muss auch in der Kopie verschwinden — sonst pruefte die
    # Abnahme einen Baum, den es nicht gibt.
    for pfad in befund["code_deleted"]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(ziel, pfad))

    if uebernehmen:
        _auspacken(repo, head, ziel, uebernehmen)
    return befund


def _scratch_root(ledger) -> str:
    """Wo der Pruefbaum entsteht — NEBEN dem Buch, das die Messung bucht.

    **Nicht fest auf `~/.solvio`.** Die erste Fassung tat das, und eine Suite,
    die ihren `scratch` nicht mitgab, legte damit ein Verzeichnis im
    PRODUKTIVEN Zustand an — gefunden vom ersten Full Gate, nicht von mir
    (`test_inventory_reconciliation`: ein unbekannter Eintrag in `~/.solvio`).
    Das ist dieselbe Klasse wie DEBT-0223.

    Der Ordner folgt jetzt dem Buch: wer `SOLVIO_AUTOPILOT_DB` umleitet, leitet
    den Pruefbaum mit um, ohne an einen zweiten Schalter denken zu muessen.
    Ein vergessener Parameter darf nicht in Produktion schreiben.
    """
    buchpfad = getattr(ledger, "path", "")
    if buchpfad:
        return os.path.join(os.path.dirname(os.path.abspath(buchpfad)),
                            "independent")
    return os.path.expanduser("~/.solvio/independent")


def measure(ledger, milestone_id: str, repo: str, *, base: str = "",
            head: str = "", python: str = "", scratch: str = "",
            now: float = 0.0) -> tuple[str, object, dict]:
    """Die unabhaengige Abnahme fahren und buchen.

    Rueckgabe `(evidence_id, GateResult|None, befund)`. Eine Verweigerung wird
    ebenso gebucht wie eine Messung — mit `ok=False` und ihrem Grund. **Eine
    nicht stattgefundene Abnahme darf nie wie eine bestandene aussehen.**
    """
    stand = head or EV.head_commit(repo)
    try:
        basis = base or original_base(ledger, milestone_id)
    except IndependentError as exc:
        # Auch das wird GEBUCHT. Eine Abnahme, die mangels Basis nicht
        # stattfand, muss im Buch stehen — sonst sieht sie spaeter aus wie
        # eine, die es nicht gab, weil niemand sie brauchte.
        beleg = ledger.record_evidence(
            milestone_id, kind=EVIDENCE_KIND, commit=stand, env_fingerprint="",
            ok=False, summary=f"unabhaengige Abnahme unmoeglich: {exc.reason}",
            payload={"head": stand, "reason": exc.reason, "detail": exc.detail},
            now=now)
        log.warning("autopilot.independent_impossible", milestone=milestone_id,
                    reason=exc.reason)
        return beleg, None, {"reason": exc.reason, "detail": exc.detail}
    wurzel = scratch or _scratch_root(ledger)
    os.makedirs(wurzel, mode=0o700, exist_ok=True)
    ordner = tempfile.mkdtemp(prefix=f"{milestone_id[:24]}-", dir=wurzel)
    try:
        try:
            befund = build_tree(repo, base=basis, head=stand, ziel=ordner)
        except IndependentError as exc:
            befund = {"reason": exc.reason, "detail": exc.detail}
            beleg = ledger.record_evidence(
                milestone_id, kind=EVIDENCE_KIND, commit=stand,
                env_fingerprint="", ok=False,
                summary=f"unabhaengige Abnahme verweigert: {exc.reason}",
                payload={"base": basis, "head": stand, **befund}, now=now)
            log.warning("autopilot.independent_refused", milestone=milestone_id,
                        reason=exc.reason)
            return beleg, None, befund

        ergebnis = EV.run_gate(ordner, python=python)
        beleg = ledger.record_evidence(
            milestone_id, kind=EVIDENCE_KIND, commit=stand,
            env_fingerprint=EV.environment_fingerprint(ordner, python=python),
            ok=ergebnis.ok,
            summary=f"unabhaengig gegen {basis[:12]}: {ergebnis.summary()}",
            payload={"base": basis, "head": stand,
                     "code_paths": (befund["code_changed"]
                                    + befund["code_added"])[:200],
                     "examination_added": befund["examination_added"][:50],
                     **ergebnis.as_dict()}, now=now)
        log.info("autopilot.independent_measured", milestone=milestone_id,
                 ok=ergebnis.ok, base=basis[:12])
        return beleg, ergebnis, befund
    finally:
        shutil.rmtree(ordner, ignore_errors=True)
