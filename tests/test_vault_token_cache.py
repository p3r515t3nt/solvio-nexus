"""DEBT-0193 — ein warmes Zugangstoken ist keine Befugnis.

Gemessen am 2026-09-03: `document_find` lief gegen den echten Core mit echten
Ergebnissen, obwohl die Tresor-Richtlinie diese Faehigkeit nicht fuehrte. Der
Grund war kein Loch im Riegel, sondern dass er nie gefragt wurde:
`_token()` gab ein bereits geholtes Zugangstoken zurueck, BEVOR
`_credentials()` — und damit der Tresor — an die Reihe kam. Eine andere,
erlaubte Faehigkeit hatte das Token kurz zuvor auf derselben gemeinsamen
Instanz geholt.

Der Riegel selbst hielt immer: auf einer KALTEN Instanz verweigerte der Tresor
korrekt. Nur die Reihenfolge war falsch.

Diese Suite stellt beide Zustaende — kalt UND warm — gegen beide
Google-Flaechen, und den Fall, der zwischen ihnen liegt: ein Scope, der NACH
dem Aufwaermen entzogen wird.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.

Direkt: python tests/test_vault_token_cache.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

from solvio.capabilities import policy as AP                    # noqa: E402
from solvio.integrations.gmail import Gmail                     # noqa: E402
from solvio.integrations.google_calendar import GoogleCalendar  # noqa: E402
from solvio.secret_vault import policy as VP                    # noqa: E402
from solvio.secret_vault.broker import SecretDenied             # noqa: E402

ERLAUBT = "gmail_search"
NICHT_ERLAUBT = "document_find"


class _Tresor:
    """Ein Tresor, der nur eine Frage beantwortet: steht die Faehigkeit drin?

    Bewusst klein: die vollstaendige Entscheidungskette ist in
    `test_secret_vault.py` geprueft. Hier geht es um die REIHENFOLGE — wird
    ueberhaupt gefragt, bevor geantwortet wird.
    """

    def __init__(self, erlaubte: set[str], *, nur_fuer: set[str] | None = None) -> None:
        self.erlaubte = set(erlaubte)
        #: Verweise, auf denen der Scope NICHT mehr gilt. Ein Entzug trifft in
        #: der Wirklichkeit selten beide Eintraege im selben Augenblick.
        self.entzogen: set[str] = set(nur_fuer or ())
        self.gefragt: list[tuple[str, str]] = []
        self.geoeffnet: list[str] = []

    def exists(self, ref: str) -> bool:
        return True

    def describe(self, ref):
        return {"version": 1, "rotated_at": ""}

    def _pruefe(self, ref: str, capability: str) -> None:
        from solvio.secret_vault import context as SC
        name = capability or SC.current().capability
        self.gefragt.append((name, ref))
        if name not in self.erlaubte or ref in self.entzogen:
            raise SecretDenied(VP.Denied.CAPABILITY_NOT_ALLOWED, ref)

    def authorize(self, ref: str, *, executor, target: str,
                  capability: str = "", origin=None) -> None:
        self._pruefe(ref, capability)

    def use(self, ref: str, *, executor, target: str, **_):
        self._pruefe(ref, "")
        self.geoeffnet.append(ref)
        return _Leihe()


class _Leihe:
    def __enter__(self):
        return _Material()

    def __exit__(self, *_):
        return False


class _Material:
    def plaintext(self) -> str:
        return "synthetisch-kein-echter-wert"


def _kontext(capability: str):
    from solvio.secret_vault import context as SC
    return SC.bound(SC.UseContext(origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                                  capability=capability))


def _client(name: str, tresor):
    """Beide Google-Flaechen, gleich gebaut, gleich zu pruefen."""
    if name == "gmail":
        return Gmail(client_id="c", broker=tresor)
    return GoogleCalendar(client_id="c", broker=tresor)


FLAECHEN = ("gmail", "calendar")


def _waerme(client) -> None:
    """Ein Token in den Speicher legen, ohne den Tresor zu beruehren — genau
    die Lage nach einem erlaubten Aufruf einer ANDEREN Faehigkeit."""
    client._access_token = "warmes-token"
    client._expires_at = time.monotonic() + 3600.0
    client._token_credential_versions = client._credential_versions()


def _grund(client, capability: str) -> str:
    try:
        with _kontext(capability):
            asyncio.run(client._token())
    except SecretDenied as exc:
        return getattr(exc.reason, "value", str(exc.reason))
    except Exception as exc:                     # noqa: BLE001
        return f"falsche_ausnahme:{type(exc).__name__}"
    return ""


# ============================================================ 1 + 2
def t_a_capability_outside_the_scope_is_denied_cold_and_warm():
    """Die beiden Faelle, die der Fund unterscheidet.

    Kalt war immer richtig. Warm war der Fehler — und beide muessen dasselbe
    sagen, sonst haengt die Sicherheit am Zufall des Zeitpunkts.
    """
    for name in FLAECHEN:
        for lage in ("kalt", "warm"):
            tresor = _Tresor({ERLAUBT})
            client = _client(name, tresor)
            if lage == "warm":
                _waerme(client)
            require_equal(_grund(client, NICHT_ERLAUBT), "capability_not_allowed",
                          f"{name}/{lage}: nicht verweigert")
            require(tresor.gefragt, f"{name}/{lage}: der Tresor wurde nie gefragt")


# ============================================================ 3
def t_an_allowed_capability_works_with_a_warm_token():
    """Die Gegenprobe. Ohne sie waere die Regel auch mit einem Riegel
    zufrieden, der ALLES verweigert."""
    for name in FLAECHEN:
        tresor = _Tresor({ERLAUBT})
        client = _client(name, tresor)
        _waerme(client)
        with _kontext(ERLAUBT):
            token = asyncio.run(client._token())
        require_equal(token, "warmes-token", f"{name}: das warme Token kam nicht durch")
        require_equal(tresor.geoeffnet, [],
                      f"{name}: der Tresor wurde geoeffnet, obwohl ein Token da war")
        require(tresor.gefragt, f"{name}: gefragt werden muss trotzdem")


# ============================================================ 5
def t_revoking_the_scope_stops_the_next_use_of_a_warm_token():
    """Der Fall, der den Unterschied ausmacht.

    Ein Token, das unter gueltigem Scope geholt wurde, darf nach dem Entzug
    nicht weiterlaufen. Sonst waere ein Entzug erst wirksam, wenn das Token von
    selbst ablaeuft — bis zu einer Stunde spaeter.
    """
    for name in FLAECHEN:
        tresor = _Tresor({ERLAUBT})
        client = _client(name, tresor)
        _waerme(client)
        with _kontext(ERLAUBT):
            require_equal(asyncio.run(client._token()), "warmes-token",
                          f"{name}: Vorbedingung")

        tresor.erlaubte.discard(ERLAUBT)          # der Entzug
        require_equal(_grund(client, ERLAUBT), "capability_not_allowed",
                      f"{name}: der Entzug wirkte nicht auf das warme Token")


# ============================================================ 6
def t_no_secret_value_appears_in_the_denial():
    """Eine Ablehnung nennt den Grund, nie den Wert."""
    tresor = _Tresor({ERLAUBT})
    client = _client("gmail", tresor)
    _waerme(client)
    text = ""
    try:
        with _kontext(NICHT_ERLAUBT):
            asyncio.run(client._token())
    except SecretDenied as exc:
        text = f"{exc} {exc.__dict__}"
    require(text, "es gab gar keine Ablehnung")
    require("warmes-token" not in text, "das Token stand in der Ablehnung")
    require("synthetisch" not in text, "ein Wert stand in der Ablehnung")


def t_both_credential_refs_are_checked_not_just_the_first():
    """Ein Entzug auf NUR einem der beiden Verweise ist ein Entzug.

    Die Erneuerung braucht Client-Geheimnis UND Auffrischungs-Token. Wer nur
    den ersten prueft, laesst einen halben Entzug durch — und eine Mutation,
    die genau die zweite Zeile streicht, blieb ohne diesen Fall unbemerkt.
    """
    for name in FLAECHEN:
        client_ref = _client(name, _Tresor({ERLAUBT})).CLIENT_SECRET_REF
        refresh_ref = _client(name, _Tresor({ERLAUBT})).REFRESH_TOKEN_REF
        for entzogen in (client_ref, refresh_ref):
            tresor = _Tresor({ERLAUBT}, nur_fuer={entzogen})
            client = _client(name, tresor)
            _waerme(client)
            require_equal(_grund(client, ERLAUBT), "capability_not_allowed",
                          f"{name}: Entzug auf {entzogen} blieb wirkungslos")


def t_the_real_broker_authorises_against_the_real_policy():
    """Der echte `SecretBroker.authorize()`, nicht die Attrappe.

    Die Faelle oben pruefen die REIHENFOLGE in den Integrationen; sie koennten
    alle gruen bleiben, waehrend `authorize()` selbst jede Anfrage durchwinkt.
    Eine Mutation, die genau das tat, ueberlebte — bis dieser Fall dazukam.

    Es wird nichts entschluesselt: `authorize()` liest die Richtlinie und
    entscheidet. Deshalb braucht dieser Fall keinen Wert und keinen Schluessel.
    """
    import tempfile
    from solvio.secret_vault import admin, keyring as K
    from solvio.secret_vault.store import VaultStore
    from solvio.secret_vault.broker import SecretBroker

    wurzel = tempfile.mkdtemp(prefix="solvio-authorize-")
    alt_dir = os.environ.get("SOLVIO_VAULT_DIR")
    alt_keys = os.environ.get("SOLVIO_VAULT_TEST_KEYSTORE")
    os.environ["SOLVIO_VAULT_DIR"] = wurzel
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(wurzel, "keys")
    try:
        K.forget_kek()
        store = VaultStore()
        admin.initialize(store)
        ref = "secret://google/refresh"
        admin.add(secret_ref=ref, kind=VP.SecretKind.OAUTH_REFRESH_TOKEN,
                  plaintext=b"synthetisch-nie-echt",
                  allowed_capabilities=(ERLAUBT,),
                  allowed_targets=("https://oauth2.googleapis.com",),
                  allowed_executors=(VP.ExecutorId.HTTP,),
                  display_name="Google", service_label="Google",
                  account_label="Test", allow_background=True, store=store)
        # Auch der zweite Verweis, den die Erneuerung braucht.
        admin.add(secret_ref="secret://google/oauth-client",
                  kind=VP.SecretKind.OAUTH_CLIENT_SECRET,
                  plaintext=b"synthetisch-nie-echt",
                  allowed_capabilities=(ERLAUBT,),
                  allowed_targets=("https://oauth2.googleapis.com",),
                  allowed_executors=(VP.ExecutorId.HTTP,),
                  display_name="Google", service_label="Google",
                  account_label="Test", allow_background=True, store=store)
        broker = SecretBroker(store)

        # Gerufen wird ueber die ECHTE Integration, nicht direkt: nur so ist
        # der Rahmen des Aufrufers `solvio.integrations.gmail` und die
        # Executor-Modulbindung des Tresors erfuellt. Das prueft die ganze
        # Kette statt nur die eine Methode.
        client = Gmail(client_id="c", broker=broker)
        _waerme(client)
        with _kontext(ERLAUBT):
            require_equal(asyncio.run(client._token()), "warmes-token",
                          "der echte Broker verweigerte eine erlaubte Faehigkeit")

        require_equal(_grund(client, NICHT_ERLAUBT), "capability_not_allowed",
                      "der echte Broker winkte eine fremde Faehigkeit durch")

        # DEBT-0218: die echten Migrationsbereiche muessen auch die drei
        # weiteren Kalender-Lesewege tragen — auf BEIDEN Google-Verweisen.
        from solvio.secret_vault.migration import ENV_PLANS
        from solvio.capabilities.calendar import SPECS as CALENDAR_SPECS
        reads = ("calendar_get_event", "calendar_search_events",
                 "calendar_find_availability")
        plans = {p.secret_ref: p for p in ENV_PLANS
                 if p.secret_ref in {"secret://google/oauth-client",
                                     "secret://google/refresh"}}
        require_equal(len(plans), 2)
        for plan in plans.values():
            admin.rescope(secret_ref=plan.secret_ref,
                          allowed_capabilities=plan.allowed_capabilities, store=store)
        calendar = GoogleCalendar(client_id="c", broker=broker)
        _waerme(calendar)
        for capability in reads:
            require(CALENDAR_SPECS[capability].is_read_only(), capability)
            with _kontext(capability):
                require_equal(asyncio.run(calendar._token()), "warmes-token",
                              f"Migrationsbereich verweigert {capability}")
        for ref, plan in plans.items():
            for capability in reads:
                admin.rescope(secret_ref=ref,
                              allowed_capabilities=tuple(c for c in plan.allowed_capabilities
                                                         if c != capability), store=store)
                _waerme(calendar)
                require_equal(_grund(calendar, capability), "capability_not_allowed",
                              f"Entzug nur auf {ref} liess {capability} durch")
                admin.rescope(secret_ref=ref,
                              allowed_capabilities=plan.allowed_capabilities, store=store)
    finally:
        K.forget_kek()
        for schluessel, wert in (("SOLVIO_VAULT_DIR", alt_dir),
                                 ("SOLVIO_VAULT_TEST_KEYSTORE", alt_keys)):
            if wert is None:
                os.environ.pop(schluessel, None)
            else:
                os.environ[schluessel] = wert


def t_authorize_keeps_the_executor_module_binding():
    """Auch die Frage bindet an das Modul des Fragenden.

    `use()` nimmt den Rahmen des Aufrufers, damit ein Executor sich seine
    Herkunft nicht aussuchen kann. Waere `authorize()` da nachlaessiger, waere
    sie der bequemere Weg — und der bequemere Weg wird genommen.

    Live gesehen beim Schreiben dieser Suite: ein direkter Aufruf aus der
    Testdatei wurde mit `executor_module_mismatch` abgewiesen. Genau richtig.
    """
    import tempfile
    from solvio.secret_vault import admin, keyring as K
    from solvio.secret_vault.store import VaultStore
    from solvio.secret_vault.broker import SecretBroker

    wurzel = tempfile.mkdtemp(prefix="solvio-bindung-")
    alt_dir = os.environ.get("SOLVIO_VAULT_DIR")
    alt_keys = os.environ.get("SOLVIO_VAULT_TEST_KEYSTORE")
    os.environ["SOLVIO_VAULT_DIR"] = wurzel
    os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(wurzel, "keys")
    try:
        K.forget_kek()
        store = VaultStore()
        admin.initialize(store)
        ref = "secret://google/refresh"
        admin.add(secret_ref=ref, kind=VP.SecretKind.OAUTH_REFRESH_TOKEN,
                  plaintext=b"synthetisch-nie-echt",
                  allowed_capabilities=(ERLAUBT,),
                  allowed_targets=("https://oauth2.googleapis.com",),
                  allowed_executors=(VP.ExecutorId.HTTP,),
                  display_name="Google", service_label="Google",
                  account_label="Test", allow_background=True, store=store)

        # Von HIER aus — einem fremden Modul — muss sie scheitern.
        grund = ""
        try:
            with _kontext(ERLAUBT):
                SecretBroker(store).authorize(
                    ref, executor=VP.ExecutorId.HTTP,
                    target="https://oauth2.googleapis.com")
        except SecretDenied as exc:
            grund = getattr(exc.reason, "value", str(exc.reason))
        require_equal(grund, "executor_module_mismatch",
                      "ein fremdes Modul durfte fragen")
    finally:
        K.forget_kek()
        for schluessel, wert in (("SOLVIO_VAULT_DIR", alt_dir),
                                 ("SOLVIO_VAULT_TEST_KEYSTORE", alt_keys)):
            if wert is None:
                os.environ.pop(schluessel, None)
            else:
                os.environ[schluessel] = wert


def t_an_authorisation_is_not_booked_as_a_use():
    """Eine Frage ist keine Nutzung.

    Wuerde `authorize()` jeden Cache-Treffer als `used` buchen, waere die
    Zugriffsspur unwahr — sie wuerde Geheimnisnutzungen zeigen, die nie
    stattgefunden haben. Eine ABLEHNUNG gehoert dagegen ins Buch.
    """
    import inspect
    from solvio.secret_vault.broker import SecretBroker

    quelle = inspect.getsource(SecretBroker.authorize)
    require("OUTCOME_USED" not in quelle and "touch_used" not in quelle,
            "eine blosse Frage wird als Nutzung gebucht")
    require("_deny(" in quelle, "eine Ablehnung wird nicht gebucht")


def t_the_cache_return_asks_before_it_answers():
    """Die Reihenfolge, strukturell.

    Der Fund war eine Reihenfolge, kein fehlender Riegel — deshalb wird sie
    hier auch als Reihenfolge geprueft: im Cache-Zweig steht die Frage VOR der
    Rueckgabe, in beiden Flaechen.
    """
    import inspect
    for modul in ("solvio.integrations.gmail", "solvio.integrations.google_calendar"):
        quelle = inspect.getsource(sys.modules[modul])
        start = quelle.index("    async def _token(")
        zweig = quelle[start:start + 600]
        require("_authorise_cached()" in zweig,
                f"{modul}: der Cache-Zweig fragt gar nicht")
        require(zweig.index("_authorise_cached()") < zweig.index("return self._access_token"),
                f"{modul}: die Rueckgabe steht vor der Frage")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
