"""Der Sicherungslauf — als eigener Prozess, absichtlich.

Der Zeitplaner des Cores kann das hier nicht leisten, und das ist gemessen und
nicht vermutet: er laeuft IN-Prozess als asyncio-Schleife, er stirbt also mit
dem Core. Und seine Aktionsliste (`ALLOWED_ACTIONS`) ist eine geschlossene Liste
aus Kalender, Gmail und Home Assistant — eine Sicherung steht nicht darauf und
soll auch nicht daraufkommen.

Also: ein eigener `launchd`-Job unter dem Benutzer, ohne root. Er braucht den
Core nicht. Ein toter Core ist der Zeitpunkt, an dem eine Sicherung am meisten
wert ist — sie darf nicht mit ihm sterben.

Was der Lauf tut, in dieser Reihenfolge:

1. **Messen.** Ist die Platte da, ist sie es wirklich, ist sie verschluesselt?
2. **Sichern**, wenn ja. Sonst sauber ueberspringen — das ist kein Fehler.
3. **Home Assistant abholen**, wenn erreichbar. Fehlschlag kippt den Satz nicht.
4. **Aufraeumen** nach der Aufbewahrungsregel.
5. **Nachpruefen** — Pruefsummen des juengsten Satzes.
6. **Buch fuehren**, intern, damit „wann lief die letzte Sicherung" auch ohne
   Platte zu beantworten ist.
7. **Sprechen, nur wenn noetig.** Und dann hoechstens einmal am Tag.

Der siebte Punkt ist der, an dem Ueberwachungen sonst scheitern. Die Regel hier
ist dieselbe wie beim Arzt: gemeldet wird, was der Mensch wissen MUSS. Eine
gesunde Sicherung sagt gar nichts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from solvio.storage import engine, health, inventory, volume
from solvio.storage.engine import BackupLocked, BackupResult, _log, list_sets
from solvio.storage.restore import verify_set
from solvio.storage.volume import StorageUnavailable, StorageUntrusted

#: Aufbewahrung. Gemessen, nicht geraten: ein Satz kostet nach Abzug der
#: Hardlinks nur das, was sich geaendert hat — im Wesentlichen die
#: SQLite-Schnappschuesse und die taegliche HA-Sicherung.
KEEP_DAILY = 14
KEEP_WEEKLY = 8
KEEP_MONTHLY = 12

#: Wie lange nach einer Meldung ueber denselben Zustand geschwiegen wird.
#: Der Arzt nimmt sechs Stunden; eine Sicherung laeuft taeglich, also passt ein
#: Tag. Sonst meldet dieselbe fehlende Platte viermal, bevor sich etwas aendern
#: konnte.
QUIET_SECONDS = 24 * 3600.0

#: Mindestabstand zwischen zwei ungenannten Laeufen. Der launchd-Job zuendet
#: nicht nur nach Uhrzeit, sondern auch bei JEDEM Einhaengen eines Volumes
#: (`StartOnMount`) — das ist der Weg, auf dem die Sicherung nach dem Wiederan-
#: stecken von selbst weiterlaeuft. Ohne diese Bremse wuerde jedes eingehaengte
#: Disk-Image eine Sicherung ausloesen.
MIN_INTERVAL_SECONDS = 6 * 3600.0

#: Wie viel des juengsten Satzes je Lauf nachgerechnet wird. `full` bei jedem
#: Lauf waere ehrlicher und teurer; `latest` faengt schleichenden Verfall im
#: Neuen, und der Rest wird bei der Wiederherstellungsprobe geprueft.
VERIFY_LATEST = True


# ------------------------------------------------------------------- Aufbewahrung
def _set_datetime(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def plan_retention(names: list[str], *, daily: int = KEEP_DAILY,
                   weekly: int = KEEP_WEEKLY,
                   monthly: int = KEEP_MONTHLY) -> tuple[list[str], list[str]]:
    """(behalten, verwerfen) nach Gross-Vater/Vater/Sohn.

    Die Regel ist die klassische, und sie hat einen Grund, der nichts mit Platz
    zu tun hat: eine Beschaedigung, die erst nach zwei Wochen auffaellt, ist mit
    vierzehn Tagessicherungen nicht zu heilen. Deshalb reichen die Wochen- und
    Monatsstuetzen weiter zurueck, als der Alltag braucht.
    """
    dated = sorted(((n, _set_datetime(n)) for n in names if _set_datetime(n)),
                   key=lambda p: p[1], reverse=True)      # type: ignore[arg-type]
    keep: "OrderedDict[str, str]" = OrderedDict()
    for name, _ in dated[:daily]:
        keep[name] = "taeglich"
    seen_weeks: set[tuple[int, int]] = set()
    for name, dt in dated:
        key = dt.isocalendar()[:2]                        # type: ignore[union-attr]
        if key in seen_weeks:
            continue
        seen_weeks.add(key)
        if len(seen_weeks) <= weekly:
            keep.setdefault(name, "woechentlich")
    seen_months: set[tuple[int, int]] = set()
    for name, dt in dated:
        key = (dt.year, dt.month)                         # type: ignore[union-attr]
        if key in seen_months:
            continue
        seen_months.add(key)
        if len(seen_months) <= monthly:
            keep.setdefault(name, "monatlich")
    # Ein ausdruecklich benannter Satz (Release-Punkt) ueberlebt jede Regel;
    # das entscheidet der Aufrufer ueber `protected`, siehe `prune`.
    drop = [n for n, _ in dated if n not in keep]
    return list(keep), drop


def _protected(sets_dir: str, name: str) -> bool:
    """Ein Satz mit Namen (Release-Punkt) wird nie automatisch verworfen."""
    try:
        with open(os.path.join(sets_dir, name, engine.MANIFEST_NAME),
                  encoding="utf-8") as fh:
            return bool(json.load(fh).get("label"))
    except (OSError, ValueError):
        # Ein Satz ohne lesbares Manifest ist kaputt — er darf weg.
        return False


def prune(sets_dir: str, **limits: int) -> dict[str, Any]:
    names = list_sets(sets_dir)
    keep, drop = plan_retention(names, **limits)
    removed, kept_by_label = [], []
    for name in drop:
        if _protected(sets_dir, name):
            kept_by_label.append(name)
            continue
        shutil.rmtree(os.path.join(sets_dir, name), ignore_errors=True)
        removed.append(name)
    return {"kept": len(keep) + len(kept_by_label), "removed": removed,
            "kept_by_label": kept_by_label}


# ------------------------------------------------------------------------ Melden
async def notify(summary: str, findings: list[str], *, kind: str,
                 now: float | None = None, store: Any = None) -> bool:
    """Legt eine Meldung in den bestehenden proaktiven Eingang.

    Kein zweiter Posteingang. Der Sicherungslauf ist ein fremder Prozess, aber
    er PLANT nichts — er BERICHTET. Genau diese Trennung ist in ADR-0023
    festgehalten.

    Zwei Fallen, beide gemessen und beide vom Arzt uebernommen: `task_id` ist
    der LEERSTRING und nicht `None` (SQLite haelt NULL in einer UNIQUE-Bedingung
    fuer verschieden, die Entdopplung greift sonst nie), und das Ruhefenster
    steckt IM Fingerabdruck, damit dieselbe Lage morgen wieder gemeldet werden
    kann, heute aber nicht viermal.
    """
    now = now if now is not None else time.time()
    if store is None:
        from solvio.proactive.store import ProactiveStore
        store = ProactiveStore()
    window = int(now // QUIET_SECONDS)
    item = {
        "notification_id": "st-" + hashlib.sha256(
            f"{kind}|{window}".encode()).hexdigest()[:20],
        "task_id": "", "run_id": None, "created_at": now,
        "priority": "wichtig", "summary": summary[:1200], "findings": findings,
        "source_capability": "storage", "content_trust": "",
        "fingerprint": f"storage:{kind}:{window}",
    }
    return bool(await store.add_item(item))


#: Welcher Zustand welche Meldung ausloest. `healthy` fehlt mit Absicht.
_MESSAGES = {
    "auth_required": ("Die Speicherplatte ist angeschlossen, aber gesperrt. "
                      "Ich kann nicht sichern, bis sie im Finder mit der "
                      "Passphrase geoeffnet wurde.", "locked"),
    "unavailable": ("Mit der Speicherplatte stimmt etwas nicht — ich schreibe "
                    "nichts Privates darauf.", "untrusted"),
    "degraded": ("Die Sicherung ist nicht auf dem Stand, auf dem sie sein "
                 "sollte.", "stale"),
}


async def maybe_notify(state: str, reason: str, *, extra: list[str] | None = None,
                       now: float | None = None, store: Any = None) -> bool:
    if state in ("healthy", "unknown"):
        return False
    template, kind = _MESSAGES.get(state, ("Die Sicherung braucht dich.", state))
    return await notify(f"{template} ({reason})", (extra or []) + [reason],
                        kind=kind, now=now, store=store)


# -------------------------------------------------------------------------- Lauf
async def run_once(*, label: str = "", include_ha: bool = True,
                   include_repos: bool = True, force: bool = False,
                   min_interval: float = MIN_INTERVAL_SECONDS,
                   notify_store: Any = None) -> dict[str, Any]:
    """Ein vollstaendiger Lauf. Wirft nie — er berichtet."""
    now = time.time()
    state = engine.load_state()
    state["last_attempt_at"] = now
    outcome: dict[str, Any] = {"attempted_at": now, "label": label}

    last_ok = float(state.get("last_success_at") or 0.0)
    if not force and not label and last_ok and (now - last_ok) < min_interval:
        age = int((now - last_ok) // 60)
        outcome.update({"ok": None, "skipped": True,
                        "reason": f"letzte Sicherung vor {age} Minuten"})
        # Auch ein uebersprungener Lauf gehoert ins Log. Sonst sieht ein Tag,
        # an dem `StartOnMount` zwanzigmal gebremst hat, aus wie ein Tag, an
        # dem der Job gar nicht existierte.
        _log(f"skipped: letzte Sicherung vor {age} Minuten")
        return outcome

    try:
        cfg = volume.load_config()
    except Exception as exc:                              # noqa: BLE001
        cfg = None
        outcome["config_error"] = str(exc)
    vol = volume.probe(cfg) if cfg else volume.probe()

    if cfg is None or not vol.usable:
        # Kein Fehler. Die Platte darf fehlen; nur die Buchhaltung muss stimmen.
        reason = "; ".join(vol.problems) or "nicht eingerichtet"
        outcome.update({"ok": None, "skipped": True, "reason": reason})
        engine.save_state(state)
        _log(f"skipped: {reason}")
        hs, hr = health.assess(health.collect(now=now, state=vol, backup_state=state))
        outcome["health"] = [hs, hr]
        outcome["notified"] = await maybe_notify(hs, hr, now=now, store=notify_store)
        return outcome

    try:
        # In einem eigenen Thread, und zwar aus einem konkreten Grund: die
        # Maschine ist synchron und soll es bleiben (eine Sicherung, die einen
        # Eventloop braucht, ist eine, die man im Notfall nicht von Hand starten
        # kann). Der Schritt fuer Home Assistant braucht aber ein `await`. Ein
        # `asyncio.run` INNERHALB des schon laufenden Loops wirft — genau daran
        # ist der zweite Probelauf gescheitert. Ein Thread hat keinen laufenden
        # Loop, also darf er dort einen aufmachen.
        result = await asyncio.to_thread(_run_with_ha, label=label,
                                         include_repos=include_repos,
                                         include_ha=include_ha)
    except BackupLocked as exc:
        outcome.update({"ok": None, "skipped": True, "reason": str(exc)})
        return outcome
    except (StorageUnavailable, StorageUntrusted) as exc:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["last_error"] = str(exc)
        engine.save_state(state)
        _log(f"failed: {exc}")
        outcome.update({"ok": False, "reason": str(exc)})
        hs, hr = health.assess(health.collect(now=now, state=vol, backup_state=state))
        outcome["notified"] = await maybe_notify(hs, hr, now=now, store=notify_store)
        return outcome
    except Exception as exc:                              # noqa: BLE001
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        engine.save_state(state)
        _log(f"failed: {type(exc).__name__}")
        outcome.update({"ok": False, "reason": str(exc)})
        return outcome

    root = volume.storage_root(vol, cfg)
    sets_dir = os.path.join(root, "Backups", "sets")
    outcome["backup_id"] = result.backup_id
    outcome["ok"] = result.ok
    outcome["total_bytes"] = result.total_bytes
    outcome["linked_bytes"] = result.linked_bytes
    outcome["duration_seconds"] = result.duration_seconds
    outcome["errors"] = result.errors
    outcome["skipped_items"] = result.skipped

    outcome["retention"] = prune(sets_dir)

    if VERIFY_LATEST and result.path:
        outcome["verify"] = verify_set(result.path)
        if not outcome["verify"]["ok"]:
            result.errors.append("Pruefsummen des neuen Satzes stimmen nicht")
            outcome["ok"] = False

    if outcome["ok"]:
        state["last_success_at"] = time.time()
        state["last_backup_id"] = result.backup_id
        state["last_error"] = None
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["last_error"] = "; ".join(result.errors)[:400]
    state["last_duration_seconds"] = result.duration_seconds
    state["last_total_bytes"] = result.total_bytes
    engine.save_state(state)

    vol_after = volume.probe(cfg)
    hs, hr = health.assess(health.collect(state=vol_after, backup_state=state))
    outcome["health"] = [hs, hr]
    outcome["notified"] = await maybe_notify(
        hs, hr, extra=result.errors[:3], store=notify_store)
    return outcome


def _run_with_ha(*, label: str, include_repos: bool, include_ha: bool) -> BackupResult:
    """Fuehrt die synchrone Maschine aus und haengt HA als Schritt an."""
    steps = []
    if include_ha:
        steps.append(_ha_step)
    return engine.run_backup(label=label, include_repos=include_repos,
                             extra_steps=steps)


def _ha_step(incoming: str, linker: Any) -> dict[str, Any] | None:
    """Holt HAs eigene Sicherung. Ein Fehlschlag kippt den Satz nicht.

    Home Assistant ist eine Faehigkeitsschicht, nicht SOLVIOs Kern. Wenn die VM
    gerade neu startet, ist das eine Notiz, kein Ausfall der Sicherung.

    Die Konfiguration kommt AUSDRUECKLICH aus dem produktiven Arbeitsbaum und
    nicht aus dem Verzeichnis, aus dem dieses Modul zufaellig geladen wurde.
    Beim ersten echten Lauf lag der Code in einem git-Worktree, dort gibt es
    keine `.env` — und die Sicherung liess Home Assistant lautlos weg. Gesichert
    wird die PRODUKTION, also wird auch ihre Konfiguration gelesen.
    """
    import os as _os

    from solvio.config import Settings
    env_file = _os.path.join(inventory.CORE_REPO, ".env")
    settings = Settings(_env_file=env_file)               # type: ignore[call-arg]
    url = (getattr(settings, "home_assistant_url", "") or "").strip()
    if not url:
        return {"skipped": f"Home Assistant nicht konfiguriert ({env_file})"}

    from solvio.capabilities import policy as _AP
    from solvio.integrations.home_assistant import HomeAssistant
    from solvio.secret_vault import context as _SC
    from solvio.secret_vault.broker import SecretBroker
    from solvio.storage import ha_backup

    # DER ZUGANG KOMMT AUS DEM TRESOR — und der Sicherungsjob sagt ehrlich, was
    # er ist: ein Hintergrundlauf ohne anwesenden Menschen. Genau dafuer traegt
    # der HA-Eintrag `allow_background`. Ein Job, der sich als interaktive
    # Sitzung ausgaebe, um an ein Geheimnis zu kommen, waere die eine Sorte
    # Bequemlichkeit, die dieses Projekt nicht kauft.
    #
    # Dieser Prozess ist NICHT der Core: `de.solvio.backup` laeuft als eigener
    # launchd-Job. Er hat keinen Router, also keinen Vorgang — deshalb wird der
    # Vorgang hier ausdruecklich gesetzt statt vorausgesetzt.
    broker = SecretBroker()
    if broker.exists(HomeAssistant.CREDENTIAL_REF):
        ha = HomeAssistant(url, timeout=30.0, broker=broker)
    elif (getattr(settings, "home_assistant_token", "") or "").strip():
        ha = HomeAssistant(url, settings.home_assistant_token, timeout=30.0)
    else:
        return {"skipped": "kein Home-Assistant-Zugang (weder Tresor noch .env)"}

    with _SC.bound(_SC.UseContext(
            origin=_AP.OriginClass.BACKGROUND_AUTOMATION,
            capability="home_assistant_backup",
            automation_id="de.solvio.backup")):
        return asyncio.run(ha_backup.fetch_into(
            incoming, ha, previous_root=getattr(linker, "previous_root", None)))


_ha_step.name = "home-assistant"                          # type: ignore[attr-defined]


# --------------------------------------------------------------------- Kommandozeile
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solvio-backup", description="SOLVIO Sicherungslauf")
    parser.add_argument("--label", default="",
                        help="Name eines Wiederherstellungspunktes; benannte "
                             "Saetze werden nie automatisch verworfen")
    parser.add_argument("--no-ha", action="store_true",
                        help="Home Assistant nicht abholen")
    parser.add_argument("--no-repos", action="store_true",
                        help="keine git-Buendel erzeugen")
    parser.add_argument("--force", action="store_true",
                        help="auch dann sichern, wenn die letzte Sicherung noch "
                             "frisch ist")
    parser.add_argument("--json", action="store_true", help="Ergebnis als JSON")
    args = parser.parse_args(argv)

    outcome = asyncio.run(run_once(label=args.label, include_ha=not args.no_ha,
                                   include_repos=not args.no_repos,
                                   force=args.force))
    if args.json:
        print(json.dumps(outcome, indent=2, ensure_ascii=False, default=str))
    else:
        if outcome.get("skipped"):
            print(f"uebersprungen: {outcome.get('reason')}")
        elif outcome.get("ok"):
            print(f"Sicherung {outcome['backup_id']} in "
                  f"{outcome['duration_seconds']}s, "
                  f"{outcome['total_bytes'] / 1024**2:.1f} MB "
                  f"(davon {outcome['linked_bytes'] / 1024**2:.1f} MB geteilt)")
        else:
            print(f"FEHLGESCHLAGEN: {outcome.get('reason') or outcome.get('errors')}")
    # Ein uebersprungener Lauf ist kein Fehler — launchd soll nicht neu starten.
    return 0 if outcome.get("ok") is not False else 1


if __name__ == "__main__":
    sys.exit(main())
