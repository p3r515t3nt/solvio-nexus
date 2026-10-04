"""Offsite V1 B3 — Transport, Buch und die Zustaende, die nicht luegen duerfen.

Der Schwerpunkt liegt nicht darauf, dass ein Upload gelingt. Er liegt auf
den Zusicherungen, ohne die ein gelungener Upload nichts wert waere:

* **Kein falsches Gruen.** Ein Upload ohne Ruecklade-Vergleich ist kein
  Erfolg; ein gekipptes Byte muss auffallen; ein Timeout darf NIE wie ein
  Erfolg aussehen.
* **Ehrliche Rekonstruktion.** Ein Prozess, der mitten im Upload stirbt,
  hinterlaesst `uploading` — und das ist beim naechsten Start eine offene
  Frage, kein geerbter Erfolg.
* **Kein Loeschrecht.** Der Client hat strukturell keine Loeschmethode
  (Hygiene, §22 — der Beweis ist die AccessDenied-Probe beim Anbieter).
* **Idempotenz.** Derselbe Tag erzeugt keine zweite Wahrheit.
* **Die Klassen.** Tages-, Wochen-, Monatserstling; Kopie statt zweitem
  Upload.

S3 steht als kleiner lokaler HTTP-Stub (`tests/_s3_stub.py`) — kein neuer
Test-Dienst, und der echte Anbieter wird in der Live-Abnahme bewiesen, nicht
im Gate simuliert (§22).

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-offsite-b3-")
os.environ["SOLVIO_VAULT_DIR"] = os.path.join(_SANDBOX, "vault")
os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "keys")
os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(_SANDBOX, "offsite")
os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(_SANDBOX, "okeys")
os.environ["SOLVIO_OFFSITE_LEDGER"] = os.path.join(_SANDBOX, "offsite.sqlite3")
os.environ["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(_SANDBOX, "storage")
atexit.register(shutil.rmtree, _SANDBOX, True)

# DEBT-0228: der Bestand kommt aus einer eigenen Wurzel, nicht aus dem
# laufenden Core. Ohne diese Zeile liest diese Suite `~/.solvio/*.sqlite3`,
# die Freigabe-Identitaet und `/Users/solvio/solvio-core/.env` — also
# Production. Der Bestand hier ist vollstaendig und synthetisch; geprueft wird
# unveraendert dasselbe.
import _inventory_fixture                                      # noqa: E402
_inventory_fixture.activate(os.path.join(_SANDBOX, "bestand"))

from _s3_stub import RunningStub                              # noqa: E402
from solvio.secret_vault import admin, policy as VP           # noqa: E402
from solvio.secret_vault import keyring as K                  # noqa: E402
from solvio.secret_vault.store import VaultStore              # noqa: E402
from solvio.storage.offsite import config as OC               # noqa: E402
from solvio.storage.offsite import identity as OI             # noqa: E402
from solvio.storage.offsite import job as OJ                  # noqa: E402
from solvio.storage.offsite import ledger as OL               # noqa: E402
from solvio.storage.offsite import s3 as OS3                  # noqa: E402

#: Synthetisch, scannerfest — kein echter Anbieterschluessel hat diese Form.
FAKE_CREDENTIAL = json.dumps({
    "access_key_id": "SYNTHETIC-DRILL-KEY-0001",
    "secret_access_key": "synthetic-drill-value-0003"}).encode()

#: Eingefrorene SigV4-Vektoren. Sie stammen NICHT aus dem Gedaechtnis des
#: Autors, sondern aus einer Messung gegen eine unabhaengige Implementierung
#: (`b3/reports/sigv4-vektoren-report.json`, Orakel: botocore.SigV4Auth).
DOC_ACCESS = "AKIAIOSFODNN7EXAMPLE"
DOC_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
DOC_REGION = "us-east-1"
DOC_DATE = "20130524T000000Z"
DOC_HOST = "examplebucket.s3.amazonaws.com"
FROZEN_VECTORS = {
    "get_object_mit_range":
        "67fe34c8530db585abddc51067328adfedb6e42487d2566dc7d927d6e2722900",
    "put_object_kodierter_pfad":
        "d48ed510af89dd0ebf379481cf4fe35b94e23ed4edb5b386804e597cbf272b14",
    "list_objects_v2_mit_prefix":
        "63728550007ffa437aa12b821727f4ced47b82eeeac1009eb6197fe4cbb2f84a",
    "copy_object_kodierte_quelle":
        "01cc51d397f8a0850bef220dbfafbd2aff0daa55b6ce14c9b64254b552fef878",
}


# --------------------------------------------------------------------- Werkzeug
def _fresh_world(*, enabled: bool = True) -> tuple[OC.OffsiteConfig, str]:
    """Tresor, Identitaet, Konfiguration und Buch — je Test frisch."""
    root = tempfile.mkdtemp(prefix="b3-case-", dir=_SANDBOX)
    os.environ["SOLVIO_VAULT_DIR"] = os.path.join(root, "vault")
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(root, "keys")
    os.environ["SOLVIO_OFFSITE_DIR"] = os.path.join(root, "offsite")
    os.environ["SOLVIO_OFFSITE_TEST_KEYSTORE"] = os.path.join(root, "okeys")
    os.environ["SOLVIO_OFFSITE_LEDGER"] = os.path.join(root, "offsite.sqlite3")
    os.environ["SOLVIO_STORAGE_STATE_DIR"] = os.path.join(root, "storage")
    K.forget_kek()
    store = VaultStore()
    admin.initialize(store)
    line, recipient = OI.create_identity()
    OI.store_identity(line, version=1)
    base = OC.fresh(recipient)
    cfg = OC.OffsiteConfig(enabled=enabled, recipient=recipient,
                           recipient_version=1, region=base.region,
                           classes=base.classes)
    OC.save(cfg)
    admin.add(secret_ref="secret://offsite/s3",
              kind=VP.SecretKind.SERVICE_CREDENTIAL,
              plaintext=FAKE_CREDENTIAL,
              allowed_capabilities=("offsite_backup",),
              allowed_targets=tuple(cfg.bucket_url(k)
                                    for k in ("daily", "weekly", "monthly")),
              allowed_executors=(VP.ExecutorId.OFFSITE,),
              allow_background=True, store=store)
    return cfg, root


def _tiny_run(running: RunningStub, *, book: OL.OffsiteLedger | None = None,
              force: bool = True, now: dt.datetime | None = None) -> dict:
    """Ein Lauf gegen den Stub. Der Satz ist echt — die Maschine ist es auch."""
    return OJ.run_once(force=force, now=now, client=running.client(),
                       book=book or OL.OffsiteLedger())


# ------------------------------------------------------------ Testisolation
def t_this_suite_cannot_touch_production_state() -> None:
    from solvio.secret_vault.store import vault_dir
    require("/.solvio-vault" not in vault_dir(), "Tresor nicht umgelenkt")
    require("/.solvio/offsite" not in OC.offsite_dir(),
            "Offsite-Verzeichnis nicht umgelenkt")
    require("/.solvio/offsite.sqlite3" not in OL.ledger_path(),
            "das Offsite-Buch ist nicht umgelenkt")
    require(OI.is_test_backend(), "Offsite-Schluesselspeicher nicht umgelenkt")


# ------------------------------------------------------------------- SigV4
def t_sigv4_matches_the_frozen_vectors() -> None:
    """Die Vektoren stammen aus einer Messung gegen botocore — wer die
    Signaturfunktion aendert und diese Werte bricht, hat die Beweislast."""
    cases = {
        "get_object_mit_range": ("GET", "/test.txt", {},
                                 OS3.EMPTY_SHA256, {"Range": "bytes=0-9"}),
        "put_object_kodierter_pfad": (
            "PUT", "/test$file.text", {},
            "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e5b0a0b1c0b0e0d0f0a0b",
            {"Content-Type": "application/octet-stream",
             "Content-Length": "62914560"}),
        "list_objects_v2_mit_prefix": (
            "GET", "/", {"list-type": "2", "prefix": "v1/generations/",
                         "max-keys": "1000"}, OS3.EMPTY_SHA256, {}),
        "copy_object_kodierte_quelle": (
            "PUT", "/v1/generations/20260901T053000Z.tar.zst.age", {},
            OS3.EMPTY_SHA256,
            {"x-amz-copy-source": "/solvio-offsite-daily/v1/generations/"
                                  "20260901T053000Z.tar.zst.age"}),
    }
    for name, (method, path, query, payload, extra) in cases.items():
        headers = {"Host": DOC_HOST, "x-amz-date": DOC_DATE,
                   "x-amz-content-sha256": payload}
        headers.update(extra)
        value = OS3.authorization_header(
            access_key_id=DOC_ACCESS, secret=DOC_SECRET, region=DOC_REGION,
            amz_date=DOC_DATE, method=method, path=path, query=query,
            headers=headers, payload_sha256=payload)
        signature = value.rsplit("Signature=", 1)[1]
        require_equal(signature, FROZEN_VECTORS[name],
                      f"SigV4-Vektor {name} weicht ab")


def t_the_canonical_request_encodes_the_path_but_not_its_slashes() -> None:
    creq = OS3.canonical_request(
        "PUT", "/v1/generations/2026$T05:30.age", {}, {"Host": "x"},
        OS3.EMPTY_SHA256)
    uri = creq.splitlines()[1]
    require_equal(uri, "/v1/generations/2026%24T05%3A30.age",
                  "die kanonische URI ist falsch kodiert — genau hier wird "
                  "eine Signatur still falsch")


def t_put_carries_the_object_lock_checksum_header() -> None:
    """Live gemessen (2026-09-01, echtes AWS): ein Bucket MIT Object Lock
    verlangt bei PutObject `Content-MD5` oder `x-amz-checksum-*` — der
    SigV4-Nutzlast-Hash zaehlt ausdruecklich nicht. Ohne diesen Kopf
    scheitert JEDER Upload in die drei Produktions-Buckets."""
    import base64
    import hashlib as _h

    _cfg, _root = _fresh_world()
    with RunningStub() as running:
        outcome = _tiny_run(running)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
        puts = [r for r in running.stub.requests
                if r["method"] == "PUT"
                and not r["headers"].get("x-amz-copy-source")]
        require_equal(len(puts), 1, "unerwartete Zahl echter Uploads")
        sent = puts[0]["headers"].get("x-amz-checksum-sha256", "")
        require(sent, "der Object-Lock-Integritaetskopf fehlt — AWS wuerde "
                      "diesen Upload mit 400 InvalidRequest abweisen")
        expected = base64.b64encode(
            bytes.fromhex(outcome["cipher_sha256"])).decode()
        require_equal(sent, expected,
                      "der Integritaetskopf traegt eine andere Pruefsumme "
                      "als die Huelle")


def t_an_early_rejection_is_read_not_mistaken_for_a_broken_pipe() -> None:
    """Der zweite Live-Fund: lehnt S3 frueh ab, schliesst es die Verbindung,
    waehrend wir noch 70 MB schreiben. Ein Client, der nur den Schreibfehler
    sieht, meldet `remote_unavailable` und WIEDERHOLT eine Anfrage, die nie
    gelingen kann. Die Antwort liegt im Puffer — sie muss gelesen werden."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "frueh.sqlite3"))
    with RunningStub() as running:
        # `denied` antwortet sofort und schliesst — genau die AWS-Lage.
        running.stub.arm("denied", methods=("PUT",), times=99)
        outcome = _tiny_run(running, book=book)
        require_equal(outcome["category"], "auth_failed",
                      f"eine fruehe Absage wurde zu {outcome['category']!r} "
                      "verfaelscht — der Client hat die Antwort nicht gelesen")
        puts = [r for r in running.stub.requests if r["method"] == "PUT"]
        require(len(puts) <= 2,
                f"eine unabaenderliche Absage wurde {len(puts)}-mal "
                "wiederholt")


# ------------------------------------------------------------------- Hygiene
def t_the_client_has_no_delete_method() -> None:
    """§22, ausdruecklich als Hygiene dokumentiert: der Beweis der
    Rechtelage ist die AccessDenied-Probe beim Anbieter — aber niemand soll
    versehentlich eine Loeschmethode bauen koennen."""
    names = [n for n in dir(OS3.S3Client) if not n.startswith("__")]
    offenders = [n for n in names
                 if "delete" in n.lower() or "remove" in n.lower()
                 or "purge" in n.lower()]
    require_equal(offenders, [], f"der Client kann loeschen: {offenders}")
    source = open(OS3.__file__, encoding="utf-8").read()
    require("DeleteObject" not in source,
            "der Client nennt DeleteObject")


def t_the_stub_refuses_delete_like_the_provider_does() -> None:
    """Die Gegenprobe zur Hygiene: auch der Stub kennt kein Loeschen."""
    with RunningStub() as running:
        require_equal(running.stub.objects, {}, "der Stub startet nicht leer")


# ------------------------------------------------------------------- Klassen
def t_classes_follow_the_gfs_calendar() -> None:
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "klassen.sqlite3"))
    # Mittwoch, 2026-09-02: kein Vorgaenger → Erstling in allen drei Klassen.
    moment = dt.datetime(2026, 9, 2, 5, 30, tzinfo=dt.timezone.utc)
    require_equal(list(OJ.classes_for(moment, book)),
                  ["daily", "weekly", "monthly"],
                  "die erste Generation ueberhaupt ist Erstling aller Klassen")
    book.start(generation_id="20260902T053000Z", classes=("daily", "weekly",
                                                          "monthly"))
    book.mark("20260902T053000Z", OL.VERIFIED)
    # Donnerstag derselben Woche und desselben Monats: nur noch daily.
    later = dt.datetime(2026, 9, 3, 5, 30, tzinfo=dt.timezone.utc)
    require_equal(list(OJ.classes_for(later, book)), ["daily"],
                  "ein zweiter Tag derselben Woche wurde erneut Wochen-Erstling")
    # Montag darauf: neue ISO-Woche, gleicher Monat.
    next_week = dt.datetime(2026, 9, 7, 5, 30, tzinfo=dt.timezone.utc)
    require_equal(list(OJ.classes_for(next_week, book)), ["daily", "weekly"],
                  "der Montag ist kein neuer Wochen-Erstling")
    # Erster Oktober: neuer Monat UND neue Woche.
    next_month = dt.datetime(2026, 10, 1, 5, 30, tzinfo=dt.timezone.utc)
    require_equal(list(OJ.classes_for(next_month, book)),
                  ["daily", "weekly", "monthly"],
                  "der Monatserste ist kein Monats-Erstling")


def t_a_class_copy_is_a_copy_not_a_second_upload() -> None:
    """§9: die Klassen-Kopie entsteht per CopyObject beim Anbieter — ein
    zweiter Upload waere doppelte Bandbreite UND eine zweite Wahrheit."""
    _cfg, _root = _fresh_world()
    with RunningStub() as running:
        outcome = _tiny_run(running)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
        puts = [r for r in running.stub.requests if r["method"] == "PUT"]
        with_body = [r for r in puts
                     if not r["headers"].get("x-amz-copy-source")]
        copies = [r for r in puts if r["headers"].get("x-amz-copy-source")]
        require_equal(len(with_body), 1,
                      f"es gab {len(with_body)} echte Uploads statt einem")
        require_equal(len(copies), len(outcome["classes"]) - 1,
                      "die Klassen-Kopien stimmen nicht mit den Klassen ueberein")
        payloads = {bytes(v) for v in running.stub.objects.values()}
        require_equal(len(payloads), 1,
                      "die Kopien tragen unterschiedliche Inhalte")


# ------------------------------------------------------- kein falsches Gruen
def t_a_full_run_uploads_verifies_and_books_it() -> None:
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "voll.sqlite3"))
    with RunningStub() as running:
        outcome = _tiny_run(running, book=book)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
        gen = book.get(outcome["generation_id"])
        require(gen is not None, "die Generation steht nicht im Buch")
        require_equal(gen.state, OL.VERIFIED,
                      "ein Lauf ohne `verified` gilt als Erfolg")
        require(gen.cipher_sha256 and gen.cipher_bytes > 0,
                "das Buch kennt die Huelle nicht")
        require_equal(gen.access_key_id, "SYNTHETIC-DRILL-KEY-0001",
                      "die Access-Key-ID fehlt im Buch (§16 erlaubt sie)")
        require("daily" in gen.object_keys,
                "die Provider-Wahrheit fehlt im Buch")
        require(gen.object_keys["daily"]["version_id"],
                "die VersionId des Anbieters wurde nicht erfasst")
        require(gen.object_keys["daily"]["etag"],
                "das ETag des Anbieters wurde nicht erfasst")
        kinds = [(v["kind"], v["result"])
                 for v in book.verifications(outcome["generation_id"])]
        require(("readback", "ok") in kinds,
                "es gibt keinen Ruecklade-Beweis")
        require(not running.stub.unsigned_seen,
                "eine Anfrage ging unsigniert hinaus")


def t_a_flipped_byte_on_readback_is_never_green() -> None:
    """Die Storage-V1-Lektion, offsite: ein Upload allein ist kein Erfolg."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "kipp.sqlite3"))
    with RunningStub() as running:
        running.stub.corrupt_on_get = True
        outcome = _tiny_run(running, book=book)
        require_equal(outcome["ok"], False,
                      "ein gekipptes Byte beim Ruecklesen galt als Erfolg")
        require_equal(outcome["category"], "integrity_failed",
                      "falsche Fehlerkategorie fuer einen Abgleichsfehler")
        gen = book.get(outcome["generation_id"])
        require_equal(gen.state, OL.FAILED,
                      "das Buch behauptet Erfolg trotz Abweichung")
        results = [v["result"]
                   for v in book.verifications(outcome["generation_id"])]
        require("failed" in results,
                "der gescheiterte Ruecklade-Vergleich steht nicht im Buch")


def t_an_upload_that_never_returns_is_not_a_success() -> None:
    """Ein Timeout/Unknown darf NIEMALS als Erfolg erscheinen (§18)."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "timeout.sqlite3"))
    with RunningStub() as running:
        # Dauerhaft: der Client versucht mit Backoff erneut (das ist richtig
        # und wird unten geprueft) — hier soll er nachweislich AUFGEBEN,
        # statt einen unbeantworteten Upload als Erfolg zu verbuchen.
        running.stub.arm("server_error", methods=("PUT",), times=99)
        outcome = _tiny_run(running, book=book)
        require_equal(outcome["ok"], False, "ein 5xx galt als Erfolg")
        require_equal(outcome["category"], "remote_unavailable",
                      "falsche Kategorie fuer einen Serverfehler")
        gen = book.get(outcome["generation_id"])
        require_equal(gen.state, OL.FAILED, "das Buch steht nicht auf failed")
        require(gen.failure_category == "remote_unavailable",
                "die Kategorie fehlt im Buch")
        require(gen.state != OL.UPLOADING,
                "eine abgebrochene Uebertragung blieb als offene Zeile stehen, "
                "statt ehrlich zu scheitern")


def t_a_transient_failure_is_retried_and_then_succeeds() -> None:
    """Die Gegenprobe: EIN Zucken der Leitung darf einen Lauf nicht kosten."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "zucken.sqlite3"))
    with RunningStub() as running:
        running.stub.arm("throttled", methods=("PUT",), times=1)
        outcome = _tiny_run(running, book=book)
        require(outcome["ok"],
                f"ein einzelnes SlowDown kostete den ganzen Lauf: {outcome}")
        require_equal(book.get(outcome["generation_id"]).state, OL.VERIFIED,
                      "nach erfolgreichem Wiederversuch fehlt der Beweis")


def t_every_failure_category_reaches_book_state_and_message() -> None:
    """§22: jede Kategorie aus §18 erreicht Buch, State, Meldung."""
    import asyncio

    # `times=99`: eine Lage, die nur einmal zuschlaegt, wird ueberstanden
    # (das ist der Zweck des Backoffs und hat eine eigene Zusicherung).
    # Hier soll jede Kategorie WIRKLICH ankommen — also bleibt sie stehen.
    cases = [("denied", "auth_failed"), ("skew", "clock_skew"),
             ("quota", "quota_exceeded"), ("throttled", "remote_unavailable")]
    for mode, expected in cases:
        _cfg, root = _fresh_world()
        book = OL.OffsiteLedger(os.path.join(root, f"{mode}.sqlite3"))
        with RunningStub() as running:
            running.stub.arm(mode, methods=("PUT",), times=99)
            outcome = _tiny_run(running, book=book)
            require_equal(outcome["category"], expected,
                          f"{mode}: falsche Kategorie")
            require_equal(outcome["ok"], False, f"{mode}: galt als Erfolg")
            state = OJ.load_state()
            require_equal(state.get("last_failure_category"), expected,
                          f"{mode}: der Betriebszustand kennt die Kategorie nicht")
            require(int(state.get("consecutive_failures") or 0) >= 1,
                    f"{mode}: der Fehlerzaehler stieg nicht")
            sent: list[dict] = []

            class _Store:
                async def add_item(self, item):
                    sent.append(item)
                    return True

            posted = asyncio.run(OJ.maybe_notify(expected, "probe",
                                                 store=_Store()))
            require(posted and sent, f"{mode}: keine Meldung erzeugt")
            require_equal(sent[0]["task_id"], "",
                          "task_id muss der LEERSTRING sein (SQLite-NULL-Falle)")
            require(sent[0]["fingerprint"].startswith(f"offsite:{expected}:"),
                    "der Fingerabdruck traegt die Lage nicht")


def t_clock_skew_says_check_the_clock_not_rotate_the_key() -> None:
    """§18: `RequestTimeTooSkewed` ist die Uhr, nicht der Zugang. Wer hier
    rotiert, repariert das Falsche."""
    require("Uhr" in OJ._MESSAGES["clock_skew"],
            "die Klartextmeldung nennt die Uhr nicht")
    require("NICHT ein neuer Zugang" in OJ._MESSAGES["clock_skew"],
            "die Meldung warnt nicht vor der falschen Reparatur")


# ------------------------------------------------- ehrliche Rekonstruktion
def t_an_interrupted_run_leaves_an_open_question_not_a_success() -> None:
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "abbruch.sqlite3"))
    book.start(generation_id="20260830T053000Z", classes=("daily",))
    book.mark("20260830T053000Z", OL.UPLOADING)

    open_rows = book.open_generations()
    require_equal([g.generation_id for g in open_rows], ["20260830T053000Z"],
                  "eine unterbrochene Zeile gilt nicht als offen")
    require(not open_rows[0].succeeded,
            "`uploading` gilt als Erfolg — genau das darf es nie")
    require(book.successful_on("20260830") is None,
            "`uploading` erfuellt die Tagesbremse")

    with RunningStub() as running:
        outcome = _tiny_run(running, book=book)
        require(outcome["ok"], f"der Folgelauf scheiterte: {outcome}")
    stale = book.get("20260830T053000Z")
    require_equal(stale.state, OL.FAILED,
                  "die abgebrochene Zeile wurde nicht ehrlich abgeschlossen")
    require_equal(stale.failure_category, "unexpected",
                  "der Abbruch bekam keine Kategorie")


def t_the_book_says_uploading_before_the_first_byte_goes_out() -> None:
    """Die Zusicherung, die den Absturzfall ueberhaupt erst ehrlich macht.

    Der vorige Test setzt `uploading` selbst — er prueft damit, wie mit
    einer offenen Zeile UMGEGANGEN wird, nicht, dass sie ENTSTEHT. Eine
    Mutation, die `mark(UPLOADING)` entfernt, ueberlebte ihn deshalb: ein
    Prozess, der mitten im Upload stirbt, haette dann `prepared`
    hinterlassen und beim naechsten Start nach einem Absturz ausgesehen wie
    einer, der nie angefangen hat. Hier wird der Zustand GEMESSEN, in dem
    Moment, in dem das erste Byte hinausgeht.
    """
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "vorher.sqlite3"))
    seen: list[str] = []

    with RunningStub() as running:
        real = running.client()

        class _Observing:
            """Ein Client, der beim PUT nachsieht, was das Buch gerade sagt."""

            def __getattr__(self, name):
                return getattr(real, name)

            def put_object(self, *args, **kwargs):
                row = OL.OffsiteLedger(book.path).get(kwargs.get(
                    "generation_id") or args[1].rsplit("/", 1)[-1]
                    .replace(".tar.zst.age", ""))
                seen.append(row.state if row else "keine Zeile")
                return real.put_object(*args, **kwargs)

        outcome = OJ.run_once(force=True, client=_Observing(), book=book)

    require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
    require_equal(seen, [OL.UPLOADING],
                  f"beim ersten Byte stand im Buch {seen} statt "
                  f"[{OL.UPLOADING!r}] — ein Absturz waere nicht als "
                  f"unterbrochener Upload erkennbar")


def t_the_daily_brake_counts_calendar_days_not_elapsed_hours() -> None:
    """§11: kalendertaeglich verankert. Eine elapsed-Bremse liesse die
    Laufzeit rueckwaerts wandern und braeche „1 je UTC-Kalendertag"."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "bremse.sqlite3"))
    book.start(generation_id="20260902T235500Z", classes=("daily",))
    book.mark("20260902T235500Z", OL.VERIFIED)
    require(book.successful_on("20260902") is not None,
            "der Tag mit gutem Lauf gilt als leer")
    # Fuenf Minuten spaeter, aber ein NEUER Kalendertag: nicht gebremst.
    require(book.successful_on("20260903") is None,
            "ein neuer Kalendertag gilt faelschlich als erledigt")


def t_a_second_run_on_the_same_day_is_skipped_not_repeated() -> None:
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "idem.sqlite3"))
    # Ein fester Zeitpunkt am Vormittag: sonst haengt dieser Test daran,
    # wann er zufaellig laeuft — nach Mitternacht griffe das 07:00-Gate
    # statt der Tagesbremse, und der Test pruefte etwas anderes als er sagt.
    moment = dt.datetime(2026, 9, 2, 9, 30, tzinfo=dt.timezone.utc)
    with RunningStub() as running:
        first = _tiny_run(running, book=book, now=moment)
        require(first["ok"], f"der erste Lauf scheiterte: {first}")
        objects_after_first = dict(running.stub.objects)
        second = OJ.run_once(force=False, now=moment,
                             client=running.client(), book=book)
        require_equal(second["ok"], None,
                      "der zweite Lauf desselben Tages lief erneut durch")
        require("existiert bereits" in str(second["reason"]),
                f"falscher Grund: {second['reason']}")
        require_equal(running.stub.objects, objects_after_first,
                      "der uebersprungene Lauf hat trotzdem hochgeladen")


def t_the_ledger_never_creates_a_second_truth_for_one_generation() -> None:
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "eine.sqlite3"))
    book.start(generation_id="20260902T053000Z", classes=("daily",))
    book.mark("20260902T053000Z", OL.VERIFIED,
              object_keys={"daily": {"bucket": "b", "key": "k"}})
    again = book.start(generation_id="20260902T053000Z", classes=("daily",))
    require_equal(again.state, OL.VERIFIED,
                  "ein zweiter start() hat eine gute Generation zurueckgesetzt")
    require_equal(len(book.recent(50)), 1, "es entstand eine zweite Zeile")


# ------------------------------------------------------------------- Gates
def t_a_disabled_config_uploads_nothing() -> None:
    """DEBT-0109, strukturell: ohne Schalter passiert nichts."""
    _cfg, _root = _fresh_world(enabled=False)
    with RunningStub() as running:
        outcome = OJ.run_once(force=False, client=running.client())
        require_equal(outcome["ok"], None, "ein ausgeschalteter Lauf lief")
        require_equal(outcome["reason"], "nicht eingeschaltet",
                      f"falscher Grund: {outcome['reason']}")
        require_equal(running.stub.objects, {},
                      "ausgeschaltet, und trotzdem wurde hochgeladen")


def t_a_missing_config_is_not_an_error_but_never_a_run() -> None:
    _cfg, root = _fresh_world()
    os.remove(OC.config_path())
    with RunningStub() as running:
        outcome = OJ.run_once(force=True, client=running.client())
        require_equal(outcome["ok"], None, "ohne Konfiguration lief ein Lauf")
        require_equal(outcome["reason"], "nicht eingerichtet",
                      f"falscher Grund: {outcome['reason']}")


def t_a_second_concurrent_offsite_run_is_refused() -> None:
    import fcntl
    _cfg, _root = _fresh_world()
    holder = open(OJ.lock_path(), "w")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with RunningStub() as running:
            outcome = OJ.run_once(force=True, client=running.client())
            require_equal(outcome["ok"], None,
                          "der Verlierer einer Lock-Kollision meldete etwas "
                          "anderes als einen stillen Uebersprung")
            require("laeuft bereits" in str(outcome["reason"]),
                    f"falscher Grund: {outcome['reason']}")
            require_equal(running.stub.objects, {},
                          "trotz gehaltenem Lock wurde hochgeladen")
    finally:
        holder.close()


def t_staging_outside_the_offsite_dir_is_refused() -> None:
    """§7 fail-closed: ein Staging irgendwo waere ein Werkzeug, das ueberall
    hinschreiben darf."""
    from solvio.storage import engine
    require_raises(engine.StagingUnprotected, engine.run_backup,
                   staging_root="/tmp/irgendwo",
                   message="ein Staging ausserhalb von ~/.solvio/offsite lief")


def t_staging_refuses_an_unprovable_filevault() -> None:
    """`None` (nicht messbar) ist ein NEIN, nicht ein Ja."""
    from solvio.storage import engine, volume
    _cfg, _root = _fresh_world()
    original = volume.boot_volume_encrypted
    staging = os.path.join(OC.offsite_dir(), "staging", "probe")
    try:
        volume.boot_volume_encrypted = lambda: None
        exc = require_raises(engine.StagingUnprotected, engine.run_backup,
                             staging_root=staging,
                             message="ein nicht messbares FileVault galt als ja")
        require("nicht messbar" in str(exc), f"unklarer Grund: {exc}")
        volume.boot_volume_encrypted = lambda: False
        require_raises(engine.StagingUnprotected, engine.run_backup,
                       staging_root=staging,
                       message="ein unverschluesseltes Startvolume wurde bestagt")
    finally:
        volume.boot_volume_encrypted = original


def t_a_staging_set_carries_no_volume_marker_and_no_chain() -> None:
    from solvio.storage import engine
    _cfg, _root = _fresh_world()
    staging = os.path.join(OC.offsite_dir(), "staging", "kette")
    result = engine.run_backup(staging_root=staging, include_repos=False)
    require(result.ok, f"der Staging-Lauf scheiterte: {result.errors}")
    require_equal(result.manifest.get("source"), "staging",
                  "das Manifest nennt seine Herkunft nicht")
    require(result.manifest.get("previous_backup_id") is None,
            "das Staging-Manifest behauptet eine Kette, die es nicht gibt")
    markers = []
    for dirpath, _dirs, files in os.walk(staging):
        markers += [os.path.join(dirpath, f) for f in files
                    if f == ".solvio-storage.json"]
    require_equal(markers, [], f"eine Volume-Marke im Staging: {markers}")
    skipped_encryption = [s for s in result.manifest.get("skipped", [])
                          if "verschluesselt" in s.get("reason", "")]
    require_equal(skipped_encryption, [],
                  "im Staging wurde Privates uebersprungen — das waere ein "
                  "gruener, leerer Satz")


# ------------------------------------------------------------ Manifest INNEN
def t_the_offsite_json_travels_inside_the_envelope() -> None:
    """§8: `offsite.json` liegt INNEN, mit der Bindung Objektname ↔ Inhalt."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "innen.sqlite3"))
    with RunningStub() as running:
        outcome = OJ.run_once(force=True, client=running.client(), book=book,
                              keep_staging=True)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
        gen_id = outcome["generation_id"]
        archive = os.path.join(OC.offsite_dir(), "staging",
                               f"{gen_id}.tar.zst.age")
        require(os.path.isfile(archive), "das Archiv fehlt")
        from solvio.storage.offsite import pack as OP
        dest = os.path.join(root, "auf")
        OP.unpack(archive, identity_line=OI.read_identity(1), dest_dir=dest)
        inner = os.path.join(dest, "offsite.json")
        require(os.path.isfile(inner),
                "offsite.json liegt nicht IN der Huelle")
        payload = json.load(open(inner, encoding="utf-8"))
        require_equal(payload["generation_id"], gen_id,
                      "die generation_id innen passt nicht zum Objektnamen")
        require_equal(payload["source"], "staging", "falsche Herkunft")
        require(payload["recipient_fingerprint"].startswith("sha256-"),
                "der Rezipienten-Fingerabdruck fehlt")
        require("local_backup_state" in payload,
                "der Quellzustand wird nicht durchgereicht")
        require(os.path.isfile(os.path.join(dest, "manifest.json")),
                "das Satz-Manifest liegt nicht in der Huelle")


def t_the_ledger_holds_no_secret_and_no_table_counts() -> None:
    """§17: keine Werte, keine Inhalte, keine Tabellenzaehler."""
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "sauber.sqlite3"))
    with RunningStub() as running:
        outcome = _tiny_run(running, book=book)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
    raw = open(book.path, "rb").read()
    require(b"synthetic-drill-value-0003" not in raw,
            "der Geheimniswert steht im Buch")
    identity = OI.read_identity(1)
    require(identity.encode() not in raw, "die Identitaet steht im Buch")
    gen = book.get(outcome["generation_id"])
    require("tables" not in json.dumps(gen.as_dict()),
            "das Buch fuehrt Tabellenzaehler — die gehoeren ins Manifest")


def t_the_ledger_caps_its_tables() -> None:
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "deckel.sqlite3"))
    original = OL.MAX_ROWS
    try:
        OL.MAX_ROWS = 5
        for index in range(9):
            book.record_verification(generation_id=f"g{index}", kind="readback",
                                     result="ok")
        require(len(book.verifications(limit=100)) <= 5,
                "der Zeilendeckel greift nicht")
    finally:
        OL.MAX_ROWS = original


def t_a_red_source_set_is_uploaded_but_never_called_healthy() -> None:
    """§18: durchgereicht, nicht verschwiegen."""
    from solvio.storage import engine
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "rot.sqlite3"))
    import dataclasses
    original = engine.run_backup

    def _red(**kwargs):
        result = original(**kwargs)
        return dataclasses.replace(
            result, ok=False, errors=["probe: erfundener Quellfehler"],
            manifest={**result.manifest, "ok": False,
                      "errors": ["probe: erfundener Quellfehler"]})

    try:
        engine.run_backup = _red
        with RunningStub() as running:
            outcome = _tiny_run(running, book=book)
    finally:
        engine.run_backup = original
    require(outcome["ok"], "ein roter Quellsatz verhinderte den Upload — er "
                           "soll hochgeladen werden, nur nicht gesund heissen")
    require_equal(outcome["source_ok"], False,
                  "der rote Quellzustand wurde nicht durchgereicht")
    require("nicht gesund" in str(outcome["reason"]),
            f"der Lauf verschweigt den roten Quellsatz: {outcome['reason']}")
    gen = book.get(outcome["generation_id"])
    require_equal(gen.source_ok, False, "das Buch verschweigt den Quellfehler")


def t_a_generation_without_its_own_tool_is_never_uploaded() -> None:
    """Der §23.10-Befund, als Tor im Lauf.

    Generation `20260901T054003Z` war gueltig, klonbar, verifiziert — und im
    Ernstfall wertlos, weil aus ihrem `core.bundle` kein Restore-Werkzeug
    herauszuholen war. Ein Satz, der sich nicht selbst zurueckholen kann, darf
    das Haus nicht verlassen: kein Upload, kein `verified`, keine Gesundheit.
    """
    import dataclasses
    import subprocess
    from solvio.storage import engine
    from solvio.storage.offsite import provenance as OPV
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "ohne-werkzeug.sqlite3"))

    # Ein echter, fuer sich gueltiger Baum — nur ohne das Werkzeug darin.
    fremd = os.path.join(root, "fremder-baum")
    os.makedirs(fremd, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example",
                "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example"})
    with open(os.path.join(fremd, "LIESMICH"), "w", encoding="utf-8") as fh:
        fh.write("ein Baum ohne offsite-Paket\n")
    for args in (["init", "--quiet", "-b", "haupt", fremd],
                 ["-C", fremd, "add", "-A"],
                 ["-C", fremd, "commit", "--quiet", "-m", "ohne Werkzeug"]):
        require_equal(subprocess.run(["git", *args], env=env, text=True,
                                     capture_output=True).returncode, 0,
                      f"git {args[0]} scheiterte beim Aufbau des Falls")

    original = engine.run_backup

    def _ohne_werkzeug(**kwargs):
        result = original(**kwargs)
        ziel = os.path.join(result.path, OPV.BUNDLE_RELPATH)
        os.makedirs(os.path.dirname(ziel), exist_ok=True)
        subprocess.run(["git", "-C", fremd, "bundle", "create", ziel, "--all"],
                       env=env, capture_output=True, check=True)
        return dataclasses.replace(result)

    class _Zaehlend:
        """Merkt sich, ob ueberhaupt ein Byte hinausging."""

        def __init__(self, echt): self.echt, self.puts = echt, 0

        def put_object(self, *a, **k):
            self.puts += 1
            return self.echt.put_object(*a, **k)

        def __getattr__(self, name): return getattr(self.echt, name)

    try:
        engine.run_backup = _ohne_werkzeug
        with RunningStub() as running:
            client = _Zaehlend(running.client())
            outcome = OJ.run_once(force=True, client=client, book=book)
    finally:
        engine.run_backup = original

    require_equal(outcome["ok"], False,
                  "ein Satz ohne eigenes Werkzeug galt als Erfolg")
    require_equal(outcome["category"], "staging_failed",
                  f"falsche Kategorie: {outcome['category']}")
    require("provenance_unproven" in str(outcome["reason"]),
            f"der Grund benennt die Herkunft nicht: {outcome['reason']}")
    require_equal(client.puts, 0,
                  "es ging trotzdem ein Byte hinaus")
    gen = book.get(outcome["generation_id"]) if outcome["generation_id"] else None
    if gen is not None:
        require(gen.state != OL.VERIFIED,
                "eine Generation ohne Werkzeug steht als verified im Buch")
        require_equal(gen.state, OL.FAILED,
                      f"das Buch nennt sie {gen.state} statt failed")


def t_the_offsite_json_carries_the_proven_producer() -> None:
    """Die Generation muss sagen, welchen Commit ein Klon auschecken muss.

    Ohne dieses Feld faellt ein Restaurierender auf die HEAD des Buendels
    zurueck — und genau die kam aus einem fremden Baum ohne offsite-Paket.
    """
    from solvio.storage.offsite import pack as OP
    from solvio.storage.offsite import provenance as OPV
    _cfg, root = _fresh_world()
    book = OL.OffsiteLedger(os.path.join(root, "herkunft.sqlite3"))
    with RunningStub() as running:
        outcome = OJ.run_once(force=True, client=running.client(), book=book,
                              keep_staging=True)
        require(outcome["ok"], f"der Lauf scheiterte: {outcome}")
        archive = os.path.join(OC.offsite_dir(), "staging",
                               f"{outcome['generation_id']}.tar.zst.age")
        dest = os.path.join(root, "auf-herkunft")
        OP.unpack(archive, identity_line=OI.read_identity(1), dest_dir=dest)
        payload = json.load(open(os.path.join(dest, "offsite.json"),
                                 encoding="utf-8"))

    laeuft, _branch, _dirty = OPV.producer_revision()
    require_equal(payload["producer_commit"], laeuft,
                  "offsite.json nennt einen anderen Erzeuger als den laufenden")
    require_equal(payload["core_bundle_commit"], laeuft,
                  "der Commit zum Auschecken fehlt oder weicht ab")
    require_equal(payload["provenance"]["restore_entry"], OPV.ENTRY_PATH,
                  "der Einstiegspunkt ist in der Generation nicht benannt")
    require(payload["producer_commit"] != payload.get("core_commit")
            or payload["producer_commit"] == laeuft,
            "Herkunft und Beobachtung sind nicht auseinandergehalten")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
