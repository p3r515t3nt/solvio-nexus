"""Kommandozeile des SOLVIO Core."""

from __future__ import annotations

import argparse
import asyncio
import sys

from solvio import __version__
from solvio.config import load_settings
from solvio.health import collect_health
from solvio.logging_setup import get_logger, setup_logging

SMOKE_PROMPT = "Antworte ausschliesslich mit: SOLVIO ONLINE"
SMOKE_INSTRUCTIONS = (
    "Du bist SOLVIO. Antworte in diesem Test ausschliesslich mit der exakten "
    "Zeichenfolge SOLVIO ONLINE, ohne Satzzeichen, ohne Erklaerung."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solvio", description="SOLVIO Core")
    parser.add_argument("--version", action="version", version=f"SOLVIO Core {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("health", help="Lokalen Zustand pruefen, ohne Netzwerkzugriff")
    sub.add_parser("config", help="Geladene Konfiguration anzeigen, Geheimnisse maskiert")

    kn = sub.add_parser("knowledge-compile",
                        help="Gedaechtnis in das lesbare Wissensbuendel kompilieren")
    kn.add_argument("--vault", default=None,
                    help="Zielverzeichnis (Vorgabe: ~/SOLVIO Knowledge)")

    kl = sub.add_parser("knowledge-lint",
                        help="Wissensbuendel pruefen — ohne Netz, ohne Gedaechtnis")
    kl.add_argument("--vault", default=None,
                    help="Zielverzeichnis (Vorgabe: ~/SOLVIO Knowledge)")

    sub.add_parser("storage-status",
                   help="Zustand der Sicherungsplatte und der letzten Sicherung")

    ss = sub.add_parser("storage-setup",
                        help="Eine angeschlossene, verschluesselte Platte als "
                             "SOLVIO-Speicher einrichten")
    ss.add_argument("--uuid", default=None,
                    help="Volume-UUID; ohne Angabe wird die einzige passende "
                         "externe APFS-Platte gesucht")
    ss.add_argument("--allow-unencrypted", action="store_true",
                    help="nur fuer Proben: unverschluesselt zulassen. Es wird "
                         "dann nichts Privates geschrieben.")

    bn = sub.add_parser("backup-now", help="Sicherung jetzt ausfuehren")
    bn.add_argument("--label", default="",
                    help="Wiederherstellungspunkt benennen (wird nie automatisch "
                         "verworfen)")
    bn.add_argument("--no-ha", action="store_true")
    bn.add_argument("--no-repos", action="store_true")

    bv = sub.add_parser("backup-verify",
                        help="Pruefsummen eines Sicherungssatzes nachrechnen")
    bv.add_argument("--set", default=None, help="Satzkennung (Vorgabe: der juengste)")

    br = sub.add_parser("backup-restore-test",
                        help="Wiederherstellungsprobe in ein Wegwerfverzeichnis")
    br.add_argument("--set", default=None, help="Satzkennung (Vorgabe: der juengste)")
    br.add_argument("--target", default=None, help="Zielverzeichnis (Vorgabe: temporaer)")
    br.add_argument("--clone-repos", action="store_true",
                    help="git-Buendel zusaetzlich auschecken")

    rt = sub.add_parser("realtime-test", help="Einen echten Textdurchlauf ueber die Realtime-API")
    rt.add_argument("--timeout", type=float, default=30.0, help="Zeitgrenze in Sekunden")

    vt = sub.add_parser("voice-test", help="Gesprochenes Gespraech ueber den Satelliten")
    vt.add_argument("--host", default="0.0.0.0", help="Adresse des Satelliten-Servers")
    vt.add_argument("--port", type=int, default=8766)
    vt.add_argument("--seconds", type=float, default=0, help="automatisch beenden nach x Sekunden")
    vt.add_argument("--voice", default="marin", help="Stimme der Sprachausgabe")
    vt.add_argument("--effort", default="minimal",
                    help="Denkaufwand: minimal, low, medium, high, xhigh")
    vt.add_argument("--eagerness", default="high",
                    help="Reaktionsfreude der Sprechpausenerkennung: low, medium, high, auto")
    return parser


def _cmd_storage(args) -> int:
    """Alles rund um Speicher und Sicherung.

    In einer Funktion, weil die Unterbefehle sich dieselben zwei Fragen teilen:
    welche Platte, und welcher Satz. Fuenf getrennte Funktionen haetten diese
    Aufloesung fuenfmal.
    """
    import json as _json
    import tempfile

    from solvio.storage import engine, health, restore, volume

    if args.command == "storage-status":
        print(_json.dumps(health.summary(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "storage-setup":
        return _cmd_storage_setup(args)

    if args.command == "backup-now":
        from solvio.storage.job import main as job_main
        argv = []
        if args.label:
            argv += ["--label", args.label]
        if args.no_ha:
            argv.append("--no-ha")
        if args.no_repos:
            argv.append("--no-repos")
        return job_main(argv)

    # Ab hier brauchen beide einen Satz auf einer vertrauenswuerdigen Platte.
    try:
        root = volume.storage_root()
    except volume.StorageError as exc:
        print(f"Speicher nicht verfuegbar: {exc}")
        return 1
    import os as _os
    sets_dir = _os.path.join(root, "Backups", "sets")
    names = engine.list_sets(sets_dir)
    if not names:
        print("Es liegt kein Sicherungssatz vor.")
        return 1
    chosen = args.set or names[-1]
    if chosen not in names:
        print(f"Unbekannter Satz {chosen}. Vorhanden: {', '.join(names)}")
        return 1
    set_path = _os.path.join(sets_dir, chosen)

    if args.command == "backup-verify":
        result = restore.verify_set(set_path)
        print(_json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

    # Standardziel ist das dafuer vorgesehene Verzeichnis AUF der verschluesselten
    # Platte, nicht `/tmp`. Eine Probe packt `.env`, PKI und Datenbanken aus —
    # das gehoert nicht in ein Systemtemp, aus dem es jemand vergisst
    # wegzuraeumen. Genau das ist beim Bau dieses Milestones passiert (78
    # entschluesselte Dateien in /tmp), und die Platte hat den Ordner ohnehin.
    if args.target:
        target = args.target
    else:
        probe_root = volume.secure_dir(_os.path.join(root, "Recovery",
                                                     "restore-tests"))
        target = tempfile.mkdtemp(prefix=f"probe-{chosen}-", dir=probe_root)
    report = restore.restore_set(set_path, target, clone_repos=args.clone_repos)
    print(_json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0 if report.ok else 1


def _cmd_storage_setup(args) -> int:
    """Richtet die Platte ein — nachdem ein MENSCH sie verschluesselt hat.

    SOLVIO verschluesselt hier nichts. Er prueft, und er sagt genau, was fehlt.
    """
    from solvio.storage import volume

    uuid = args.uuid
    if not uuid:
        found = volume.find_candidates()
        if not found:
            print("Keine externe APFS-Platte gefunden.")
            return 1
        if len(found) > 1:
            print("Mehrere externe APFS-Platten gefunden — bitte --uuid angeben:")
            for cand in found:
                print(f"  {cand['volume_uuid']}  {cand['name']}  "
                      f"verschluesselt={cand['encrypted']}")
            return 1
        uuid = found[0]["volume_uuid"]

    config = volume.StorageConfig(volume_uuid=uuid.upper(),
                                  require_encryption=not args.allow_unencrypted)
    state = volume.probe(config)
    if not state.present:
        print(f"Die Platte {uuid} ist nicht angeschlossen.")
        return 1
    if state.problems:
        for problem in state.problems:
            print(f"  ! {problem}")
        if not state.encrypted and not args.allow_unencrypted:
            print("\nDie Verschluesselung setzt ein Mensch, nicht SOLVIO:")
            print('  Finder -> Rechtsklick auf das Volume -> "... verschluesseln"')
        return 1

    config = volume.StorageConfig(volume_uuid=uuid.upper(),
                                  require_encryption=not args.allow_unencrypted,
                                  label_hint=state.volume_name or "")
    path = volume.save_config(config)
    root = volume.storage_root(state, config)
    created = volume.create_layout(root, config)
    print(f"Speicherplatte eingerichtet: {state.volume_name} ({uuid})")
    print(f"  Konfiguration: {path}")
    print(f"  Wurzel:        {root}")
    for line in created:
        print(f"    {line}")
    return 0


def _cmd_config(settings) -> int:
    print(f"host: {settings.solvio_host}")
    print(f"port: {settings.solvio_port}")
    print(f"log_level: {settings.solvio_log_level}")
    print(f"realtime_model: {settings.openai_realtime_model}")
    print(f"openai_api_key: {settings.masked(settings.openai_api_key)}")
    print(f"home_assistant_url: {settings.home_assistant_url or 'leer'}")
    print(f"home_assistant_token: {settings.masked(settings.home_assistant_token)}")
    print(f"voice_satellite_host: {settings.voice_satellite_host or 'leer'}")
    return 0


def _cmd_realtime_test(settings, timeout: float) -> int:
    from solvio.realtime import RealtimeClient

    if not settings.has_realtime:
        print("Realtime ist nicht konfiguriert. Es fehlt der API-Schluessel in der .env.")
        return 2

    client = RealtimeClient(settings.openai_api_key, settings.openai_realtime_model, timeout=timeout)
    result = asyncio.run(client.text_roundtrip(SMOKE_PROMPT, instructions=SMOKE_INSTRUCTIONS))

    print("")
    print("--- Realtime Smoke Test ---")
    print(f"Modell:            {result.model}")
    print(f"Session:           {result.session_id or 'keine'}")
    print(f"Anfrage:           {SMOKE_PROMPT}")
    print(f"Erwartet:          SOLVIO ONLINE")
    print(f"Empfangen:         {result.text.strip() or '(nichts)'}")
    print(f"Sauber geschlossen:{' ja' if result.closed_cleanly else ' nein'}")
    if result.error:
        print(f"Fehler:            {result.error}")
    t = result.timings
    print("")
    print("--- Latenz ---")
    print(f"Verbindungsaufbau (T0->T1):     {t.connection_ms} ms")
    print(f"Erste Antwort     (T2->T3):     {t.time_to_first_response_ms} ms")
    print(f"Antwort komplett  (T2->T4):     {t.total_response_ms} ms")
    print("")
    print(f"Ereignisse: {', '.join(result.event_types)}")
    if result.usage:
        print(f"Verbrauch: {result.usage}")

    normalized = result.text.strip().strip(".!").upper()
    ok = normalized == "SOLVIO ONLINE"
    print("")
    print(f"ERGEBNIS: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _cmd_voice_test(settings, args) -> int:
    from solvio.realtime.voice_session import VoiceSession

    if not settings.has_realtime:
        print("Realtime ist nicht konfiguriert. Es fehlt der API-Schluessel in der .env.")
        return 2

    session = VoiceSession(
        api_key=settings.openai_api_key,
        model=settings.openai_realtime_model,
        host=args.host,
        port=args.port,
        reasoning_effort=args.effort,
        voice=args.voice,
        eagerness=args.eagerness,
    )
    try:
        metrics = asyncio.run(session.run(duration=args.seconds or None))
    except KeyboardInterrupt:
        metrics = session.metrics
    print(metrics.render())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = load_settings()
    setup_logging(settings.solvio_log_level)
    log = get_logger()

    if args.command == "config":
        return _cmd_config(settings)

    if args.command == "knowledge-compile":
        import asyncio

        from solvio.knowledge.service import compile_knowledge, vault_dir
        target = args.vault or vault_dir()
        counts = asyncio.run(compile_knowledge(target))
        print(f"Vault: {target}")
        for key, value in counts.items():
            print(f"  {key}: {value}")
        return 0

    if args.command == "knowledge-lint":
        from solvio.knowledge.service import lint_knowledge, vault_dir
        target = args.vault or vault_dir()
        findings = lint_knowledge(target)
        print(f"Vault: {target}")
        for line in findings:
            print(f"  - {line}")
        if not findings:
            print("  nichts zu melden")
        # Ein Befund ist kein Fehler des Werkzeugs. `1` hiesse „Lauf kaputt",
        # und das waere gelogen — der Lauf hat genau das getan, was er soll.
        return 0

    if args.command in ("storage-status", "storage-setup", "backup-now",
                        "backup-verify", "backup-restore-test"):
        return _cmd_storage(args)

    if args.command == "realtime-test":
        return _cmd_realtime_test(settings, args.timeout)

    if args.command == "voice-test":
        return _cmd_voice_test(settings, args)

    log.info("solvio.start", version=__version__, phase="voice-prototype")
    print(collect_health(settings).render())
    log.info("solvio.stop", reason="kein Dauerbetrieb in dieser Phase")
    return 0


if __name__ == "__main__":
    sys.exit(main())
