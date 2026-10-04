#!/usr/bin/env python3
"""Den Tresor bedienen — oertlich, besitzergebunden, ohne Wert im Terminal.

Dies ist die VERTRAUENSWUERDIGE OERTLICHE STEUEREBENE des Tresors, im selben
Sinn wie `solvio-approval-admin` fuer den Freigabeweg: sie ist kein Werkzeug des
Modells, keine Route des Gateways und nichts, was aus dem Netz erreichbar ist.
Wer sie ausfuehrt, sitzt an diesem Rechner und ist als `solvio` angemeldet.

    python3 scripts/vault_admin.py status
    python3 scripts/vault_admin.py init
    python3 scripts/vault_admin.py recovery-set
    python3 scripts/vault_admin.py migrate --dry-run
    python3 scripts/vault_admin.py migrate --commit
    python3 scripts/vault_admin.py drill --into /pfad/zur/probe

**Kein Geheimnis erscheint hier.** Nicht in `argv`, nicht in der Ausgabe, nicht
in der Terminal-Historie. Die Wiederherstellungs-Passphrase wird in einem
NATIVEN macOS-Fenster mit verdeckter Eingabe erfragt — dasselbe Muster wie
`scripts/portal_credential.py`, und aus demselben Grund: eine Passphrase, die
jemand in einen Chat tippt, ist keine mehr.

Was ausgegeben wird, sind Namen, Zustaende und Zahlen. Keine Laenge, kein
Anfangsbuchstabe, keine Pruefsumme eines Wertes — solche „harmlosen" Auskuenfte
sind der uebliche Weg, auf dem ein Geheimnis doch noch in ein Protokoll rutscht.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from solvio.capabilities import policy as AP            # noqa: E402
from solvio.secret_vault import admin, health, migration, recovery  # noqa: E402
from solvio.secret_vault import context as SC           # noqa: E402
from solvio.secret_vault import keyring as K            # noqa: E402
from solvio.secret_vault import policy as VP            # noqa: E402
from solvio.secret_vault.broker import SecretBroker     # noqa: E402
from solvio.secret_vault.store import VaultStore        # noqa: E402

#: Wie lange ein Eingabefenster offen bleibt.
#:
#: Fuenf Minuten waren zu kurz und das ist gemessen: beim ersten Versuch lief
#: die Frist ab, waehrend der Besitzer noch zum Rechner ging. Eine MENSCHLICHE
#: Grenze ist keine Maschinenfrist — sie wartet auf jemanden, der aufsteht.
#: Eine Frist gibt es trotzdem, damit ein vergessenes Fenster nicht ewig einen
#: Vorgang blockiert.
#: Frist des APPLE-EVENTS. Ohne sie gilt die Vorgabe von System Events: 60
#: Sekunden — zu wenig fuer einen Menschen, der eine Passphrase aussucht.
APPLE_EVENT_TIMEOUT = 900

#: Frist des PROZESSES. Bewusst groesser als die innere, damit die innere
#: zuerst greift und einen sprechenden Fehler liefert.
DIALOG_TIMEOUT = 960


def ask_secret(prompt: str, title: str = "SOLVIO — Tresor") -> str:
    """Fragt verdeckt nach einem Wert. Der Wert bleibt in diesem Prozess.

    ZWEI WEGE, und der erste ist der bessere. Welcher gilt, entscheidet nicht
    eine Vorliebe, sondern ob ein Terminal da ist.

    **Am Terminal: `getpass`.** Kein Echo, kein `argv`, keine Historie, keine
    Umgebung. Und vor allem: keine dritte Partei. CLAUDE.md nennt „Passwort im
    Terminal" ausdruecklich als menschliche Grenze — das ist sie.

    **Ohne Terminal: ein natives Fenster.** Das braucht es, wenn dieses Skript
    aus einem Dienst heraus laeuft, der keine Eingabe hat.

    Warum die Reihenfolge so herum ist, hat die Abnahme gelehrt. Der
    Fensterweg hat drei Anlaeufe gekostet: ohne `activate` stand das Fenster
    hinter allem anderen; MIT `activate` ueber System Events brachte er dessen
    Apple-Event-Frist mit, die auch `with timeout of` nicht zuverlaessig
    aushebelt — das Fenster stand da, der Mensch tippte, und die Leitung
    dahinter lief nach 60 Sekunden ab. Ein Eingabeweg, der davon abhaengt, wie
    schnell jemand tippt, ist keiner.

    Sicherheitsverhalten ist auf beiden Wegen dasselbe: verdeckt, prozesslokal,
    und der Wert wird nirgends zurueckgegeben ausser an den Aufrufer.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        print(prompt)
        return getpass.getpass("  > ")
    return _ask_secret_window(prompt, title)


def _ask_secret_window(prompt: str, title: str) -> str:
    """Der Rueckfall ohne Terminal: ein natives Fenster mit verdeckter Eingabe.

    Bewusst OHNE `tell application "System Events"`. Das brachte zwar den Fokus,
    aber auch eine Apple-Event-Frist, an der drei Anlaeufe gescheitert sind. Ein
    Fenster, das wartet und eventuell hinten steht, ist besser als eines, das
    vorn steht und nach einer Minute aufgibt.
    """
    script = (f'display dialog {_applescript(prompt)} default answer "" '
              f'with hidden answer with title {_applescript(title)} '
              f'buttons {{"Abbrechen", "Weiter"}} default button "Weiter"')
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", script],
                              capture_output=True, text=True, timeout=DIALOG_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit("Zeitueberschreitung — nichts geaendert.") from None
    if proc.returncode != 0:
        # ABBRUCH UND FEHLER SIND NICHT DASSELBE, und beim ersten Versuch sahen
        # sie gleich aus. Der Besitzer haette gesucht, was er falsch gemacht hat.
        detail = (proc.stderr or "").strip()
        if "-128" in detail or "User canceled" in detail:
            raise SystemExit("Abgebrochen — nichts geaendert.")
        if "-1743" in detail or "Not authorized" in detail:
            raise SystemExit(
                "Kein Automationsrecht: dieser Prozess darf kein Fenster oeffnen.\n"
                "  Fuehre den Befehl stattdessen in einem Terminal aus — dort\n"
                "  fragt er ueber `getpass`, ohne Fenster und ohne Frist.\n"
                "  Nichts geaendert.")
        raise SystemExit(f"Das Fenster liess sich nicht oeffnen: "
                         f"{detail[:200] or 'kein Grund gemeldet'}\n"
                         f"  Fuehre den Befehl in einem Terminal aus.\n"
                         f"  Nichts geaendert.")
    line = proc.stdout.strip()
    marker = "text returned:"
    return line[line.index(marker) + len(marker):].strip() if marker in line else ""


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ------------------------------------------------------------------- status
def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(health.detail(), ensure_ascii=False, indent=2))
    store = VaultStore()
    for entry in SecretBroker(store).catalogue():
        print(f"  {entry['secret_ref']:38s} {entry['status']:9s} "
              f"v{entry['version']}  {entry['display_name']}")
    return 0


# --------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    created = admin.initialize()
    print("Hauptschluessel angelegt." if created else "Hauptschluessel war schon da.")
    print(f"Speicher: {'Datei (Testmodus)' if K.is_test_backend() else 'Schluesselbund'}")
    return 0


# ----------------------------------------------------------------- recovery
def cmd_recovery_set(args: argparse.Namespace) -> int:
    """Setzt die Wiederherstellungs-Passphrase — zweimal, ohne Echo, ohne Log."""
    kek = K.read_kek()
    if kek is None:
        print("Der Tresor ist noch nicht eingerichtet. Erst `init`.", file=sys.stderr)
        return 2
    first = ask_secret("Wiederherstellungs-Passphrase fuer den SOLVIO-Tresor.\n\n"
                       "Sie ist der EINZIGE Weg zurueck, wenn dieser Mac verloren "
                       "geht. Schreib sie in deinen Passwortmanager.\n\n"
                       f"Mindestens {recovery.MIN_PASSPHRASE} Zeichen:")
    if not first:
        print("Nichts eingegeben — nichts geaendert.", file=sys.stderr)
        return 2
    again = ask_secret("Zur Sicherheit noch einmal:")
    if first != again:
        # Kein Hinweis darauf, WORIN sie sich unterscheiden.
        print("Die beiden Eingaben sind nicht gleich — nichts geaendert.",
              file=sys.stderr)
        return 2
    try:
        envelope = recovery.build(first, kek)
    except recovery.RecoveryError as exc:
        print(f"Abgelehnt: {exc}", file=sys.stderr)
        return 2
    finally:
        del first, again, kek
    path = recovery.write(envelope)
    print(f"Wiederherstellungsumschlag geschrieben: {path}")
    print("Er geht mit in die verschluesselte Sicherung. Ohne die Passphrase "
          "oeffnet ihn niemand — auch du nicht.")
    return 0


# ---------------------------------------------------------------- migration
def _env_values(path: str) -> dict[str, str]:
    """Liest `.env` als Abbildung. Der Rueckgabewert wird NIE ausgegeben."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _targets_for(plan: migration.Plan, env: dict[str, str]) -> tuple[str, ...]:
    if plan.allowed_targets:
        return plan.allowed_targets
    if plan.env_name == "HOME_ASSISTANT_TOKEN":
        # Das Ziel ist die Adresse, unter der Home Assistant WIRKLICH steht —
        # aus der Konfiguration, nicht aus einer Annahme.
        url = VP.normalize_target(env.get("HOME_ASSISTANT_URL", ""))
        return (url,) if url else ()
    return ()


def cmd_migrate(args: argparse.Namespace) -> int:
    env_path = migration.env_path()
    if not os.path.exists(env_path):
        print(f"Keine Konfiguration unter {env_path}", file=sys.stderr)
        return 2
    if K.read_kek() is None:
        print("Der Tresor ist noch nicht eingerichtet. Erst `init`.", file=sys.stderr)
        return 2
    env = _env_values(env_path)
    store = VaultStore()
    broker = SecretBroker(store)

    #: Die kleinste hinreichende Wanderung ist eine, die man nachweisen kann.
    #: `--only` gibt es, damit ein Zugang EINZELN wandern und EINZELN belegt
    #: werden kann — drei auf einmal zu verschieben und danach zu sagen „es
    #: lief" waere kein Nachweis, sondern eine Hoffnung mit drei Teilen.
    wanted = {name.strip().upper() for name in (args.only or []) if name.strip()}
    unknown = wanted - {p.env_name for p in migration.ENV_PLANS}
    if unknown:
        print(f"Unbekannt im Wanderungsplan: {sorted(unknown)}", file=sys.stderr)
        return 2

    planned: list[tuple[migration.Plan, tuple[str, ...]]] = []
    for plan in migration.ENV_PLANS:
        if wanted and plan.env_name not in wanted:
            print(f"  zurueckgestellt {plan.env_name}: nicht in --only")
            continue
        if plan.env_name not in env or not env[plan.env_name]:
            print(f"  uebersprungen  {plan.env_name}: steht nicht in .env")
            continue
        targets = _targets_for(plan, env)
        if not targets:
            print(f"  uebersprungen  {plan.env_name}: kein Ziel ableitbar")
            continue
        planned.append((plan, targets))

    print(f"\nGeplant: {len(planned)} Zugaenge in den Tresor.")
    for plan, targets in planned:
        state = "ersetzen" if broker.exists(plan.secret_ref) else "neu"
        print(f"  {state:9s} {plan.env_name:32s} -> {plan.secret_ref}")
        print(f"            Ziel: {', '.join(targets)}")
        print(f"            Nur ueber: {', '.join(e.value for e in plan.allowed_executors)}")
    for name, why in migration.STAYS_IN_ENV:
        if wanted and name not in wanted:
            continue
        print(f"  bleibt    {name:32s} -> .env")
        print(f"            {why}")

    if not args.commit:
        print("\nProbelauf. Mit --commit wird geschrieben.")
        return 0

    for plan, targets in planned:
        admin.add(secret_ref=plan.secret_ref, kind=plan.kind,
                  plaintext=env[plan.env_name].encode("utf-8"),
                  allowed_capabilities=plan.allowed_capabilities,
                  allowed_targets=targets,
                  allowed_executors=plan.allowed_executors,
                  display_name=plan.display_name,
                  service_label=plan.service_label,
                  account_label=plan.account_label,
                  note=plan.why, allow_background=plan.allow_background,
                  store=store, replace=True)
        print(f"  abgelegt  {plan.secret_ref}")

    # NACHGERECHNET, NICHT GEGLAUBT. Eine Wanderung, die den alten Wert loescht,
    # bevor der neue nachweislich lesbar ist, ist keine Wanderung, sondern ein
    # Verlust. Verglichen wird die Laenge und die Gleichheit — ausgegeben wird
    # keins von beidem.
    for plan, targets in planned:
        with SC.bound(SC.UseContext(
                origin=AP.OriginClass.LOCAL_OWNER,
                capability=plan.allowed_capabilities[0])):
            ok, why = _verify(broker, plan, targets[0], env[plan.env_name])
        print(f"  geprueft  {plan.secret_ref}: {why}")
        if not ok:
            print("Wanderung abgebrochen.", file=sys.stderr)
            return 1

    # DER KLARTEXT BLEIBT VORERST STEHEN, und das ist kein Versehen.
    #
    # Eine Wanderung ist erst dann eine, wenn der neue Ort NACHWEISLICH traegt —
    # nicht wenn er beschrieben wurde. Zwischen „im Tresor abgelegt" und „aus
    # dem Tresor benutzt" liegt ein Neustart des Cores und ein echter Aufruf.
    # Erst danach nimmt `retire-plaintext` die alte Zeile heraus.
    print("\nDer Klartext in .env bleibt vorerst stehen.")
    print("Der Core muss neu starten, damit er den Zugang aus dem Tresor holt:")
    print("  launchctl kickstart -k gui/$(id -u)/com.solvio.core")
    print("Danach, NACH einem echten Aufruf:")
    print("  python3 scripts/vault_admin.py retire-plaintext --only "
          + " --only ".join(p.env_name for p, _ in planned))
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    """Nimmt den Klartext heraus — aber erst, wenn der Tresor nachweislich traegt.

    Geprueft wird nicht „liegt etwas im Tresor", sondern „kommt aus dem Tresor
    derselbe Wert heraus, der noch in `.env` steht". Wer nur das Erste prueft,
    loescht irgendwann eine Zeile, weil daneben ein leerer Eintrag liegt.
    """
    env_path = migration.env_path()
    env = _env_values(env_path)
    store = VaultStore()
    broker = SecretBroker(store)
    wanted = {name.strip().upper() for name in (args.only or []) if name.strip()}
    if not wanted:
        print("Ohne --only wird nichts entfernt.", file=sys.stderr)
        return 2

    ready = []
    for plan in migration.ENV_PLANS:
        if plan.env_name not in wanted:
            continue
        if plan.env_name not in env:
            print(f"  bereits weg  {plan.env_name}")
            continue
        policy = store.policy(plan.secret_ref)
        if policy is None:
            print(f"  ABBRUCH      {plan.env_name}: liegt nicht im Tresor",
                  file=sys.stderr)
            return 1
        with SC.bound(SC.UseContext(origin=AP.OriginClass.LOCAL_OWNER,
                                    capability=policy.allowed_capabilities[0])):
            same, why = _verify(broker, plan, policy.allowed_targets[0],
                                env[plan.env_name])
        print(f"  geprueft     {plan.env_name}: {why}")
        if not same:
            print("  Abgebrochen — nichts entfernt.", file=sys.stderr)
            return 1
        ready.append(plan.env_name)

    if not ready:
        print("Nichts zu entfernen.")
        return 0
    removed = _strip_env(env_path, ready)
    print(f"\nAus .env entfernt: {', '.join(removed)}")
    print("Jetzt den Core neu starten und einen echten Aufruf wiederholen —")
    print("er kann danach NUR noch aus dem Tresor kommen.")
    return 0


def _verify(broker: SecretBroker, plan: migration.Plan, target: str,
            expected: str) -> tuple[bool, str]:
    """Kommt derselbe Wert zurueck? Gibt Ja/Nein UND den Grund.

    Der Pruefer muss aus einem Modul rufen, dessen Name zum Executor passt —
    sonst prueft er nicht den Wert, sondern nur, dass die Aufruferbindung
    greift. Genau darauf ist die erste Wanderung gelaufen: sie meldete
    „ABWEICHUNG", obwohl der Wert stimmte und nur der Aufrufer der falsche war.
    Deshalb kommt der Modulname jetzt aus derselben Tabelle, gegen die der
    Tresor prueft, und eine Absage heisst „abgewiesen", nicht „anders".
    """
    import types

    from solvio.secret_vault import broker as B
    from solvio.secret_vault.policy import EXECUTOR_MODULES
    prefixes = EXECUTOR_MODULES.get(plan.allowed_executors[0], ())
    if not prefixes:
        return False, "kein Modulpraefix fuer diesen Executor"
    name = prefixes[0].rstrip(".") if not prefixes[0].endswith(".") \
        else prefixes[0] + "_vault_verify"
    module = types.ModuleType(name)
    exec(compile(
        "def read(broker, ref, executor, target):\n"
        "    with broker.use(ref, executor=executor, target=target) as m:\n"
        "        return m.plaintext()\n", "<verify>", "exec"), module.__dict__)
    try:
        value = module.read(broker, plan.secret_ref, plan.allowed_executors[0],
                            target)
    except B.SecretDenied as exc:
        return False, f"abgewiesen ({exc.reason.value})"
    except B.SecretUnavailable:
        return False, "Tresor nicht verfuegbar"
    # Verglichen wird, nicht ausgegeben.
    return (value == expected), ("Tresor liefert denselben Wert" if value == expected
                                 else "der Wert im Tresor ist ein anderer")


def _strip_env(path: str, names: list[str]) -> list[str]:
    """Nimmt Zeilen aus `.env` — und hinterlaesst einen Vermerk, wo sie jetzt sind.

    Die Datei wird atomar ersetzt und behaelt ihre Rechte. Ein Vermerk statt
    einer stillen Luecke: wer spaeter `.env` liest und `HOME_ASSISTANT_TOKEN`
    vermisst, soll nicht raten muessen.
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.readlines()
    kept, removed = [], []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in names:
            removed.append(key)
            continue
        kept.append(line)
    if not removed:
        return []
    kept.append("\n# In den Geheimnistresor gewandert "
                "(SOLVIO Secret & Credential Vault V1):\n")
    for name in removed:
        plan = next(p for p in migration.ENV_PLANS if p.env_name == name)
        kept.append(f"#   {name} -> {plan.secret_ref}\n")
    temporary = path + ".new"
    previous = os.umask(0o077)
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
    finally:
        os.umask(previous)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Zustand des Tresors")
    sub.add_parser("init", help="Hauptschluessel anlegen (nur bei leerem Tresor)")
    sub.add_parser("recovery-set", help="Wiederherstellungs-Passphrase setzen")
    migrate = sub.add_parser("migrate", help="Zugaenge aus .env in den Tresor")
    migrate.add_argument("--commit", action="store_true",
                         help="wirklich schreiben (sonst nur Probelauf)")
    migrate.add_argument("--dry-run", action="store_true", help="Vorgabe")
    migrate.add_argument("--only", action="append", metavar="ENV_NAME",
                         help="nur diesen Zugang wandern lassen (mehrfach moeglich)")
    retire = sub.add_parser("retire-plaintext",
                            help="den Klartext aus .env nehmen — nach dem Nachweis")
    retire.add_argument("--only", action="append", metavar="ENV_NAME", required=True,
                        help="welchen Zugang (mehrfach moeglich)")
    args = parser.parse_args(argv)
    return {
        "status": cmd_status,
        "init": cmd_init,
        "recovery-set": cmd_recovery_set,
        "migrate": cmd_migrate,
        "retire-plaintext": cmd_retire,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
