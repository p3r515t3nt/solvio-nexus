"""Der Kaefig bekommt ein Zeitfenster, keinen Schluessel.

Diese Suite prueft die drei Verneinungen, auf denen Provider Broker V1 steht,
und sie prueft sie einzeln, weil jede fuer sich fallen kann:

1. **Kein Anbieterzugang.** Was im Kaefig liegt, ist ein Broker-Token. Der echte
   Schluessel erreicht genau ein Modul und keine Kaefig-Datei.
2. **Kein Zugang ohne laufenden Auftrag.** Ohne offenes Lease `403`.
3. **Kein Zugang ueber den Auftrag hinaus.** Beim letzten Lease-Schluss wird neu
   gepraegt; der alte Token ist `401`.

Zwei Dinge werden hier absichtlich anders geprueft, als es bequem waere:

* Die Antwort wird **byte-gleich** verglichen. Der Verbraucher im Kaefig baut
  seine Ausgabe aus den einzelnen `response.output_item.done`-Ereignissen und
  liest `response.output` aus dem Abschlussereignis nie. Ein Proxy, der den
  Strom uebersetzt, verliert Werkzeugaufrufe **still** — es gibt keine Ausnahme,
  weil ein Abschlussereignis ja ankam. Nur ein Byte-Vergleich faengt das.
* Die Tokenkappe wird an der **Vorbelastung** geprueft, nicht am gemeldeten
  Verbrauch. `usage` kommt erst im letzten Stromereignis und bei abgeklemmter
  Verbindung nie; eine Kappe, die darauf wartet, waere mit einem abgebrochenen
  Strom auf null zu setzen.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `-O`.

Direkt: python tests/test_provider_broker.py
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

# N8/C4: the Anthropic-surface cases below need a vault with a SYNTHETIC
# credential. The vault and its key store are redirected into a private
# sandbox before any vault module is imported (same isolation as the writer
# suite); a case asserts the redirection before touching the vault.
_VAULT_SANDBOX = tempfile.mkdtemp(prefix="solvio-broker-vault-")
os.environ.setdefault("SOLVIO_VAULT_DIR", os.path.join(_VAULT_SANDBOX, "vault"))
os.environ.setdefault("SOLVIO_VAULT_TEST_KEYSTORE", os.path.join(_VAULT_SANDBOX, "keys"))
import atexit  # noqa: E402
import shutil  # noqa: E402
atexit.register(shutil.rmtree, _VAULT_SANDBOX, True)

from _broker_fixtures import (  # noqa: E402
    CHAT_STREAM, FAKE_PROVIDER_KEY, TEXT_STREAM, TOOL_CALL_STREAM,
    TRUNCATED_STREAM, Harness, free_port, responses_body, run,
)

from solvio.deep import isolation  # noqa: E402
from solvio.provider_broker import proxy as px  # noqa: E402
from solvio.provider_broker import session as sess  # noqa: E402
from solvio.provider_broker import upstream as up  # noqa: E402
from solvio.provider_broker.service import BrokerService, bot_principal  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _source(*names: str) -> str:
    body = []
    for name in names:
        with open(os.path.join(SRC, "solvio", "provider_broker", name),
                  encoding="utf-8") as handle:
            body.append(handle.read())
    return "\n".join(body)


# ------------------------------------------------------------------ Tor: Token

def t_a_valid_token_names_its_principal():
    registry = sess.Registry()
    token = registry.register("deep-gateway")
    principal = registry.resolve(token)
    require(principal is not None, "ein gueltiger Token findet seinen Auftraggeber")
    require_equal(principal.name, "deep-gateway", "und zwar den richtigen")
    require(token.startswith(sess.TOKEN_PREFIX), "der Token traegt das Praefix")


def t_a_foreign_token_is_refused():
    registry = sess.Registry()
    registry.register("deep-gateway")
    require(registry.resolve("sk-solvio-broker-" + "0" * 48) is None,
            "ein erfundener Token oeffnet nichts")
    require(registry.resolve("") is None, "und ein leerer erst recht nicht")


def t_a_stale_generation_is_refused_after_the_same_name_is_reregistered():
    registry = sess.Registry()
    old = registry.register("deep-gateway")
    new = registry.register("deep-gateway")
    require(old != new, "eine neue Registrierung praegt neu")
    require(registry.resolve(old) is None, "der alte Token ist erledigt")
    require(registry.resolve(new) is not None, "der neue traegt")
    require_equal(registry.principal("deep-gateway").generation, 2,
                  "die Generation zaehlt hoch")


def t_a_sibling_principal_is_untouched_by_a_reregistration():
    """Der Doktor provisioniert im `hermes_restart` NUR Deep neu.

    Ein globaler Generationszaehler wuerde dabei die drei Bot-Token entwerten,
    die niemand nachpraegt — die Bots kassierten ab da schweigend `401`.
    """
    registry = sess.Registry()
    bot = registry.register(bot_principal("solvio-researcher"))
    registry.register("deep-gateway")
    registry.register("deep-gateway")
    require(registry.resolve(bot) is not None,
            "ein Deep-Neustart entwertet keinen Bot-Token")


def t_the_token_comparison_is_constant_time():
    """Ein Woerterbuchtreffer waere schneller — und seine Laufzeit haengt am Wert.

    Geprueft wird der AUFRUF im Syntaxbaum, nicht die Zeichenkette: der Name
    `hmac.compare_digest` steht auch im Docstring, und eine Fassung, die nur den
    Aufruf gegen `==` tauscht, kaeme an einer blossen Textsuche vorbei. Genau
    das hat eine Mutation vorgefuehrt.
    """
    code = _source("session.py")
    tree = ast.parse(code)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "resolve":
            target = node
    require(target is not None, "es gibt eine Aufloesung")

    calls = [n for n in ast.walk(target)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "compare_digest"]
    require_equal(len(calls), 1,
                  "genau EIN konstanter Vergleich traegt die Aufloesung")
    comparisons = [n for n in ast.walk(target) if isinstance(n, ast.Compare)
                   and any(isinstance(op, ast.Eq) for op in n.ops)]
    require_equal(comparisons, [],
                  "und kein einziges == auf dem Geheimnis")


# ------------------------------------------------------------------ Tor: Lease

def t_without_a_lease_nothing_is_forwarded():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            status, _ = await h.call(token)
            require_equal(status, 403, "ohne Lease wird nichts weitergeleitet")
            require_equal(len(h.upstream.calls), 0, "und niemand ruft nach draussen")
            reasons = [r["denied_reason"] for r in h.rows()]
            require("lease_absent" in reasons, "der Grund steht im Buch")
    run(go())


def t_an_expired_lease_is_no_lease():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() - 1)
            status, _ = await h.call(token)
            require_equal(status, 403, "eine abgelaufene Leihe traegt nichts")
            require_equal(len(h.upstream.calls), 0, "kein Anruf nach draussen")
    run(go())


def t_an_open_lease_forwards():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "task-1", deadline=time.time() + 60)
            status, payload = await h.call(token)
            require_equal(status, 200, "mit Lease geht es durch")
            require_equal(len(h.upstream.calls), 1, "genau ein Anruf nach draussen")
            require_equal(payload, TOOL_CALL_STREAM, "und zwar byte-gleich zurueck")
    run(go())


def t_the_last_close_rotates_the_token():
    """Schicht 3 — der Unterschied zwischen „verschoben" und „unnoetig"."""
    async def go():
        async with Harness() as h:
            old = h.broker.register_principal("deep-gateway")
            lease = h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            h.broker.close_lease(lease)
            new = h.broker.registry.principal("deep-gateway").token
            require(new != old, "der Schluss des letzten Lease praegt neu")
            status, _ = await h.call(old)
            require_equal(status, 401, "der alte Token ist erledigt")
            h.broker.open_lease("deep-gateway", "t2", deadline=time.time() + 60)
            status, _ = await h.call(new)
            require_equal(status, 200, "der naechste Auftrag gelingt ohne Neustart")
    run(go())


def t_a_sibling_lease_keeps_the_principal_alive():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            first = h.broker.open_lease("deep-gateway", "a", deadline=time.time() + 60)
            h.broker.open_lease("deep-gateway", "b", deadline=time.time() + 60)
            h.broker.close_lease(first)
            require_equal(h.broker.registry.principal("deep-gateway").token, token,
                          "solange ein Geschwister-Lease lebt, wird nicht rotiert")
            status, _ = await h.call(token)
            require_equal(status, 200, "und der Zugang traegt weiter")
    run(go())


def t_a_credential_writer_is_told_about_the_fresh_token():
    async def go():
        async with Harness() as h:
            seen = []
            h.broker.register_principal("deep-gateway")
            h.broker.set_credential_writer("deep-gateway", seen.append)
            lease = h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            h.broker.close_lease(lease)
            require_equal(len(seen), 1, "der Eigentuemer der Datei wird genau einmal gerufen")
            require_equal(seen[0], h.broker.registry.principal("deep-gateway").token,
                          "und bekommt genau den frisch gepraegten Wert")
    run(go())


def t_a_failing_credential_write_leaves_the_old_token_invalid():
    """Reihenfolge: erst praegen, dann schreiben. Ausfall vor Weiterverwendung."""
    async def go():
        async with Harness() as h:
            old = h.broker.register_principal("deep-gateway")

            def explode(_token):
                raise OSError("Platte voll")

            h.broker.set_credential_writer("deep-gateway", explode)
            lease = h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            h.broker.close_lease(lease)
            status, _ = await h.call(old)
            require_equal(status, 401,
                          "ein gescheitertes Schreiben macht den alten Token nicht wieder gut")
    run(go())


# ------------------------------------------------------------------ Tor: Modell

def t_the_allowed_model_passes_and_a_foreign_one_does_not():
    model, _ = px.parse_model(responses_body())
    require_equal(model, "gpt-5.4-mini", "das freigegebene Modell wird gelesen")
    _refused(b'{"model":"gpt-4o","input":"x"}', "ein fremdes Modell")


def t_a_duplicate_top_level_model_is_refused_in_both_orders():
    """JSON-Dekoder nehmen den letzten, ein Erstfundscanner den ersten.

    Wer den Rumpf selbst baut, spielte die beiden gegeneinander aus. Deshalb
    wird bei zwei `model` **abgelehnt** und ausdruecklich nicht das freigegebene
    ausgewaehlt.
    """
    _refused(b'{"model":"gpt-5.4-mini","model":"gpt-4o"}', "freigegeben zuerst")
    _refused(b'{"model":"gpt-4o","model":"gpt-5.4-mini"}', "freigegeben zuletzt")


def t_a_decoy_model_inside_a_string_value_is_ignored():
    _refused(b'{"input":"model: gpt-5.4-mini","model":"gpt-4o"}',
             "ein Koeder in einer Zeichenkette")
    model, _ = px.parse_model(
        b'{"input":"\\"model\\":\\"gpt-4o\\"","model":"gpt-5.4-mini"}')
    require_equal(model, "gpt-5.4-mini", "und das echte Feld greift trotzdem")


def t_a_nested_decoy_does_not_count_as_the_top_level_model():
    _refused(b'{"meta":{"model":"gpt-5.4-mini"},"model":"gpt-4o"}',
             "ein verschachtelter Koeder")
    model, _ = px.parse_model(b'{"meta":{"model":"gpt-4o"},"model":"gpt-5.4-mini"}')
    require_equal(model, "gpt-5.4-mini", "die oberste Ebene entscheidet")


def t_model_as_the_last_field_is_read_correctly():
    """Die Drahtform serialisiert das SDK, nicht SOLVIO."""
    model, _ = px.parse_model(b'{"input":"x","stream":true,"model":"gpt-5.4-mini"}')
    require_equal(model, "gpt-5.4-mini", "`model` darf hinten stehen")


def t_a_malformed_or_non_object_body_is_refused():
    _refused(b'{"model":', "ein abgeschnittener Rumpf")
    _refused(b'[{"model":"gpt-5.4-mini"}]', "eine Liste als Rumpf")
    _refused(b'"gpt-5.4-mini"', "eine blanke Zeichenkette")
    _refused(b'{"input":"x"}', "ein fehlendes Modell")
    _refused(b'{"model":123}', "ein Modell, das keine Zeichenkette ist")


def t_the_model_gate_never_uses_a_scanner():
    """Regex, Teilzeichenkette oder Erstfund waeren kein Tor, sondern eine Suche."""
    code = _source("proxy.py")
    tree = ast.parse(code)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "parse_model":
            target = node
    require(target is not None, "das Modelltor existiert")
    body = ast.dump(target)
    require("object_pairs_hook" in body, "es liest die Paare der obersten Ebene")
    for forbidden in ("re.search", "re.match", "re.findall", "startswith", "find("):
        require(forbidden not in ast.get_source_segment(code, target),
                f"das Modelltor benutzt {forbidden} nicht")


def _refused(body: bytes, what: str) -> None:
    refused = False
    try:
        px.parse_model(body)
    except px.BodyRejected as exc:
        refused = True
        require_equal(exc.reason, "model_not_allowed", f"{what}: der Grund stimmt")
    require(refused, f"{what} wird abgewiesen")


# ------------------------------------------------------------------- Tor: Pfad

def t_both_forwarded_paths_reach_upstream():
    async def go():
        for path in ("/v1/responses", "/v1/chat/completions"):
            async with Harness() as h:
                token = h.broker.register_principal("deep-gateway")
                h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
                status, _ = await h.call(token, path=path)
                require_equal(status, 200, f"{path} geht durch")
                require_equal(h.upstream.calls[0]["path"], path,
                              "geprueft und weitergeleitet ist derselbe Pfad")
    run(go())


def t_the_model_list_is_answered_locally_with_a_context_length():
    """Ohne Fensterlaenge bemisst Hermes das Fenster still falsch."""
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            # KEIN Lease: die Modellliste durchlaeuft nur Tor 1 und Tor 3.
            status, payload = await h.call(token, path="/v1/models", method="GET")
            require_equal(status, 200, "die Liste wird beantwortet")
            require_equal(len(h.upstream.calls), 0, "und zwar ohne Anruf nach draussen")
            rows = h.rows()
            require_equal(rows[0]["outcome"], "answered_locally",
                          "das Buch sagt LOKAL beantwortet, nicht weitergeleitet")
            body = json.loads(payload)
            require_equal(body["object"], "list", "die OpenAI-Huelle stimmt")
            entry = body["data"][0]
            require_equal(entry["id"], "gpt-5.4-mini", "genau ein Modell")
            require_equal(entry["context_length"],
                          px.MODEL_CONTEXT_LENGTHS["gpt-5.4-mini"],
                          "mit Fensterlaenge")
            require(1024 <= entry["context_length"] <= 10_000_000,
                    "und in dem Bereich, den der Aufloeser ueberhaupt annimmt")
            require(entry["owned_by"] != "llamacpp",
                    "kein owned_by, das einen Sonderweg ausloest")
    run(go())


def t_every_fingerprint_probe_stays_local():
    """Ein `200` auf `/api/v1/models` liesse Hermes uns fuer LM Studio halten.

    Danach laese er eine ganz andere Rumpfform (`models[].loaded_instances`),
    und die `context_length` der OpenAI-Huelle waere unsichtbar.
    """
    probes = ("/api/v1/models", "/api/tags", "/v1/props", "/props", "/version",
              "/models", "/api/show", "/v1/models/gpt-5.4-mini")

    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            for path in probes:
                status, _ = await h.call(token, path=path, method="GET")
                require_equal(status, 404, f"{path} wird oertlich abgewiesen")
            require_equal(len(h.upstream.calls), 0,
                          "keine einzige Sonde erreicht den Anbieter")
    run(go())


def t_other_inference_endpoints_are_refused():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            for path in ("/v1/embeddings", "/v1/audio/speech",
                         "/v1/audio/transcriptions", "/v1/files",
                         "/v1/images/generations", "/v1/fine_tuning/jobs"):
                status, _ = await h.call(token, path=path)
                require_equal(status, 404, f"{path} ist kein Weg nach draussen")
            require_equal(len(h.upstream.calls), 0, "und keiner davon ruft an")
    run(go())


def t_encoded_traversal_and_query_strings_are_refused():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            for path in ("/v1/%2e%2e/admin", "/v1/responses/../embeddings",
                         "/v1/responses%00", "//v1/responses",
                         "/v1/responses?api-version=evil"):
                status, _ = await h.call(token, path=path)
                require(status in (404, 400), f"{path} kommt nicht durch")
            require_equal(len(h.upstream.calls), 0, "nichts davon erreicht den Anbieter")
    run(go())


# --------------------------------------------------------------------- Upstream

def t_the_upstream_origin_is_pinned_in_code():
    require_equal(up.UPSTREAM_ORIGIN, "https://api.openai.com",
                  "das Ziel steht im Code")
    require_equal(up.target_url("/v1/responses"),
                  "https://api.openai.com/v1/responses",
                  "kein /v1/v1")
    code = _source("upstream.py")
    require("os.environ" not in code, "das Ziel kommt aus keiner Umgebung")


def t_a_redirect_is_never_followed():
    async def go():
        for location in ("https://api.openai.com/v2/responses",
                         "https://evil.example/v1/responses"):
            async with Harness(status=302, location=location) as h:
                token = h.broker.register_principal("deep-gateway")
                h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
                status, _ = await h.call(token)
                require_equal(status, 502, f"{location} wird kategorisch abgelehnt")
                require_equal(len(h.upstream.calls), 1,
                              "der Umzug wird nicht verfolgt")
    run(go())


def t_the_inbound_authorization_never_travels_and_routing_headers_are_stripped():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token, headers={
                "Cookie": "session=abc",
                "X-Forwarded-For": "9.9.9.9",
                "X-Real-IP": "9.9.9.9",
                "Proxy-Authorization": "Basic ZXZpbA==",
                "Forwarded": "for=1.2.3.4",
                "X-Api-Key": "smuggled",
            })
            sent = h.upstream.calls[0]["headers"]
            require_equal(sent["authorization"], f"Bearer {FAKE_PROVIDER_KEY}",
                          "nach draussen geht der ECHTE Schluessel")
            require(token not in json.dumps(sent),
                    "und niemals der Broker-Token des Kaefigs")
            for name in ("cookie", "x-forwarded-for", "x-real-ip",
                         "proxy-authorization", "forwarded", "x-api-key"):
                require(name not in sent, f"{name} faellt weg")
            require_equal(sent.get("host", ""), "api.openai.com",
                          "der Wirt wird gesetzt, nicht geerbt")
    run(go())


def t_the_client_ignores_the_proxy_environment_and_keeps_tls():
    code = _source("upstream.py")
    require("trust_env=False" in code,
            "der Klient uebernimmt keinen Proxy aus der Umgebung")
    require("ssl=False" not in code, "die TLS-Pruefung wird nie abgeschaltet")
    require("verify=False" not in code, "und auch nicht so")
    service = _source("service.py")
    require("allow_redirects=False" in service, "ein Umzug wird nie verfolgt")


def t_a_set_cookie_from_upstream_never_reaches_the_jail():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                        h.base + "/v1/responses", data=responses_body(),
                        headers={"Authorization": f"Bearer {token}"}) as response:
                    await response.read()
                    require("Set-Cookie" not in response.headers,
                            "der Keks des Anbieters wird abgeschnitten")
    run(go())


# ------------------------------------------------------------------------ Kappen

def t_the_request_cap_refuses_once_the_day_is_full():
    registry = sess.Registry()
    registry.register("deep-gateway", caps=sess.Caps(requests_per_day=2))
    principal = registry.principal("deep-gateway")
    now = time.time()
    registry.precharge(principal, estimate=1, now=now)
    registry.precharge(principal, estimate=1, now=now)
    refused = False
    try:
        registry.admit(principal, now=now)
    except sess.CapExceeded as exc:
        refused = True
        require_equal(exc.reason, "rate_capped", "der Grund stimmt")
    require(refused, "die Tageszahl begrenzt")


def t_the_token_estimate_is_charged_before_the_call():
    """Reisst die Schaetzung die Kappe, geht NICHTS nach draussen."""
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.registry.principal("deep-gateway").caps = sess.Caps(
                tokens_per_day=10)
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            status, _ = await h.call(token)
            require_equal(status, 429, "die Tokenkappe greift")
            require_equal(len(h.upstream.calls), 0,
                          "und zwar OHNE Anruf nach draussen")
            reasons = [r["denied_reason"] for r in h.rows()]
            require("token_capped" in reasons, "der Grund steht im Buch")
    run(go())


def t_reported_usage_replaces_the_estimate_in_both_directions():
    registry = sess.Registry()
    registry.register("deep-gateway")
    principal = registry.principal("deep-gateway")
    now = time.time()
    registry.precharge(principal, estimate=1000, now=now)
    registry.replace_estimate(principal, estimate=1000, reported=1540, now=now)
    require_equal(principal.tokens_today, 1540, "nach oben ersetzt, nicht addiert")
    registry.precharge(principal, estimate=1000, now=now)
    registry.replace_estimate(principal, estimate=1000, reported=200, now=now)
    require_equal(principal.tokens_today, 1740, "und nach unten ebenso")


def t_a_stream_without_usage_keeps_the_estimate_charged():
    """Zurueckgebucht wird nie — sonst waere die Kappe mit einem Abbruch auf null."""
    async def go():
        async with Harness(body=TRUNCATED_STREAM) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token)
            principal = h.broker.registry.principal("deep-gateway")
            require(principal.tokens_today > 0,
                    "die Vorbelastung bleibt stehen, wenn nie ein usage kommt")
            row = [r for r in h.rows() if r["outcome"] == "forwarded"][0]
            require_equal(row["tokens_source"], "estimated",
                          "und das Buch sagt ehrlich, dass es eine Schaetzung ist")
            require(row["input_tokens"] > 0, "sie wird nicht stillschweigend null")
    run(go())


def t_a_reported_usage_lands_in_the_ledger_as_reported():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token)
            row = [r for r in h.rows() if r["outcome"] == "forwarded"][0]
            require_equal(row["tokens_source"], "reported", "gemeldet schlaegt geschaetzt")
            require_equal(row["input_tokens"], 1200, "und zwar mit dem echten Wert")
            require_equal(row["output_tokens"], 340, "in beiden Richtungen")
    run(go())


def t_each_physical_retry_counts_on_its_own():
    """Bis zu 24 physische POSTs koennen auf EINEN logischen Aufruf entfallen.

    Sie sind echte Anbieteranfragen und kosten echtes Geld. Jede zaehlt
    einzeln — es gibt auf dem Draht keine Idempotenzkennung, an der sich
    zusammengehoerige Anfragen erkennen liessen.

    **Was der Broker seit Deep Research Reliability V1 unterscheidet:** ein
    WACHSENDER Rumpf ist der naechste Schritt derselben Arbeit und laeuft
    weiter — genau der Fall, der hier geprueft wird. Ein byte-IDENTISCHER
    Rumpf nach einer bereits gelieferten Antwort ist etwas anderes; er hat
    seine eigene Zusicherung (`t_an_identical_resend_after_a_delivered_answer_is_refused`
    in `test_deep_research_reliability.py`).
    """
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            for step in range(3):
                await h.call(token, body=responses_body(input=f"Schritt {step}"))
            forwarded = [r for r in h.rows() if r["outcome"] == "forwarded"]
            require_equal(len(forwarded), 3, "drei Anfragen sind drei Zeilen")
            principal = h.broker.registry.principal("deep-gateway")
            require_equal(principal.requests_today, 3, "und drei Buchungen")
            require_equal(principal.tokens_today, 3 * (1200 + 340),
                          "jede zaehlt einzeln auf die Tokenkappe")
    run(go())


def t_a_body_over_the_ceiling_is_refused():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            previous = px.MAX_BODY_BYTES
            px.MAX_BODY_BYTES = 512
            try:
                fat = b'{"model":"gpt-5.4-mini","input":"' + b"x" * 4096 + b'"}'
                status, _ = await h.call(token, body=fat)
                require_equal(status, 413, "ueber der Obergrenze wird abgelehnt")
                require_equal(len(h.upstream.calls), 0, "und nichts geht raus")
            finally:
                px.MAX_BODY_BYTES = previous
            reasons = [r["denied_reason"] for r in h.rows()]
            require("body_too_large" in reasons, "der Grund steht im Buch")
    run(go())


def t_the_aggregate_body_ceiling_is_separate_from_the_single_one():
    code = _source("service.py")
    require("MAX_TOTAL_BUFFERED_BYTES" in code,
            "es gibt eine Summe ueber alle Auftraggeber")
    require(px.MAX_TOTAL_BUFFERED_BYTES >= px.MAX_BODY_BYTES,
            "und sie ist nicht kleiner als die Einzelgrenze")


def t_the_concurrent_request_cap_refuses():
    registry = sess.Registry()
    registry.register("deep-gateway", caps=sess.Caps(max_inflight=1))
    principal = registry.principal("deep-gateway")
    principal.inflight = 1
    refused = False
    try:
        registry.admit(principal, now=time.time())
    except sess.CapExceeded as exc:
        refused = True
        require_equal(exc.reason, "rate_capped", "der Grund stimmt")
    require(refused, "gleichzeitige Anfragen sind begrenzt")


def t_the_lease_ceiling_refuses():
    registry = sess.Registry()
    registry.register("deep-gateway", caps=sess.Caps(max_leases=1))
    now = time.time()
    registry.open_lease("deep-gateway", "a", deadline=now + 60, now=now)
    refused = False
    try:
        registry.open_lease("deep-gateway", "b", deadline=now + 60, now=now)
    except sess.CapExceeded:
        refused = True
    require(refused, "gleichzeitige Leases sind begrenzt")


def t_the_day_boundary_resets_the_counters():
    registry = sess.Registry()
    registry.register("deep-gateway")
    principal = registry.principal("deep-gateway")
    monday = time.mktime(time.strptime("2026-08-27 12:00:00", "%Y-%m-%d %H:%M:%S"))
    registry.precharge(principal, estimate=5000, now=monday)
    require(principal.tokens_today >= 5000, "der Tag ist gebucht")
    registry.admit(principal, now=monday + 86400 * 2)
    require_equal(principal.tokens_today, 0, "ein neuer UTC-Tag faengt bei null an")


# ---------------------------------------------------------------------- Stroeme

def t_a_tool_call_survives_the_stream_byte_for_byte():
    """Der einzige Test, der einen naiven Uebersetzer wirklich faengt."""
    async def go():
        async with Harness(body=TOOL_CALL_STREAM) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            _, payload = await h.call(token)
            require_equal(payload, TOOL_CALL_STREAM, "kein Byte veraendert")
            require(b"response.output_item.done" in payload,
                    "das tragende Ereignis kommt an")
            require(b'"name":"web_search"' in payload, "und der Werkzeugaufruf darin")
    run(go())


def t_a_text_and_reasoning_stream_survives():
    async def go():
        async with Harness(body=TEXT_STREAM) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            _, payload = await h.call(token)
            require_equal(payload, TEXT_STREAM, "Text und Begruendung unveraendert")
    run(go())


def t_a_chat_completions_stream_survives_in_its_native_form():
    async def go():
        async with Harness(body=CHAT_STREAM) as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            _, payload = await h.call(token, path="/v1/chat/completions")
            require_equal(payload, CHAT_STREAM, "die eigene Drahtform bleibt")
            row = [r for r in h.rows() if r["outcome"] == "forwarded"][0]
            require_equal(row["input_tokens"], 7, "prompt_tokens werden gelesen")
            require_equal(row["output_tokens"], 3, "completion_tokens ebenso")
    run(go())


# ------------------------------------------------------------------------- Buch

def t_the_ledger_records_all_three_outcomes():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            await h.call(token)                                    # denied
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            await h.call(token)                                    # forwarded
            outcomes = {r["outcome"] for r in h.rows()}
            require("denied" in outcomes, "Ablehnungen sind Zeilen")
            require("forwarded" in outcomes, "Weiterleitungen auch")
    run(go())


def t_the_ledger_never_holds_a_secret_or_a_prompt():
    async def go():
        async with Harness() as h:
            token = h.broker.register_principal("deep-gateway")
            h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)
            secret_prompt = "MEIN-GEHEIMER-PROMPT-TEXT"
            await h.call(token, body=json.dumps(
                {"model": "gpt-5.4-mini", "input": secret_prompt}).encode())
            blob = json.dumps(h.rows())
            require(FAKE_PROVIDER_KEY not in blob, "kein Anbieterschluessel im Buch")
            require(token not in blob, "kein Broker-Token im Buch")
            require(secret_prompt not in blob, "kein Aufforderungsinhalt im Buch")
            require("Bearer" not in blob, "kein Kopfsatz im Buch")
    run(go())


def t_a_denied_reason_outside_the_closed_set_is_refused():
    from solvio.provider_broker.ledger import DENIED_REASONS, BrokerLedger, Entry

    path = os.path.join(tempfile.mkdtemp(), "b.sqlite3")
    ledger = BrokerLedger(path)
    ledger.open()
    try:
        refused = False
        try:
            ledger.record(Entry(principal="p", generation=1, method="POST",
                                path="/v1/responses", outcome="denied",
                                denied_reason="der Nutzer hat es erlaubt"),
                          at=time.time())
        except ValueError:
            refused = True
        require(refused, "ein Freitext kommt nicht in die Spalte")
        # Eine AUFZAEHLUNG mit Grund je Zeile, keine Obergrenze: ein neuer
        # Grund soll nicht unmoeglich sein, aber er soll HIER auffallen und
        # eine Begruendung bekommen.
        require_equal(sorted(DENIED_REASONS), sorted([
            # die acht der OpenAI-Flaeche, unveraendert
            "bad_token", "body_too_large", "lease_absent", "model_not_allowed",
            "path_not_allowed", "provider_tool_not_allowed", "rate_capped", "stale_generation",
            "token_capped",
            # Deep Research Reliability V1: die Aufgabengrenze und ihr
            # Rueckfallnetz. Sie sind KEINE Tageskappe — `token_capped`
            # erholt sich um UTC-Mitternacht, diese drei nie.
            "duplicate_request", "lease_budget_exhausted",
            "lease_request_capped",
            # die fuenf der Anthropic-Flaeche (V0.6):
            # gueltiges Token, aber nicht fuer DIESE Flaeche
            "principal_not_allowed",
            # es liegt keine Anthropic-Anmeldung im Tresor
            "no_credential",
            # der Tresor sagte nein / der Wert war unlesbar / nicht zu haben
            "credential_denied", "credential_malformed", "vault_unavailable",
        ]), "die Menge ist geschlossen und vollstaendig")
    finally:
        ledger.close()


def t_the_ledger_file_is_private():
    from solvio.provider_broker.ledger import BrokerLedger, Entry

    root = tempfile.mkdtemp()
    ledger = BrokerLedger(os.path.join(root, "nested", "b.sqlite3"))
    ledger.open()
    try:
        mode = os.stat(ledger.path).st_mode & 0o777
        require_equal(mode, 0o600, "die Datei gehoert nur dem Eigentuemer")
        directory = os.stat(os.path.dirname(ledger.path)).st_mode & 0o777
        require_equal(directory, 0o700, "und das Verzeichnis ebenso")
        # Der WAL-Modus legt zwei Beidateien an, und SQLite nimmt dafuer die
        # umask des Prozesses statt der Rechte der Datenbank. Im `-wal` stehen
        # die zuletzt geschriebenen Buchzeilen; sie waren hier einmal `0644`.
        ledger.record(Entry(principal="p", generation=1, method="POST",
                            path="/v1/responses", outcome="forwarded"),
                      at=time.time())
        for suffix in ("-wal", "-shm"):
            side = ledger.path + suffix
            if os.path.exists(side):
                require_equal(os.stat(side).st_mode & 0o777, 0o600,
                              f"auch {suffix} gehoert nur dem Eigentuemer")
    finally:
        ledger.close()


# -------------------------------------------------------------------- Isolation

def t_the_profile_allows_exactly_the_broker_port():
    profile = isolation.render_profile(jail="/tmp/j", python_root="/tmp/p")
    require('(remote tcp "localhost:8792")' in profile,
            "der Broker-Port ist der eine erlaubte Rueckschleifen-Ausgang")


def t_the_profile_has_no_loopback_wildcard_anywhere():
    """Die Ausgangszeile ALLEIN haette eine begehbare Luecke geschaffen.

    Ein Kaefigprozess bindet 8792, ein zweiter verbindet sich — und fuer den
    Kaefig ist der Broker an nichts als der Portnummer erkennbar.
    """
    profile = isolation.render_profile(jail="/tmp/j", python_root="/tmp/p")
    require("localhost:*" not in profile,
            "nirgends ein Platzhalter-Port, auch nicht beim Binden")
    require('(allow network-bind (local ip "localhost:8791"))' in profile,
            "gebunden werden darf nur der eigene Gateway-Port")
    require('(allow network-inbound (local ip "localhost:8791"))' in profile,
            "und angenommen ebenso")
    require("localhost:8766" not in profile, "der Core bleibt unerreichbar")
    require("localhost:8770" not in profile, "der Freigabeweg ebenso")
    require("localhost:8123" not in profile, "und Home Assistant auch")


def t_the_broker_port_is_a_forbidden_environment_name():
    require("SOLVIO_BROKER_PORT" in isolation.FORBIDDEN_ENV,
            "der Kaefig lernt den Broker nicht aus seiner Umgebung")
    env = isolation.child_environment(jail="/tmp/j", hermes_home="/tmp/j/home")
    require_equal(isolation.leaking_names(env), [], "die Kindumgebung ist sauber")


# ----------------------------------------------------------------------- Start

def t_the_broker_has_no_restart_playbook():
    from solvio.doctor import playbooks as P

    require("broker" in P.FORBIDDEN_RESTARTS,
            "ein Alleinneustart verwaiste alle vier Kaefig-Token")
    require_equal(P.for_component("broker"), [], "und es gibt kein Playbook dafuer")


def t_the_control_plane_has_no_http_route():
    """Der Kaefig kann sich selbst kein Lease oeffnen."""
    code = _source("service.py")
    tree = ast.parse(code)
    routes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = getattr(node.func, "attr", "")
            if func in ("add_route", "add_get", "add_post"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        routes.append(arg.value)
    for forbidden in ("register", "lease", "mint", "rotate", "admin"):
        for route in routes:
            require(forbidden not in route.lower(),
                    f"es gibt keine Route, die {forbidden} anbietet")


def t_a_broker_that_cannot_bind_refuses_instead_of_half_starting():
    """Ein LEBENDER Besetzer laesst den Start scheitern — nach der Frist.

    Die Frist selbst ist eine Korrektur aus der Live-Abnahme: ohne sie
    scheiterte ein `launchd`-Neustart am eigenen Vorgaenger, der den Socket noch
    hielt, und Deep und die Bots blieben eine ganze Core-Lebenszeit unten.
    """
    async def go():
        from solvio.provider_broker import service as svc

        port = free_port()
        previous_grace = svc.BIND_GRACE
        # Wiederhergestellt, nicht geloescht (DEBT-0223): ohne Variable faellt
        # jedes spaetere Buch dieser Suite auf `~/.solvio/broker.sqlite3`.
        previous_db = os.environ.get("SOLVIO_BROKER_DB")
        svc.BIND_GRACE = 0.5
        # ZUERST der Schalter, DANN der Dienst. `BrokerLedger.__init__` loest
        # seinen Pfad SOFORT auf (`resolve_path` im Konstruktor) — wer den
        # Dienst vorher baut, hat das produktive Buch `~/.solvio/broker.sqlite3`
        # in der Hand, und `start()` oeffnet es dann wirklich. Genau so stand es
        # hier, und genau so ist beim Bau von Deep Research Reliability V1 die
        # neue Spalte in das produktive Buch gewandert.
        os.environ["SOLVIO_BROKER_DB"] = os.path.join(tempfile.mkdtemp(), "a.sqlite3")
        first = BrokerService(provider_key=FAKE_PROVIDER_KEY, port=port)
        await first.start()
        try:
            os.environ["SOLVIO_BROKER_DB"] = os.path.join(
                tempfile.mkdtemp(), "b.sqlite3")
            second = BrokerService(provider_key=FAKE_PROVIDER_KEY, port=port)
            failed = False
            try:
                await second.start()
            except OSError:
                failed = True
            require(failed, "ein belegter Port ist ein Fehler, kein Halbzustand")
            require(not second.listening(), "und listening() bleibt falsch")
        finally:
            svc.BIND_GRACE = previous_grace
            await first.stop()
            if previous_db is None:
                os.environ.pop("SOLVIO_BROKER_DB", None)
            else:
                os.environ["SOLVIO_BROKER_DB"] = previous_db
    run(go())


def t_a_broker_without_a_provider_key_refuses_to_start():
    async def go():
        service = BrokerService(provider_key="", port=free_port())
        refused = False
        try:
            await service.start()
        except RuntimeError:
            refused = True
        require(refused, "ohne echten Schluessel gibt es keinen Broker")
    run(go())


def t_deep_and_bots_refuse_without_a_listening_broker():
    from solvio.bots import team as team_mod
    from solvio.deep import service as deep_service

    class _Down:
        def listening(self):
            return False

    class _Settings:
        openai_api_key = "egal"

    require(team_mod.from_environment(_Settings(), _Down()) is None,
            "ohne Broker kein Botteam")

    config = deep_service.ex.ExecutorConfig(jail="/tmp/j", venv_bin="/tmp/j/venv/bin",
                                            python_root="/tmp/p")
    service = deep_service.DeepRuntimeService(config=config, state_dir="/tmp/s",
                                              broker=_Down())
    refused = False
    try:
        asyncio.run(service.start())
    except deep_service.SandboxUnavailable:
        refused = True
    require(refused, "ohne Broker kein tiefer Executor")


def t_the_scrub_is_unconditional_and_removes_every_jail_env():
    """Ohne dieses Ausraeumen ist der Satz nur wahrscheinlich, nicht wahr."""
    from solvio.realtime.core_server import _scrub_jail_credentials

    jail = tempfile.mkdtemp()
    home = os.path.join(jail, "home")
    profiles = os.path.join(home, "profiles")
    os.makedirs(profiles, exist_ok=True)
    targets = [os.path.join(home, ".env")]
    for name in ("solvio-researcher", "solvio-project-keeper", "solvio-diagnostician"):
        os.makedirs(os.path.join(profiles, name), exist_ok=True)
        targets.append(os.path.join(profiles, name, ".env"))
    for path in targets:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"OPENAI_API_KEY={FAKE_PROVIDER_KEY}\n")

    previous = os.environ.get("SOLVIO_DEEP_JAIL")
    os.environ["SOLVIO_DEEP_JAIL"] = jail
    try:
        removed = _scrub_jail_credentials()
    finally:
        if previous is None:
            os.environ.pop("SOLVIO_DEEP_JAIL", None)
        else:
            os.environ["SOLVIO_DEEP_JAIL"] = previous
    require_equal(removed, 4, "alle vier Dateien verschwinden")
    for path in targets:
        require(not os.path.exists(path), f"{path} ist weg")


# ------------------------------------------------------------- Zugangsfluss

def t_the_deep_env_carries_a_broker_token_and_never_the_provider_key():
    from solvio.deep import executor as ex

    jail = tempfile.mkdtemp()
    config = ex.ExecutorConfig(jail=jail, venv_bin=jail + "/venv/bin",
                               python_root="/tmp/p")
    token = "sk-solvio-broker-" + "a" * 48
    ex.provision(config, api_key="local-gateway-key-0123456789abcdef",
                 broker_token=token)
    with open(os.path.join(config.home, ".env"), encoding="utf-8") as handle:
        body = handle.read()
    require(f"OPENAI_API_KEY={token}" in body, "in der Zeile steht der Broker-Token")
    require(f"OPENAI_BASE_URL=http://127.0.0.1:{config.broker_port}/v1" in body,
            "und daneben die Rueckschleifen-Basis")
    require(FAKE_PROVIDER_KEY not in body, "kein Anbieterschluessel")
    require("API_SERVER_KEY=" in body, "die Nicht-Schluesselzeilen bleiben erhalten")
    require_equal(os.stat(os.path.join(config.home, ".env")).st_mode & 0o777, 0o600,
                  "und die Datei bleibt privat")

    with open(os.path.join(config.home, "config.yaml"), encoding="utf-8") as handle:
        cfg = handle.read()
    require("title_generation:" in cfg and "enabled: false" in cfg,
            "die Titelerzeugung ist stillgelegt")
    require("memory_enabled: false" in cfg, "und das Gedaechtnis ebenso")


def t_every_bot_env_carries_a_broker_token_and_never_the_provider_key():
    from solvio.bots import posture

    directory = tempfile.mkdtemp()
    token = "sk-solvio-broker-" + "b" * 48
    posture.write_credential(directory, token, broker_port=8792)
    with open(os.path.join(directory, ".env"), encoding="utf-8") as handle:
        body = handle.read()
    require(f"OPENAI_API_KEY={token}" in body, "der Broker-Token steht drin")
    require("OPENAI_BASE_URL=http://127.0.0.1:8792/v1" in body, "die Basis daneben")
    require(FAKE_PROVIDER_KEY not in body, "kein Anbieterschluessel")


def t_the_provider_name_stays_openai_api():
    """Die Aufloeser fuer auto/custom/openrouter sehen OPENAI_BASE_URL nicht an."""
    from solvio.deep import service as deep_service

    require_equal(deep_service.DEFAULT_PROVIDER, "openai-api",
                  "ein Wechsel schaltete die Umleitung still ab")


# ------------------------------------------------------------------- Inventar

def t_the_local_gateway_key_is_not_called_a_provider_credential():
    from solvio.storage.inventory import secret_inventory

    entries = {e["ort"]: e for e in secret_inventory()}
    api_key = entries.get("~/.solvio-hermes/api_key")
    require(api_key is not None, "der Ort steht im Inventar")
    require("Anbieterschluessel" not in api_key["inhalt"],
            "er ist KEIN Anbieterzugang, sondern der lokale Gateway-Zugang")
    require("API_SERVER_KEY" in api_key["inhalt"], "und heisst beim Namen")


def t_the_ephemeral_broker_files_are_never_durable_authority():
    from solvio.storage.inventory import CLASS_NEVER, secret_inventory

    hit = [e for e in secret_inventory() if "home/.env" in e["ort"]]
    require(len(hit) == 1, "die Kaefig-.env stehen jetzt ueberhaupt im Inventar")
    require_equal(hit[0]["klasse"], CLASS_NEVER,
                  "ein kurzlebiger Token ist keine sicherbare Autoritaet")


# ------------------------------------------------------------- AST-Zusicherung

#: Je ANBIETERFLAECHE genau ein Modul, das die Anmeldung beruehrt — und die
#: Liste ist eine AUFZAEHLUNG mit Grund, keine Obergrenze. Ein drittes Modul
#: soll nicht unmoeglich sein, aber es soll HIER auffallen.
#:
#: * `upstream.py`  — OpenAI. Haelt den Schluessel im Prozess (der Core
#:   braucht ihn beim Start), baut den ausgehenden `Authorization`-Kopf.
#: * `anthropic.py` — Anthropic (V0.6). Haelt NICHTS: leiht je Anfrage aus dem
#:   Tresor, gebunden an `ExecutorId.ANTHROPIC_BROKER`, und setzt den Wert
#:   erst am Ausgang ein.
CREDENTIAL_MODULES = ("upstream.py", "anthropic.py")


def _builds_header(quelle: str, name: str) -> list[int]:
    """Zeilen, in denen `name` als Kopf GESETZT statt gelesen wird.

    Gelesen heisst: `…headers.get("x-api-key", …)` — so holt der Broker das
    Token, das der Kaefig mitschickt. Gesetzt heisst: der Name steht als
    Schluessel in einem Wortbuch oder links einer Zuweisung — so entsteht ein
    ausgehender Kopf, und das darf nur ein Credential-Modul.
    """
    try:
        baum = ast.parse(quelle)
    except SyntaxError:
        return []
    gelesen: set[int] = set()
    for knoten in ast.walk(baum):
        if (isinstance(knoten, ast.Call)
                and isinstance(knoten.func, ast.Attribute)
                and knoten.func.attr == "get"):
            for arg in knoten.args:
                if isinstance(arg, ast.Constant) and arg.value == name:
                    gelesen.add(id(arg))
    treffer: list[int] = []
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Constant) and knoten.value == name:
            if id(knoten) not in gelesen:
                treffer.append(knoten.lineno)
    return sorted(treffer)


def _code_only(quelle: str) -> str:
    """Der Quelltext ohne Kommentare und ohne Docstrings.

    Ein Waechter, der Prosa mitliest, bestraft das Erklaeren. Diese Zeile
    trennt beides sauber: Kommentare fallen ueber `tokenize`, Docstrings ueber
    den Syntaxbaum. Was uebrig bleibt, ist das, was der Rechner tut.
    """
    import io
    import tokenize

    docstrings = set()
    try:
        baum = ast.parse(quelle)
    except SyntaxError:
        return quelle
    for knoten in ast.walk(baum):
        if not isinstance(knoten, (ast.Module, ast.FunctionDef,
                                   ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        koerper = getattr(knoten, "body", [])
        if (koerper and isinstance(koerper[0], ast.Expr)
                and isinstance(koerper[0].value, ast.Constant)
                and isinstance(koerper[0].value.value, str)):
            docstrings.add((koerper[0].lineno, koerper[0].col_offset))

    stuecke = []
    for tok in tokenize.generate_tokens(io.StringIO(quelle).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start in docstrings:
            continue
        stuecke.append(tok.string)
    return "\n".join(stuecke)


def t_only_the_named_modules_touch_a_provider_credential():
    """Die Bauform der Zahlung: ein Wert erreicht genau ein benanntes Modul.

    Seit V0.6 gibt es zwei Anbieterflaechen und deshalb zwei solche Module —
    aber **nicht** zwei Stellen je Flaeche. Was diese Zusicherung haelt, ist
    die Aufzaehlung: wer eine dritte Stelle baut, faellt hier auf.
    """
    package = os.path.join(SRC, "solvio", "provider_broker")
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py") or name in CREDENTIAL_MODULES:
            continue
        with open(os.path.join(package, name), encoding="utf-8") as handle:
            roh = handle.read()
        # Nur der CODE, nicht die Erklaerungen. Dieselbe Lehre wie bei
        # `isolation.sealed_violations`: eine Textsuche ueber die Rohfassung
        # schlaegt ausgerechnet an dem Kommentar an, der die Regel erklaert —
        # und erzieht dazu, weniger zu erklaeren. Live passiert, als
        # `_authenticate` einen Docstring bekam, der beide Kopfnamen nennt.
        code = _code_only(roh)
        require("Bearer " not in code,
                f"{name} baut keinen Authorization-Kopf")
        # `x-api-key` darf hier nur GELESEN werden, nie gebaut. Der
        # Unterschied ist der ganze Punkt: den eingehenden Kopf zu lesen holt
        # das Broker-Token des Kaefigs; ihn zu SETZEN hiesse, eine Anmeldung
        # nach draussen zu schreiben — und das darf nur ein Credential-Modul.
        gebaut = _builds_header(roh, "x-api-key")
        require(not gebaut,
                f"{name} baut einen x-api-key-Kopf (Zeilen {gebaut})")
        require("_provider_key" not in code or name == "service.py",
                f"{name} haelt den echten Schluessel nicht")

    with open(os.path.join(package, "upstream.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    builders = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "authorization"]
    require_equal(len(builders), 1,
                  "genau EINE Stelle baut den echten OpenAI-Authorization-Kopf")


def t_the_anthropic_credential_is_borrowed_in_exactly_one_module():
    """Der Tresor nimmt den Modulnamen per Stack-Inspektion.

    Deshalb muss `broker.use(...)` **woertlich** in `anthropic.py` stehen und
    in keinem Helfer: in einem Helfer stuende dort der Helfer, und die
    Bindung, die diesen Zugang schuetzt, waere die auf ein anderes Modul.
    """
    from solvio.secret_vault import policy as VP

    require_equal(VP.EXECUTOR_MODULES[VP.ExecutorId.ANTHROPIC_BROKER],
                  ("solvio.provider_broker.anthropic",),
                  "die Executor-Bindung nennt nicht genau ein Modul")

    package = os.path.join(SRC, "solvio", "provider_broker")
    with open(os.path.join(package, "anthropic.py"), encoding="utf-8") as handle:
        code = handle.read()
    require(".use(SECRET_REF," in code,
            "der Leihvorgang steht nicht woertlich in anthropic.py")
    require("ExecutorId.ANTHROPIC_BROKER" in code,
            "der Leihvorgang nennt nicht seinen eigenen Executor")

    # Und kein anderes Modul im Paket leiht ihn.
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py") or name == "anthropic.py":
            continue
        with open(os.path.join(package, name), encoding="utf-8") as handle:
            other = handle.read()
        require("ANTHROPIC_BROKER" not in other,
                f"{name} beruehrt den Anthropic-Executor")
        require("SECRET_REF" not in other,
                f"{name} kennt den Tresorplatz der Anmeldung")


def t_the_service_hands_the_key_to_nobody_but_upstream():
    with open(os.path.join(SRC, "solvio", "provider_broker", "service.py"),
              encoding="utf-8") as handle:
        code = handle.read()
    require("provider_key" in code, "der Wert kommt herein")
    require("Upstream(provider_key)" in code,
            "und wird unmittelbar an das eine Modul gereicht")
    require(code.count("provider_key") <= 3,
            "er wird nicht herumgereicht, nur durchgereicht")




# ------------------------------------------- Was die Mutationen aufgedeckt haben

def t_the_last_close_aborts_a_running_forward():
    """Ein Lease-Schluss beendet eine bereits ZUGELASSENE Weiterleitung nicht
    von selbst — das ist der Teil, den das Bedrohungsmodell offen benennt.

    Der Abbruch bei Lease-Null ist die Handhabe, die ihn schliesst. Ohne ihn
    stroeme eine Antwort beliebig lange ueber den Auftrag hinaus.
    """
    async def go():
        async with Harness(body=TEXT_STREAM, chunk_pause=0.4) as h:
            token = h.broker.register_principal("deep-gateway")
            lease = h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)

            call = asyncio.create_task(h.call(token))
            await asyncio.sleep(0.5)          # der Strom laeuft
            require_equal(len(h.broker._inflight.get("deep-gateway", ())), 1,
                          "eine Weiterleitung ist unterwegs")
            h.broker.close_lease(lease)       # letztes Lease faellt
            status, payload = await call
            require(len(payload) < len(TEXT_STREAM),
                    "der Strom wird abgeschnitten, nicht zu Ende bedient")
            require_equal(len(h.broker._inflight.get("deep-gateway", ())), 0,
                          "und die Handhabe ist abgeraeumt")

            fresh = h.broker.registry.principal("deep-gateway").token
            status, _ = await h.call(fresh)
            require_equal(status, 403, "die naechste Anfrage findet kein Lease")
    run(go())


def t_0185_the_accounting_decision_covers_every_case_by_name():
    """DEBT-0185: was gebucht wird, und woher die Zahl kommt.

    Die Entscheidung steht an EINER Stelle, und diese Zusicherung geht sie
    Fall fuer Fall durch. Der mittlere ist der Fund: bricht der Klient ab,
    kommt `usage` nie — und die Vorbelastung blieb stehen. 65 024 Token fuer
    eine Anfrage, die vielleicht 200 verbraucht hat.
    """
    from solvio.provider_broker import proxy as px

    # 1) Normaler Abschluss mit gemeldetem usage: die Wahrheit, unveraendert.
    require_equal(px.settled_charge(seen=True, input_tokens=10, output_tokens=5,
                                    estimate=65024, request_bytes=4000,
                                    sent_bytes=100, aborted=False, status=200),
                  (10, 5, "reported"),
                  "ein gemeldetes usage wird nicht mehr uebernommen")

    # 2) Spaeter Abbruch: der Rumpf ging ganz raus, 800 Bytes kamen zurueck.
    ein, aus, quelle = px.settled_charge(
        seen=False, input_tokens=0, output_tokens=0, estimate=65024,
        request_bytes=4000, sent_bytes=800, aborted=True, status=200)
    require_equal(quelle, "measured_bytes", f"falsche Quelle: {quelle}")
    require(ein + aus < 65024 // 10,
            f"die Schaetzung steht immer noch: {ein}+{aus}")
    require(aus > 0, "die zurueckgeflossenen Bytes wurden nicht gebucht")

    # 3) Frueher Abbruch: nichts kam zurueck — der Eingang bleibt trotzdem.
    ein2, aus2, quelle2 = px.settled_charge(
        seen=False, input_tokens=0, output_tokens=0, estimate=65024,
        request_bytes=4000, sent_bytes=0, aborted=True, status=200)
    require_equal(quelle2, "measured_bytes", f"falsche Quelle: {quelle2}")
    require_equal(aus2, 0, "es wurde Ausgang gebucht, der nie floss")
    require(ein2 > 0,
            "ein Abbruch ist ein Freifahrtschein geworden — der Anbieter hat "
            "den Rumpf gelesen")

    # 4) Anbieterfehler ohne usage: die Schaetzung bleibt. Fail-safe.
    require_equal(px.settled_charge(seen=False, input_tokens=0, output_tokens=0,
                                    estimate=65024, request_bytes=4000,
                                    sent_bytes=0, aborted=False, status=500),
                  (65024, 0, "estimated"),
                  "ein Anbieterfehler setzt die Kappe auf einen Messwert, den "
                  "es nicht gibt")

    # 5) Kein Abbruch, kein usage — ebenfalls fail-safe.
    require_equal(px.settled_charge(seen=False, input_tokens=0, output_tokens=0,
                                    estimate=65024, request_bytes=4000,
                                    sent_bytes=900, aborted=False, status=200)[2],
                  "estimated",
                  "ohne Abbruch und ohne usage wird gemessen statt geschaetzt")


def t_0185_an_aborted_stream_no_longer_carries_the_estimate():
    """Der Beweis am Buch, nicht an der Funktion.

    Derselbe Ablauf wie beim Lease-Schluss: ein echter Strom wird
    abgeschnitten. Die Zeile muss danach `measured_bytes` tragen und deutlich
    weniger als die Vorbelastung.
    """
    from solvio.provider_broker.ledger import BrokerLedger

    async def go():
        async with Harness(body=TEXT_STREAM, chunk_pause=0.4) as h:
            token = h.broker.register_principal("deep-gateway")
            lease = h.broker.open_lease("deep-gateway", "t",
                                        deadline=time.time() + 60)
            call = asyncio.create_task(h.call(token))
            await asyncio.sleep(0.5)
            h.broker.close_lease(lease)
            await call

            ledger = BrokerLedger(h._db)
            ledger.open()
            try:
                row = ledger.rows(limit=10)[0]
            finally:
                ledger.close()
            require_equal(row["outcome"], "client_aborted",
                          f"falscher Ausgang: {row['outcome']}")
            require_equal(row["tokens_source"], "measured_bytes",
                          f"die Zeile traegt noch die Schaetzung: "
                          f"{row['tokens_source']}")
            require(row["input_tokens"] > 0,
                    "ein Abbruch buchte gar nichts — das waere ein "
                    "Freifahrtschein")
    run(go())


def t_a_client_aborted_stream_is_not_booked_as_upstream_error():
    """Der Anbieter antwortete mit `200`, der Klient brach ab — das ist kein
    Anbieterfehler.

    Derselbe Ablauf wie beim Lease-Schluss oben, aber diesmal wird das Buch
    gelesen: die Zeile darf nicht als `upstream_error` stehen, wenn der
    Statuscode ein Erfolg war und der Abbruch vom Kaefig kam.
    """
    from solvio.provider_broker.ledger import BrokerLedger

    async def go():
        async with Harness(body=TEXT_STREAM, chunk_pause=0.4) as h:
            token = h.broker.register_principal("deep-gateway")
            lease = h.broker.open_lease("deep-gateway", "t", deadline=time.time() + 60)

            call = asyncio.create_task(h.call(token))
            await asyncio.sleep(0.5)          # der Strom laeuft
            h.broker.close_lease(lease)       # letztes Lease faellt, Abbruch
            status, payload = await call
            require(len(payload) < len(TEXT_STREAM),
                    "der Strom wird tatsaechlich abgeschnitten")

            ledger = BrokerLedger(h._db)
            ledger.open()
            try:
                rows = ledger.rows(limit=10)
            finally:
                ledger.close()
            row = rows[0]
            require_equal(row["status_code"], 200,
                          "der Anbieter hat mit einem Erfolgscode geantwortet")
            require_equal(row["outcome"], "client_aborted",
                          "ein Erfolgscode mit Klientenabbruch ist keine "
                          "upstream_error — sonst behauptet das Buch einen "
                          "Anbieterfehler, den es nie gab")
            require(row["outcome"] != "upstream_error",
                    "die falsche Aussage darf nicht mehr entstehen")
    run(go())


def t_a_deep_task_leaves_no_open_lease_on_any_outcome():
    """Der Schluss im `finally` ist der einzige Ort, der JEDEN Ausgang sieht.

    Erfolg, Schemafehler, Zeitablauf, Abbruch, Absturz — und der Selbstabbruch,
    bei dem `_one_run` schlicht `None` liefert und `_drive` ohne jede Ausnahme
    durchlaeuft.
    """
    from datetime import datetime, timezone

    from solvio.contracts.deep_runtime import DeepTask, DeepTaskType, TaskOrigin
    from solvio.contracts.trust import TrustContext, TrustLevel
    from solvio.deep import runtime as rt

    class _Broker:
        def __init__(self):
            self.open = {}
            self.budgets = []
            self.counter = 0

        def open_lease(self, principal, ref, *, deadline, max_tokens=None,
                       max_requests=None):
            self.counter += 1
            key = f"lease-{self.counter}"
            self.open[key] = principal
            self.budgets.append((max_tokens, max_requests))
            return key

        def close_lease(self, lease_id):
            self.open.pop(lease_id, None)

    async def go():
        for outcome in ("erfolg", "schema", "zeitablauf", "abbruch", "absturz",
                        "selbstabbruch"):
            broker = _Broker()
            engine = rt.HermesDeepRuntime.__new__(rt.HermesDeepRuntime)
            engine.broker = broker
            engine._leases = {}
            task = DeepTask(id=f"task-{outcome}", task_type=DeepTaskType.RESEARCH,
                            instruction="x", origin=TaskOrigin.USER_VOICE,
                            trust_context=TrustContext(
                                origin_trust=TrustLevel.USER_DIRECT),
                            created_at=datetime.now(timezone.utc), timeout=1.0)
            engine._open_lease(task)
            require_equal(len(broker.open), 1, f"{outcome}: das Fenster ist offen")
            # Und es traegt das Aufgabenbudget. Ohne diese Zeile koennte der
            # Deckel aus dem Aufruf verschwinden, ohne dass ein Test es merkt —
            # er war schon einmal deklariert und wurde nie gelesen.
            require_equal(broker.budgets[-1][1], rt.MAX_PROVIDER_CALLS,
                          f"{outcome}: die Anrufgrenze reist mit")
            engine._close_lease(task.id)
            require_equal(len(broker.open), 0,
                          f"{outcome}: und es ist danach zu")

    run(go())
    # Und die Struktur selbst: der Schluss steht in einem `finally`, nicht auf
    # einem Erfolgspfad, den ein Fehler ueberspringt.
    with open(os.path.join(SRC, "solvio", "deep", "runtime.py"),
              encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    drive = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "_drive"]
    require_equal(len(drive), 1, "es gibt genau eine Pumpe")
    finals = [n for n in ast.walk(drive[0]) if isinstance(n, ast.Try) and n.finalbody]
    require(finals, "sie hat ein finally")
    closes = [n for f in finals for n in ast.walk(ast.Module(body=f.finalbody,
                                                             type_ignores=[]))
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", "") == "_close_lease"]
    require(closes, "und darin wird das Lease geschlossen")


def t_a_bot_question_leaves_no_open_lease_on_any_outcome():
    """`ask` hat nach dem Lauf sechs Ausgaenge. Keiner darf ein Lease halten."""
    with open(os.path.join(SRC, "solvio", "bots", "team.py"),
              encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    ask = [n for n in ast.walk(tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "ask"]
    require_equal(len(ask), 1, "es gibt genau ein ask")
    finals = [n for n in ast.walk(ask[0]) if isinstance(n, ast.Try) and n.finalbody]
    require(finals, "der Lauf steht in einem try/finally")
    closes = [n for f in finals for n in ast.walk(ast.Module(body=f.finalbody,
                                                             type_ignores=[]))
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", "") == "_close_lease"]
    require(closes, "und das Lease wird dort geschlossen")

    from solvio.bots import team as team_mod

    class _Broker:
        def __init__(self):
            self.open = {}

        def open_lease(self, principal, ref, *, deadline):
            self.open["l"] = principal
            return "l"

        def close_lease(self, lease_id):
            self.open.pop(lease_id, None)

    jail = runner_jail()
    broker = _Broker()
    team = team_mod.BotTeam(jail, broker=broker, root=SRC)
    lease = team._open_lease("solvio-researcher", "frage")
    require_equal(len(broker.open), 1, "das Fenster geht auf")
    team._close_lease("solvio-researcher", lease)
    require_equal(len(broker.open), 0, "und wieder zu")


def runner_jail():
    from solvio.bots import runner

    return runner.Jail(path="/tmp/solvio-test-jail",
                       venv_bin="/tmp/solvio-test-jail/venv/bin",
                       python_root="/tmp/python")


def t_the_startup_scrub_is_called_unconditionally_before_the_broker():
    """Eine Mutation, die den Aufruf in ein `if False` legt, muss auffallen.

    Der Test auf die Funktion allein genuegt nicht — er prueft, dass das
    Ausraeumen FUNKTIONIERT, nicht dass es STATTFINDET.
    """
    path = os.path.join(SRC, "solvio", "realtime", "core_server.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    serve = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve"]
    require_equal(len(serve), 1, "es gibt genau ein serve")

    found = None
    lifetime = next(n for n in serve[0].body if isinstance(n, ast.Try) and n.finalbody)
    require_equal(lifetime.handlers, [], "outer lifetime never swallows bootstrap failures")
    for index, node in enumerate(lifetime.body):
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_scrub_jail_credentials"]
        if calls:
            found = (index, node)
            break
    require(found is not None, "serve raeumt die Kaefig-Zugaenge aus")
    index, node = found
    require(isinstance(node, ast.Expr),
            "und zwar als blosse Anweisung — nicht unter einer Bedingung, "
            "nicht in einem try, nicht in einer Schleife")


def t_a_broker_bind_failure_is_caught_broadly_enough_to_spare_the_voice():
    """`except ZeroDivisionError` liesse einen `OSError` durch — und der Core faellt.

    Ein Broker, der den Core mitnimmt, waere die schlechtere Schuld.
    """
    path = os.path.join(SRC, "solvio", "realtime", "core_server.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    serve = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve"][0]

    guarded = []
    for node in ast.walk(serve):
        if not isinstance(node, ast.Try):
            continue
        starts = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", "") == "start"
                  and getattr(getattr(n.func, "value", None), "id", "") == "candidate"]
        if not starts:
            continue
        for handler in node.handlers:
            name = getattr(handler.type, "id", "")
            guarded.append(name)
    require("Exception" in guarded,
            "der Broker-Start ist breit gefasst — ein Bindefehler nimmt die "
            "Stimme nicht mit")


def t_the_deep_handle_is_published_before_the_start_can_fail():
    """Der produktive Reparaturdefekt: der Doktor fand keinen Dienst.

    Der Griff wurde erst NACH einem erfolgreichen Start hinterlegt. Scheiterte
    der ERSTE Start — und genau das tat er, weil das Gefaengnis angeblich belegt
    war —, hatte der Doktor nichts, was er haette neu starten koennen, und
    meldete zweimal eine fehlgeschlagene Reparatur, bevor er aufgab.
    """
    path = os.path.join(SRC, "solvio", "realtime", "core_server.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    serve = [n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve"][0]

    assign_line = None
    start_line = None
    for node in ast.walk(serve):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (getattr(target, "attr", "") == "deep_service"
                        and getattr(node.value, "id", "") == "deep"):
                    if assign_line is None or node.lineno < assign_line:
                        assign_line = node.lineno
        if isinstance(node, ast.Await):
            call = node.value
            if (isinstance(call, ast.Call)
                    and getattr(call.func, "attr", "") == "start"
                    and getattr(getattr(call.func, "value", None), "id", "") == "deep"):
                start_line = call.lineno
    require(assign_line is not None, "der Griff wird hinterlegt")
    require(start_line is not None, "und der Start passiert")
    require(assign_line < start_line,
            "der Griff steht VOR dem Start — sonst kann der Doktor einen "
            "gescheiterten Erststart nicht reparieren")


def t_the_router_registers_exactly_the_allowlisted_paths():
    """Das Pfadtor im Handler ist die ZWEITE Schranke; die erste ist der Router.

    Eine Mutation, die das Tor im Handler oeffnet, ueberlebte deshalb — zu
    Recht: `/v1/embeddings` erreicht diesen Handler gar nicht. Damit das so
    bleibt und nicht eines Tages still aufgeweicht wird, steht die Routentafel
    hier als eigene Zusicherung.
    """
    code = _source("service.py")
    tree = ast.parse(code)
    routes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_route":
            args = [a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if len(args) >= 2:
                routes.append((args[0], args[1]))
    forwarded = [path for method, path in routes if path in px.FORWARDED_PATHS]
    require_equal(sorted(forwarded), sorted(px.FORWARDED_PATHS),
                  "genau die zwei weitergeleiteten Pfade sind verdrahtet")
    for method, path in routes:
        if path in px.FORWARDED_PATHS:
            require_equal(method, "POST", f"{path} nimmt nur POST")
    catch_all = [p for _, p in routes if p.startswith("/{")]
    require(catch_all, "und alles andere faellt in die kategorische Absage")


def t_registering_one_principal_never_invalidates_another():
    """Die schaerfere Fassung: nicht der ZAEHLER, sondern der TOKEN zaehlt."""
    registry = sess.Registry()
    deep = registry.register("deep-gateway")
    a = registry.register(bot_principal("solvio-researcher"))
    b = registry.register(bot_principal("solvio-diagnostician"))
    registry.register("deep-gateway")
    for name, token in (("researcher", a), ("diagnostician", b)):
        require(registry.resolve(token) is not None,
                f"{name} behaelt seinen Zugang")
    require(registry.resolve(deep) is None, "nur der neu gepraegte verliert ihn")


# ------------------------------------------------------------------------
# N8/C4 — der Nexus-Claude-Arbeiter als eigener Auftraggeber auf der
# Anthropic-Flaeche: Kappen, Faehigkeitsbindung je Auftraggeber, Buch je
# Auftrag (`usage_for_task`) und die Nichtstart-Klassifikation darauf.
# ------------------------------------------------------------------------
from solvio.provider_broker import anthropic as AN  # noqa: E402
from solvio.provider_broker import service as SVC  # noqa: E402

SYNTHETIC_CREDENTIAL = "sk-ant-oat-SYNTHETISCH-BROKERSUITE-0002"
#: Ein Strom mit einer `usage`-Zeile in der Form, die der vorhandene
#: Nutzungsleser des Brokers liest (`input_tokens`/`output_tokens` am
#: Ereignis). Die native Anthropic-Form (`message.usage`, `message_delta`
#: nur mit `output_tokens`) liest er NICHT — dann bleibt die Schaetzung
#: gebucht; das ist eine bestehende Grenze des Brokers, nicht dieses Buches.
ANTHROPIC_STREAM = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","usage":{"input_tokens":12,"output_tokens":7}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


def t_the_broker_suite_vault_is_redirected_away_from_production():
    from solvio.secret_vault import keyring as VK
    from solvio.secret_vault.store import vault_dir
    require("/.solvio-vault" not in vault_dir(), "Tresor nicht umgelenkt")
    require(VK.is_test_backend(), "Schluesselbund des Tresors nicht umgelenkt")


_KEK_READY = False


def _vault(capabilities, *, target, kind="subscription_oauth"):
    global _KEK_READY
    from solvio.secret_vault import admin as VA, broker as VB, keyring as VK, policy as VP
    from solvio.secret_vault.store import VaultStore
    if not _KEK_READY:
        # Once per process: every store below is sealed under this one key.
        VK.initialize_kek(allow_overwrite=True)
        _KEK_READY = True
    store = VaultStore(os.path.join(tempfile.mkdtemp(dir=_VAULT_SANDBOX), "v.sqlite3"))
    VA.add(secret_ref=AN.SECRET_REF,
           kind=VP.SecretKind.OAUTH_REFRESH_TOKEN if kind == "subscription_oauth" else VP.SecretKind.API_KEY,
           plaintext=json.dumps({"kind": kind, "token": SYNTHETIC_CREDENTIAL}).encode(),
           allowed_capabilities=list(capabilities), allowed_targets=[target],
           allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
           allow_background=True, display_name="synthetisch", store=store, replace=True)
    return VB.SecretBroker(store)


class _AnthropicHarness:
    """Broker mit Tresor und falschem Anthropic-Anbieter auf der Rueckschleife."""

    def __init__(self, capabilities, *, status=200, body=ANTHROPIC_STREAM):
        from _broker_fixtures import FakeUpstream
        self.upstream = FakeUpstream(body=body, status=status)
        self.capabilities = capabilities
        self.broker = None
        self._pinned = ""
        self._db = ""

    async def __aenter__(self):
        origin = await self.upstream.start()
        self._pinned = AN.UPSTREAM_ORIGIN
        AN.UPSTREAM_ORIGIN = origin
        self._db = os.path.join(tempfile.mkdtemp(dir=_VAULT_SANDBOX), "broker.sqlite3")
        self.broker = BrokerService(provider_key=FAKE_PROVIDER_KEY, port=free_port(),
                                    ledger_path=self._db,
                                    vault=_vault(self.capabilities, target=origin))
        await self.broker.start()
        return self

    async def __aexit__(self, *exc):
        if self.broker is not None:
            await self.broker.stop()
        await self.upstream.stop()
        AN.UPSTREAM_ORIGIN = self._pinned
        return False

    def rows(self, limit: int = 200) -> list[dict]:
        return self.broker.ledger.rows(limit=limit)

    async def messages(self, token, *, model="claude-sonnet-5", **extra):
        import aiohttp
        body = json.dumps({"model": model, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "eine Frage"}], **extra}).encode()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(f"http://127.0.0.1:{self.broker.port}/v1/messages",
                                    data=body, headers={"x-api-key": token,
                                                        "Content-Type": "application/json"}) as response:
                return response.status, await response.read()


def t_the_nexus_worker_principal_has_its_own_caps_on_the_anthropic_surface():
    name = SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL
    require_equal(name, "nexus-worker-claude")
    require(name in SVC.ANTHROPIC_PRINCIPALS, "der Arbeiter fehlt auf der Anthropic-Flaeche")
    caps = sess.NEXUS_WORKER_CLAUDE_CAPS
    require_equal((caps.max_leases, caps.max_inflight, caps.requests_per_day, caps.tokens_per_day),
                  (2, 2, 600, 3_000_000))
    require_equal(caps.allowed_models, frozenset({"claude-sonnet-5"}))
    require_equal(caps.allowed_provider_tools, frozenset())
    async def go():
        async with Harness() as h:
            h.broker.register_principal(name)
            require(h.broker.registry.principal(name).caps is caps,
                    "der Arbeiter bekam die Kappen eines anderen Auftraggebers")
            writer = SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL
            h.broker.register_principal(writer)
            require(h.broker.registry.principal(writer).caps is sess.AUTOPILOT_WRITER_CLAUDE_CAPS)
    run(go())


def t_running_names_only_a_listening_broker_of_this_process():
    async def go():
        require(SVC.running() is None, "kein Broker gestartet, aber einer gemeldet")
        async with Harness() as h:
            require(SVC.running() is h.broker)
        require(SVC.running() is None, "nach stop() noch gemeldet")
    run(go())


def t_the_worker_borrows_under_its_own_capability_and_never_under_the_writers():
    from solvio.provider_broker.anthropic import AnthropicAuthError
    class Inbound:
        headers = {"content-type": "application/json", "x-api-key": "broker-token-des-kaefigs"}
    target = "https://api.anthropic.com"
    both = AN.AnthropicUpstream(_vault([AN.CAPABILITY, AN.WORKER_CAPABILITY], target=target))
    writer_only = AN.AnthropicUpstream(_vault([AN.CAPABILITY], target=target))
    worker_only = AN.AnthropicUpstream(_vault([AN.WORKER_CAPABILITY], target=target))
    require_equal(AN.capability_for(SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL),
                  (AN.WORKER_CAPABILITY, AN.WORKER_AUTOMATION_ID))
    require_equal(AN.capability_for(SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL), (AN.CAPABILITY, AN.AUTOMATION_ID))
    require_equal(AN.capability_for(""), (AN.CAPABILITY, AN.AUTOMATION_ID))
    for upstream, principal, ok in (
            (both, SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL, True),
            (both, SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL, True),
            (writer_only, SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL, True),
            (writer_only, SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL, False),
            (worker_only, SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL, True),
            (worker_only, SVC.AUTOPILOT_WRITER_CLAUDE_PRINCIPAL, False),
            (both, "deep-gateway", False)):
        try:
            kopf = upstream.outbound_headers(Inbound(), principal=principal)
        except AnthropicAuthError as exc:
            require(not ok, f"{principal} wurde zu Unrecht abgewiesen: {exc.reason}")
            require_equal(exc.reason, "credential_denied")
        else:
            require(ok, f"{principal} durfte unter fremder Faehigkeit leihen")
            require_equal(kopf.get("Authorization"), "Bearer " + SYNTHETIC_CREDENTIAL)
    # Der bestehende Aufruf ohne Auftraggeber bleibt der Schreiber.
    require_equal(writer_only.outbound_headers(Inbound()).get("Authorization"),
                  "Bearer " + SYNTHETIC_CREDENTIAL)


def t_usage_for_task_sums_worked_rows_and_reports_the_status_distribution_per_task():
    from solvio.agent_runtime.native_tasks import nonstart_proven
    name = SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL
    async def go():
        async with _AnthropicHarness([AN.CAPABILITY, AN.WORKER_CAPABILITY]) as h:
            token = h.broker.register_principal(name)
            # Ohne Lease: Ablehnung ohne Auftragsverweis — sie gehoert keinem Auftrag.
            status, _ = await h.messages(token)
            require_equal(status, 403)
            lease = h.broker.open_lease(name, "task:at-one", deadline=time.time() + 60)
            status, _ = await h.messages(token)
            require_equal(status, 200)
            status, _ = await h.messages(token, model="claude-opus-5")   # Modelltor: denied 403
            require_equal(status, 403)
            h.broker.close_lease(lease)
            book = h.broker.ledger.usage_for_task("task:at-one", since=0)
            require_equal(book["rows"], 2)
            require_equal(book["outcomes"], {"forwarded": 1, "denied": 1})
            require_equal(book["status_codes"], {"200": 1, "403": 1})
            require_equal((book["requests"], book["input_tokens"], book["output_tokens"]), (1, 12, 7))
            require_equal(book["tokens"], 19)
            require(not nonstart_proven(book), "ein weitergeleiteter Turn ist kein Nichtstart")
            other = h.broker.ledger.usage_for_task("task:at-other", since=0)
            require_equal((other["rows"], other["requests"], other["tokens"]), (0, 0, 0))
            require(not nonstart_proven(other), "ein leeres Buch beweist keinen Nichtstart")
            for row in h.rows():
                require(SYNTHETIC_CREDENTIAL not in json.dumps(row) and token not in json.dumps(row))
            forwarded = [r for r in h.rows() if r["outcome"] == "forwarded"]
            require_equal(forwarded[0]["task_ref"], "task:at-one",
                          "die weitergeleitete Zeile traegt den Auftragsverweis")
            require_equal(forwarded[0]["principal"], name)
            upstream_call = h.upstream.calls[-1]
            require_equal(upstream_call["headers"].get("authorization"), "Bearer " + SYNTHETIC_CREDENTIAL)
            require("broker-token" not in json.dumps(upstream_call["headers"]))
    run(go())


def t_a_quota_refused_turn_is_classified_as_nonstart_only_from_exclusive_429_rows():
    from solvio.agent_runtime.native_tasks import nonstart_proven
    name = SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL
    async def go():
        # 1. Der Anbieter antwortet auf jede Anfrage 429: upstream_error/429/0.
        async with _AnthropicHarness([AN.CAPABILITY, AN.WORKER_CAPABILITY], status=429, body=b"") as h:
            token = h.broker.register_principal(name)
            since = time.time() - 1
            lease = h.broker.open_lease(name, "task:at-quota", deadline=time.time() + 60)
            for _ in range(2):
                status, _ = await h.messages(token)
                require_equal(status, 429)
            h.broker.close_lease(lease)
            book = h.broker.ledger.usage_for_task("task:at-quota", since=since)
            require_equal(book["outcomes"], {"upstream_error": 2})
            require_equal(book["status_codes"], {"429": 2})
            require_equal(book["output_tokens"], 0)
            require(nonstart_proven(book), "ausschliesslich 429 ohne Ausgabetoken ist der Nichtstart")
        # 2. Die eigene Kappe lehnt ab (denied/429), ebenfalls ein Nichtstart.
        async with _AnthropicHarness([AN.CAPABILITY, AN.WORKER_CAPABILITY]) as h:
            token = h.broker.register_principal(name)
            since = time.time() - 1
            lease = h.broker.open_lease(name, "task:at-capped", deadline=time.time() + 60, max_requests=0)
            status, body = await h.messages(token)
            require_equal(status, 429)
            require("lease_request_capped" in body.decode())
            h.broker.close_lease(lease)
            book = h.broker.ledger.usage_for_task("task:at-capped", since=since)
            require_equal((book["outcomes"], book["status_codes"]), ({"denied": 1}, {"429": 1}))
            require(nonstart_proven(book))
        # 3. Gegenprobe: eine einzige 200-Zeile im selben Fenster — kein Nichtstart.
        async with _AnthropicHarness([AN.CAPABILITY, AN.WORKER_CAPABILITY]) as h:
            token = h.broker.register_principal(name)
            since = time.time() - 1
            lease = h.broker.open_lease(name, "task:at-mixed", deadline=time.time() + 60, max_requests=1)
            require_equal((await h.messages(token))[0], 200)
            require_equal((await h.messages(token))[0], 429)
            h.broker.close_lease(lease)
            book = h.broker.ledger.usage_for_task("task:at-mixed", since=since)
            require_equal(book["status_codes"], {"200": 1, "429": 1})
            require(not nonstart_proven(book), "eine 200-Zeile heisst: der Anbieter hat geantwortet")
        # 4. Ein Fenster VOR dem Turn zaehlt nicht mit: `since` grenzt ab.
            later = h.broker.ledger.usage_for_task("task:at-mixed", since=time.time() + 10)
            require_equal(later["rows"], 0)
            require(not nonstart_proven(later))
    run(go())


def t_the_anthropic_surface_refuses_provider_side_tools_unless_the_principal_may_name_them():
    """Review Runde 16, B16-1: die Anthropic-Flaeche hatte kein Tor 5b — der Claude-Auftragsarbeiter
    haette aus dem „netzlosen" Kaefig ein Anbieter-`web_search` mitschicken koennen, und der Broker
    haette es mit der echten Anmeldung weitergeleitet (ein unbeobachteter Netzkanal). Jetzt gilt
    dort dieselbe Regel wie auf der OpenAI-Flaeche: eingeschlossen, nie ausgeschlossen. Die
    klientenseitigen Werkzeuge der CLI (gemessen 20.09.2026: Bash/Edit/Read ohne `type`, mit
    `input_schema`) bleiben erlaubt; `custom` ebenso."""
    name = SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL
    from solvio.provider_broker import proxy as px
    require_equal(sess.NEXUS_WORKER_CLAUDE_CAPS.allowed_provider_tools, frozenset())
    # das Tor selbst, deterministisch
    px.check_anthropic_provider_tools({"tools": [{"name": "Bash", "input_schema": {"type": "object"}},
                                                 {"type": "custom", "name": "Read", "input_schema": {}}]}, allowed=frozenset())
    for body in ({"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
                 {"tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}]},
                 {"tools": [{"type": "code_execution_20250522", "name": "code_execution"}]},
                 {"tools": [{"type": "computer_20250124", "name": "computer"}]},
                 {"tools": [{"type": 7, "name": "x"}]},
                 {"mcp_servers": [{"type": "url", "url": "https://example.invalid/mcp", "name": "m"}]}):
        try:
            px.check_anthropic_provider_tools(body, allowed=frozenset())
        except px.BodyRejected as exc:
            require_equal(exc.reason, "provider_tool_not_allowed")
        else:
            raise AssertionError(f"accepted: {body}")
    # eine bewusste Politik je Auftraggeber: Familie oder voller Typ
    px.check_anthropic_provider_tools({"tools": [{"type": "web_search_20250305"}]}, allowed=frozenset({"web_search"}))
    px.check_anthropic_provider_tools({"tools": [{"type": "web_search_20250305"}]}, allowed=frozenset({"web_search_20250305"}))
    px.check_anthropic_provider_tools({"mcp_servers": [{"type": "url"}]}, allowed=frozenset({"mcp"}))

    async def go():
        async with _AnthropicHarness([AN.CAPABILITY, AN.WORKER_CAPABILITY]) as h:
            token = h.broker.register_principal(name)
            lease = h.broker.open_lease(name, "task:at-tools", deadline=time.time() + 60)
            status, _ = await h.messages(token, tools=[{"name": "Bash", "input_schema": {"type": "object"}}])
            require_equal(status, 200, "die klientenseitigen Werkzeuge der CLI kamen nicht durch")
            forwarded_before = len(h.upstream.calls)
            # Der eine erlaubte Rumpf oben hat gebucht; ab hier darf nichts mehr dazukommen.
            forwarded_tokens = int(h.broker.registry.principal(name).tokens_today)
            status, body = await h.messages(token, tools=[{"type": "web_search_20250305", "name": "web_search"}])
            require_equal(status, 403)
            require("provider_tool_not_allowed" in body.decode())
            status, body = await h.messages(token, mcp_servers=[{"type": "url", "url": "https://example.invalid/mcp", "name": "m"}])
            require_equal((status, "provider_tool_not_allowed" in body.decode()), (403, True))
            require_equal(len(h.upstream.calls), forwarded_before, "ein abgewiesener Rumpf erreichte den Anbieter")
            # Die Werkzeugpruefung liegt VOR der Vorbuchung — und das ist nur dann eine
            # Zusicherung, wenn es jemand misst: eine Ablehnung darf weder die Tageskappe
            # noch das Auftragsbudget belasten. Hinter `precharge` verschoben faehrt ein
            # Auftraggeber beides mit abgewiesenen Ruempfen leer, ohne dass eine Suite rot
            # wird (Review Runde 23, B1 — gemessen: der Mutant bucht 59 Token und einen
            # Aufruf je Ablehnung, Status bleibt 403).
            after_one = h.broker.registry.principal(name)
            require_equal((int(after_one.requests_today), int(after_one.tokens_today)), (1, forwarded_tokens),
                          "eine abgewiesene Werkzeugflaeche hat Tageskappe oder Budget belastet")
            h.broker.close_lease(lease)
            rows = [r for r in h.rows() if r["principal"] == name]
            require_equal(sorted(r["denied_reason"] for r in rows if r["outcome"] == "denied"),
                          ["provider_tool_not_allowed", "provider_tool_not_allowed"])
            require_equal(sum(1 for r in rows if r["outcome"] == "forwarded"), 1)
    run(go())


def t_the_worker_without_the_capability_in_the_vault_is_denied_not_borrowed():
    name = SVC.NEXUS_WORKER_CLAUDE_PRINCIPAL
    async def go():
        async with _AnthropicHarness([AN.CAPABILITY]) as h:          # nur der Schreiber im Grant
            token = h.broker.register_principal(name)
            lease = h.broker.open_lease(name, "task:at-nogrant", deadline=time.time() + 60)
            status, body = await h.messages(token)
            h.broker.close_lease(lease)
            require_equal(status, 503)
            require("credential_denied" in body.decode(), body[:120])
            require_equal(h.upstream.calls, [], "ohne Faehigkeit ging nichts zum Anbieter")
            rows = [r for r in h.rows() if r["task_ref"] == "task:at-nogrant"]
            require_equal([r["outcome"] for r in rows], ["denied"])
            require_equal(rows[0]["denied_reason"], "credential_denied")
    run(go())


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
