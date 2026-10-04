"""Der Kaefig als Angreifer — nicht als Nutzer.

Angreifer **B** aus dem Bedrohungsmodell: beliebiger Code als Kaefigprozess,
aber Seatbelt haelt. Er baut seine Rueckfrage von Hand, er liest jede Datei im
Kaefig (auch die Token seiner Geschwister), und er hat Zeit. Was er NICHT hat,
ist ein Weg, sich selbst Autoritaet zu geben.

Diese Suite fragt nur eines: kommt er irgendwo durch, wo er nicht durchkommen
soll? Sie prueft deshalb nicht das gute Verhalten, sondern die Absage — und sie
prueft an jeder Stelle auch, dass **kein Anruf nach draussen** stattgefunden
hat. Eine Absage, die vorher schon Geld ausgegeben hat, ist keine.

Der wichtigste Satz, den sie belegt: **ein Modell darf anfragen, nie sich selbst
genehmigen.** Es gibt keine HTTP-Route, auf der der Kaefig ein Lease oeffnen,
einen Auftraggeber anlegen oder einen Token praegen koennte — die Steuerebene
ist ein Python-Handle im Core-Prozess.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_provider_broker_adversarial.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from _broker_fixtures import (  # noqa: E402
    FAKE_PROVIDER_KEY, Harness, bot_principal_name, responses_body, run,
)

from solvio.provider_broker import proxy as px  # noqa: E402
from solvio.provider_broker import session as sess  # noqa: E402


# ------------------------------------------------- Sich selbst genehmigen

def t_the_cage_cannot_open_a_lease_for_itself():
    """Die Steuerebene hat keine Route. Das ist die tragende Trennung."""
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            attempts = [
                ("POST", "/v1/lease"), ("POST", "/lease"),
                ("POST", "/v1/register"), ("POST", "/register"),
                ("POST", "/v1/admin/lease"), ("POST", "/admin"),
                ("POST", "/v1/tokens"), ("POST", "/rotate"),
                ("GET", "/v1/leases"), ("PUT", "/v1/responses"),
                ("DELETE", "/v1/responses"), ("PATCH", "/v1/responses"),
            ]
            for method, path in attempts:
                status, _ = await h.call(token, path=path, method=method,
                                         body=b'{"principal":"deep-gateway"}')
                require(status in (403, 404, 405),
                        f"{method} {path} oeffnet nichts")
            require(not h.broker.registry.has_live_lease("deep-gateway",
                                                         now=time.time()),
                    "nach allen Versuchen gibt es kein Lease")
            require_equal(len(h.upstream.calls), 0, "und keinen Anruf nach draussen")
    run(go())


def t_a_claimed_lease_in_a_header_or_body_is_worth_nothing():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            status, _ = await h.call(
                token,
                headers={"X-Solvio-Lease": "lease-deadbeef",
                         "X-Lease-Id": "lease-deadbeef",
                         "X-Solvio-Principal": "deep-gateway"},
                body=json.dumps({"model": "gpt-5.4-mini", "input": "x",
                                 "lease_id": "lease-deadbeef",
                                 "user_authorized": True}).encode())
            require_equal(status, 403, "eine behauptete Leihe ist keine")
            require_equal(len(h.upstream.calls), 0, "nichts geht raus")
    run(go())


# ------------------------------------------------------------ Das Modelltor

def t_a_duplicate_model_written_as_a_unicode_escape_is_still_a_duplicate():
    """`\\u006dodel` dekodiert zu `model` — der Zaehler darf das nicht uebersehen."""
    body = b'{"model":"gpt-5.4-mini","\\u006dodel":"gpt-4o"}'
    refused = False
    try:
        px.parse_model(body)
    except px.BodyRejected:
        refused = True
    require(refused, "zwei Schluessel bleiben zwei, wie auch immer geschrieben")


def t_a_model_hidden_behind_deep_nesting_does_not_count():
    body = json.dumps({
        "a": {"b": {"c": {"d": {"model": "gpt-5.4-mini"}}}},
        "model": "gpt-4o",
    }).encode()
    refused = False
    try:
        px.parse_model(body)
    except px.BodyRejected:
        refused = True
    require(refused, "ein tief vergrabener Koeder rettet kein fremdes Modell")


def t_a_model_in_a_list_of_objects_does_not_count():
    body = json.dumps({"input": [{"model": "gpt-5.4-mini"}], "model": "gpt-4o"}).encode()
    refused = False
    try:
        px.parse_model(body)
    except px.BodyRejected:
        refused = True
    require(refused, "auch in einer Liste ist es nicht die oberste Ebene")


def t_case_variations_of_the_model_name_are_not_the_model():
    for name in ("GPT-5.4-MINI", "gpt-5.4-Mini", " gpt-5.4-mini",
                 "gpt-5.4-mini ", "gpt-5.4-mini\\u0000", "openai/gpt-5.4-mini",
                 "gpt-5.4-mini-evil", "evil-gpt-5.4-mini"):
        refused = False
        try:
            px.parse_model(json.dumps({"model": name}).encode())
        except px.BodyRejected:
            refused = True
        require(refused, f"{name!r} ist nicht das freigegebene Modell")


def t_a_body_that_is_not_utf8_is_refused():
    refused = False
    try:
        px.parse_model(b'{"model":"\xff\xfe"}')
    except px.BodyRejected:
        refused = True
    require(refused, "unlesbare Bytes sind kein Rumpf")


def t_an_absurdly_nested_body_does_not_crash_the_gate():
    """Ein Angreifer darf den Broker nicht mit einem Rumpf umlegen."""
    body = b"[" * 2000 + b"]" * 2000
    refused = False
    try:
        px.parse_model(body)
    except px.BodyRejected:
        refused = True
    require(refused, "tiefe Verschachtelung wird abgewiesen, nicht verschluckt")


# --------------------------------------------------------------- Das Pfadtor

def t_path_case_and_shape_variations_do_not_reach_upstream():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            # Nur Schreibweisen, die den Broker auch WIRKLICH so erreichen.
            # `/./v1/responses`, `/v1/responses/../responses` und
            # `/%76%31/responses` loest schon der Klient auf — `yarl` dekodiert
            # die Prozentform, bevor etwas auf den Draht geht. Ueber das Tor
            # sagen sie deshalb nichts. Ein Angreifer mit einem rohen Socket
            # kann sie sehr wohl senden; der Server dekodiert dann NICHT, und
            # genau dieser Fall wird unten direkt an `canonical_path` geprueft.
            for path in ("/V1/responses", "/v1/RESPONSES", "/v1/responses/",
                         "/v1//responses"):
                status, _ = await h.call(token, path=path)
                require(status in (404, 400, 301, 308),
                        f"{path} ist kein Listeneintrag")
            require_equal(len(h.upstream.calls), 0,
                          "keine Schreibweise erreicht den Anbieter")
    run(go())


def t_the_checked_path_and_the_forwarded_path_are_the_same_value():
    """Es gibt keine zweite Lesart zwischen Pruefung und Weiterleitung.

    Der rohe Pfad wird woertlich gegen die Freigabeliste gehalten und woertlich
    weitergereicht. Damit ist eine kodierte Traversierung kein Sonderfall, den
    jemand bedacht haben muss, sondern schlicht kein Listeneintrag.
    """
    class _Url:
        def __init__(self, raw, query=""):
            self.raw_path = raw
            self.query_string = query

    class _Request:
        def __init__(self, raw, query=""):
            self.rel_url = _Url(raw, query)
            self.path = raw

    for raw in ("/v1/%2e%2e/admin", "/V1/responses", "/v1/responses/",
                "/v1//responses", "/%76%31/responses", "/v1/responses\x00"):
        got = px.canonical_path(_Request(raw))
        require(got not in px.FORWARDED_PATHS,
                f"{raw!r} ist kein Eintrag der Freigabeliste")
    require_equal(px.canonical_path(_Request("/v1/responses")), "/v1/responses",
                  "der kanonische Pfad geht unveraendert durch")
    require_equal(px.canonical_path(_Request("/v1/responses", "x=1")), "",
                  "eine Abfragezeichenkette macht den Pfad ungueltig")


def t_an_absolute_uri_as_the_path_does_not_redirect_the_broker():
    """SSRF ist hier nicht gemildert, sondern strukturell abwesend."""
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            # Eine absolute Anfragezeile (`http://…`) kann dieser Klient gar
            # nicht senden — sie ist deshalb hier nicht pruefbar. Was pruefbar
            # ist und den Punkt traegt: der Broker liest NIRGENDS einen Wirt aus
            # der Anfrage, das Ziel steht im Code.
            for path in ("//evil.example/v1/responses",
                         "/v1/responses@evil.example"):
                status, _ = await h.call(token, path=path)
                require(status in (404, 400), f"{path} benennt kein Ziel")
            require_equal(len(h.upstream.calls), 0, "nichts erreicht einen fremden Wirt")
    run(go())


def t_a_host_header_cannot_move_the_upstream():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            status, _ = await h.call(token, headers={
                "Host": "evil.example",
                "X-Forwarded-Host": "evil.example",
                "X-Original-URL": "http://evil.example/v1/responses",
            })
            require_equal(status, 200, "die Anfrage selbst ist gueltig")
            require_equal(len(h.upstream.calls), 1, "und geht an das gepinnte Ziel")
            sent = h.upstream.calls[0]["headers"]
            require_equal(sent.get("host", ""), "api.openai.com",
                          "der Wirt kommt vom Broker, nicht aus der Anfrage")
            require("x-forwarded-host" not in sent, "und der Vorschlag faellt weg")
            require("x-original-url" not in sent, "dieser auch")
    run(go())


# ------------------------------------------------------------ Zwischen Kaefigen

def t_a_sibling_token_cannot_ride_another_principals_lease():
    """Alle Kaefig-Token sind gegenseitig LESBAR — aber nicht austauschbar.

    Ein Botprofil liest den Token des Gateways: ein Kaefig, `file-read*` ueber
    den ganzen Unterpfad. Token je Auftraggeber kaufen deshalb unabhaengige
    Widerrufbarkeit und Zuordnung, keine Vertraulichkeit untereinander. Was sie
    aber sehr wohl kaufen: das Lease des einen traegt den anderen nicht.
    """
    async def go():
        async with Harness() as h:
            h.broker.register_principal("deep-gateway")
            bot = h.broker.register_principal(bot_principal_name("solvio-researcher"))
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            status, _ = await h.call(bot)
            require_equal(status, 403,
                          "das Lease von Deep traegt den Bot nicht")
            require_equal(len(h.upstream.calls), 0, "und nichts geht raus")
    run(go())


def t_a_sibling_cannot_spend_another_principals_token_budget():
    async def go():
        async with Harness() as h:
            deep = h.broker.register_principal("deep-gateway")
            bot = h.broker.register_principal(bot_principal_name("solvio-researcher"))
            h.broker.registry.principal("deep-gateway").caps = sess.Caps(
                tokens_per_day=5)
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            h.broker.open_lease(bot_principal_name("solvio-researcher"), "q",
                                deadline=time.time() + 60)
            status, _ = await h.call(deep)
            require_equal(status, 429, "Deep steht an seiner Kappe")
            status, _ = await h.call(bot)
            require_equal(status, 200, "der Bot ist davon unberuehrt")
    run(go())


# ------------------------------------------------------------------ Rotation

def t_the_old_token_dies_the_moment_the_last_lease_closes():
    async def go():
        async with Harness() as h:
            old = h.broker.register_principal("deep-gateway")
            first = h.broker.open_lease("deep-gateway", "a", deadline=time.time() + 60)
            second = h.broker.open_lease("deep-gateway", "b", deadline=time.time() + 60)
            h.broker.close_lease(first)
            status, _ = await h.call(old)
            require_equal(status, 200, "solange ein Geschwister lebt, traegt er")
            h.broker.close_lease(second)
            status, _ = await h.call(old)
            require_equal(status, 401, "beim letzten Schluss ist er tot")
    run(go())


def t_a_closed_lease_cannot_be_reopened_by_repeating_its_id():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            lease = h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            h.broker.close_lease(lease)
            h.broker.close_lease(lease)          # doppelt schliessen tut nichts
            fresh = h.broker.registry.principal("deep-gateway").token
            status, _ = await h.call(fresh)
            require_equal(status, 403,
                          "ein zweimal geschlossenes Lease ist kein offenes")
            require_equal(len(h.upstream.calls), 0, "und nichts geht raus")
            require(token != fresh, "rotiert wurde trotzdem genau einmal")
    run(go())


# ----------------------------------------------------------------- Die Kappen

def t_a_huge_requested_output_cannot_slip_past_the_token_cap():
    body = responses_body(max_output_tokens=250_000)
    estimate = px.estimate_tokens(body, json.loads(body))
    require(estimate > 200_000, "eine grosse Anforderung wird gross veranschlagt")


def t_a_negative_or_absurd_max_tokens_falls_back_to_the_default():
    for value in (-1, 0, 99_000_000, "viele", None, True):
        body = responses_body(max_output_tokens=value)
        estimate = px.estimate_tokens(body, json.loads(body))
        require(estimate >= px.DEFAULT_OUTPUT_ESTIMATE,
                f"{value!r} setzt die Veranschlagung nicht auf null")


def t_an_empty_body_still_costs_something():
    require(px.estimate_tokens(b"{}", {}) > 0,
            "es gibt keine kostenlose Anfrage")


def t_a_stream_the_broker_aborts_never_reports_usage_and_keeps_its_charge():
    """Eine vom Broker abgebrochene Weiterleitung meldet nie ein `usage`."""
    from _broker_fixtures import TRUNCATED_STREAM

    async def go():
        async with Harness(body=TRUNCATED_STREAM) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token)
            rows = [r for r in h.rows() if r["outcome"] == "forwarded"]
            require_equal(rows[0]["tokens_source"], "estimated",
                          "die Schaetzung bleibt die Wahrheit dieser Zeile")
            require(rows[0]["input_tokens"] > 0, "und sie ist nicht null")
    run(go())


# --------------------------------------------------------------- Das Buch

def t_prompt_content_cannot_be_smuggled_into_the_ledger():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            poison = "GEHEIM-XYZ'; DROP TABLE broker_ledger; --"
            await h.call(token, body=json.dumps(
                {"model": "gpt-5.4-mini", "input": poison,
                 "task_ref": poison, "principal": poison,
                 "denied_reason": poison}).encode())
            blob = json.dumps(h.rows())
            require("GEHEIM-XYZ" not in blob, "kein Rumpfinhalt landet im Buch")
            require(len(h.rows()) >= 1, "und die Tabelle steht noch")
    run(go())


def t_a_ledger_row_can_never_carry_the_provider_key_even_on_an_upstream_error():
    async def go():
        async with Harness(status=500) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token)
            blob = json.dumps(h.rows())
            require(FAKE_PROVIDER_KEY not in blob,
                    "auch ein Anbieterfehler traegt keinen Schluessel ins Buch")
            outcomes = {r["outcome"] for r in h.rows()}
            require("upstream_error" in outcomes, "der Fehler wird aber gebucht")
    run(go())


# ------------------------------------------------------------- Der Schluessel

def t_the_provider_key_is_never_returned_to_the_cage():
    """Auch nicht in einer Fehlermeldung."""
    async def go():
        async with Harness(status=401) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            _, payload = await h.call(token)
            require(FAKE_PROVIDER_KEY.encode() not in payload,
                    "die Antwort an den Kaefig traegt keinen Schluessel")
            for bad in (b"Bearer", b"Authorization"):
                require(bad not in payload, f"und kein {bad!r}")
    run(go())


def t_a_denial_body_is_categorical_and_says_nothing_useful():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            _, payload = await h.call(token)
            body = json.loads(payload)
            require_equal(sorted(body["error"]), ["code", "type"],
                          "die Absage hat genau zwei Felder")
            require_equal(body["error"]["type"], "solvio_broker",
                          "und sie sagt, wer abgelehnt hat")
            require(FAKE_PROVIDER_KEY not in payload.decode(), "und sonst nichts")
    run(go())


def t_the_broker_token_never_travels_to_the_provider():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token)
            call = h.upstream.calls[0]
            sent = json.dumps(call["headers"]) + call["body"].decode("utf-8", "replace")
            require(token not in sent,
                    "der Broker-Token bleibt diesseits des Brokers")
    run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
