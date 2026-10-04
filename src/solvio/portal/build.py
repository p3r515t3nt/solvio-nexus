"""Welchen Code der Arbeiter wirklich ausfuehrt.

Der Arbeiter lebt hinter einer Benutzergrenze und laeuft aus einer **Kopie**. Das
ist der Preis der Trennung, und er hat eine Nebenwirkung, die man erst bemerkt,
wenn es darauf ankommt: eine Korrektur an `browser/` oder `portal/` erreicht ihn
nicht, bevor jemand sie ausliefert. Solange nur eine Uebungsseite dranhing, war
das laestig. Sobald ein echter Zugang dranhaengt, ist es die falsche Art von
Ueberraschung — der Core glaubt, eine Policy sei repariert, und der Prozess, der
sie durchsetzen soll, kennt sie nicht.

Also bekommt die Auslieferung eine Identitaet. Der Bauzustand ist der Hash ueber
die sortierte Liste (Pfad, Inhaltshash) genau der Dateien, die der Arbeiter
ausfuehrt. Beim Handschlag nennt er ihn; der Core vergleicht ihn mit dem, was
sein eigener Quellbaum ergaebe. Weichen sie ab, beginnt **keine** Sitzung.

Drei Faelle, ein Verhalten:

* **Veraltet** — der Arbeiter laeuft aus einer aelteren Kopie.
* **Manipuliert** — jemand hat eine Datei in der Kopie veraendert.
* **Unvollstaendig** — eine Auslieferung brach ab.

Alle drei ergeben einen anderen Hash, und alle drei enden gleich: fail-closed.
Es gibt keinen Weg, den Vergleich zu uebergehen; der Bauzustand wird aus Dateien
gerechnet, nicht aus einer Datei gelesen, die man mitfaelschen koennte.
"""
from __future__ import annotations

import hashlib
import os

#: Genau die Dateien, die der Arbeiter ausfuehrt. Eine Liste, kein Verzeichnis:
#: was er nicht braucht, bekommt er nicht, und was er bekommt, ist gezaehlt.
#: `vault.py` und `client.py` stehen ausdruecklich nicht darin.
MODULES: tuple[str, ...] = (
    "solvio/__init__.py",
    "solvio/logging_setup.py",
    # Die zentrale Redaktion. Sie MUSS mit, seit `logging_setup` sie importiert
    # — ohne sie startet der Arbeiter nicht, und genau daran ist die erste
    # Auslieferung des Tresors gescheitert. Sie ist dafuer auch die richtige
    # Datei fuer diesen Baum: reines `re` und `logging`, keine Abhaengigkeit
    # nach draussen, und der Arbeiter ist der Prozess, der einem Geheimnis am
    # naechsten kommt.
    "solvio/redaction.py",
    "solvio/browser/__init__.py",
    "solvio/browser/cdp.py",
    "solvio/browser/js.py",
    "solvio/browser/page.py",
    "solvio/browser/policy.py",
    "solvio/portal/__init__.py",
    "solvio/portal/binding.py",
    "solvio/portal/build.py",
    "solvio/portal/manifest.py",
    "solvio/portal/permit.py",
    "solvio/portal/protocol.py",
    "solvio/portal/redact.py",
    "solvio/portal/service.py",
)

#: Dateien, die im Arbeiterbaum nichts zu suchen haben. Wird nach dem Kopieren
#: geprueft — eine Liste, die niemand kontrolliert, ist eine Absichtserklaerung.
FORBIDDEN = ("vault.py", "client.py", "config.py", ".env", "capabilities",
             "security", "memory", "realtime", "integrations", "deep")

BUILD_UNKNOWN = "unknown"


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_build(root: str, modules: tuple[str, ...] = MODULES) -> tuple[str, dict[str, str]]:
    """Der Bauzustand eines Baums. Fehlt eine Datei, faellt das auf.

    Eine fehlende Datei ergibt bewusst KEINEN Fehler, sondern einen eigenen
    Eintrag — sonst koennte eine abgebrochene Auslieferung als „Ausnahme beim
    Rechnen" durchgehen, und ein Ausnahmefall wird schneller weggefangen als ein
    abweichender Hash.
    """
    parts: dict[str, str] = {}
    for relative in sorted(modules):
        path = os.path.join(root, relative)
        try:
            parts[relative] = file_digest(path)
        except OSError:
            parts[relative] = "missing"
    payload = "\n".join(f"{name}:{digest}" for name, digest in sorted(parts.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32], parts


def expected_build(repo_root: str) -> str:
    """Was der Quellbaum des Core ergaebe — die Erwartung."""
    build, _parts = compute_build(os.path.join(repo_root, "src"))
    return build


def installed_build(app_root: str) -> str:
    """Was tatsaechlich installiert ist — die Wirklichkeit."""
    build, _parts = compute_build(app_root)
    return build


def differences(repo_root: str, app_root: str) -> list[str]:
    """Welche Dateien abweichen. Fuer die Fehlermeldung, nicht fuer die Regel."""
    _expected, mine = compute_build(os.path.join(repo_root, "src"))
    _actual, theirs = compute_build(app_root)
    return sorted(name for name in mine if mine[name] != theirs.get(name, "missing"))
