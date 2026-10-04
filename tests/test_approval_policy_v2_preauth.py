"""Die gebundene Vorab-Autorisierung — die einzige Lockerung in Approval Policy V2.

Der Nutzer hat genau eine Bequemlichkeit bestellt: „Mach jeden Abend um 20 Uhr
das Aussenlicht an" soll nicht jeden Abend eine Face-ID-Runde kosten. Wer
zehnmal am Tag gedankenlos bestaetigt, prueft beim elften Mal nicht mehr, WAS
er bestaetigt — die Gewoehnung ist hier das Sicherheitsrisiko, nicht die
Ausnahme.

Was daraus NICHT werden darf, ist eine Vollmacht. Diese Datei misst genau diese
Grenze, und sie misst sie am Verhalten: gepraegt wird eine Bindung an die
WIRKUNG — Faehigkeit, aufgeloestes Geraet, am Geraet gemessene Klasse, exakte
Argumente, Zeitplanregel, Identitaet der Automatisierung. Aendert sich davon
irgendetwas material, traegt die alte Erlaubnis nicht mehr.

Der Satz, an dem alles haengt und den mehrere Tests hier einzeln nachweisen:
eine Erlaubnis fuer „Aussenlicht an um 20 Uhr" kann strukturell nie „Haustuer
auf um 20 Uhr" autorisieren.

Hermetisch: kein Netz, kein echtes Home Assistant, kein echtes Freigabe-Gateway.
Das Haus ist aus Papier, der Speicher liegt in einer Wegwerfdatei.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

enforce_assertions()

from solvio.capabilities import preauth as PA  # noqa: E402
from solvio.capabilities import proactive as PRO  # noqa: E402
from solvio.capabilities.contract import ArgumentSource  # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome  # noqa: E402
from solvio.capabilities.home_assistant import (  # noqa: E402
    HACapabilities, HAExposure, register as ha_register,
)
from solvio.capabilities.invocation import (  # noqa: E402
    CapabilityInvocationGate, voice_trust,
)
from solvio.capabilities.policy import (  # noqa: E402
    ActionClass, Decision, OriginClass,
)
from solvio.capabilities.router import CapabilityRouter  # noqa: E402
from solvio.proactive import schedule as SCH  # noqa: E402
from solvio.proactive import store as S  # noqa: E402
from solvio.proactive.runner import background_trust  # noqa: E402
from solvio.security.approval import ApprovalBroker  # noqa: E402

#: Zwei feste Augenblicke. Zeitplaene werden hier nie gegen die echte Uhr
#: gemessen — ein Test, der um 20:00 anders ausgeht als um 21:00, misst die
#: Uhr und nicht die Bindung.
T1 = 1_780_000_000.0
T2 = T1 + 3 * 86_400 + 5 * 3_600

#: Der Satz, mit dem der Mensch die Automatisierung anlegt.
AUFTRAG_SATZ = "Mach jeden Abend um acht das Aussenlicht an."

AUFTRAG = {"titel": "Aussenlicht abends", "wann": "taeglich 20:00",
           "aktion": "ha_turn_on", "argumente": {"name": "Aussenlicht"}}


# =====================================================================
# Ein Haus, das es nicht gibt
# =====================================================================

_EXPOSED = {
    "light.aussen": {"conversation": True},
    "light.wohnzimmer": {"conversation": True},
    # Auffindbar, aber sicherheitsrelevant — genau das Geraet, das die
    # Erlaubnis fuer die Lampe niemals erreichen darf.
    "lock.haustuer": {"conversation": True},
}

_STATES = [
    {"entity_id": "light.aussen", "state": "off",
     "attributes": {"friendly_name": "Aussenlicht"}},
    {"entity_id": "light.wohnzimmer", "state": "off",
     "attributes": {"friendly_name": "Wohnzimmerlicht"}},
    {"entity_id": "lock.haustuer", "state": "locked",
     "attributes": {"friendly_name": "Haustuer", "device_class": "lock"}},
]

_ENTITY_REGISTRY = [
    {"entity_id": "light.aussen", "area_id": "garten", "device_id": None,
     "name": None, "original_name": "Aussenlicht"},
    {"entity_id": "light.wohnzimmer", "area_id": "wohnzimmer", "device_id": None,
     "name": None, "original_name": "Wohnzimmerlicht"},
    {"entity_id": "lock.haustuer", "area_id": "flur", "device_id": None,
     "name": None, "original_name": "Haustuer"},
]

_AREA_REGISTRY = [{"area_id": "garten", "name": "Garten"},
                  {"area_id": "wohnzimmer", "name": "Wohnzimmer"},
                  {"area_id": "flur", "name": "Flur"}]


class _PapierHaus:
    """Ein Home Assistant aus Papier. Zaehlt mit, was wirklich geschaltet wurde.

    Die Liste `calls` ist der einzige ehrliche Beleg dafuer, ob eine Wirkung
    eingetreten ist. „Der Router hat SUCCESS gesagt" ist keiner.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._state = {s["entity_id"]: dict(s) for s in _STATES}

    async def ws_commands(self, types):
        return {
            "homeassistant/expose_entity/list": {"exposed_entities": dict(_EXPOSED)},
            "config/entity_registry/list": list(_ENTITY_REGISTRY),
            "config/device_registry/list": [],
            "config/area_registry/list": list(_AREA_REGISTRY),
        }

    async def states(self):
        return [dict(v) for v in self._state.values()]

    async def state(self, entity_id):
        return dict(self._state.get(entity_id,
                                    {"entity_id": entity_id, "state": "unknown"}))

    async def call_service(self, domain, service, data=None):
        self.calls.append((domain, service, dict(data or {})))
        entity_id = (data or {}).get("entity_id", "")
        if entity_id in self._state:
            self._state[entity_id]["state"] = "on" if service == "turn_on" else "off"
        return []


class _Approver:
    def is_trusted(self, request, identity):
        return identity == "owner"


def _speicher(path: str = "") -> S.ProactiveStore:
    """Ein echter ProactiveStore auf einer Wegwerfdatei — kein Speicher aus Papier.

    Die Dauerhaftigkeit einer Erlaubnis ist selbst eine Zusage dieses
    Meilensteins; sie laesst sich gegen ein Dictionary nicht pruefen.
    """
    return S.ProactiveStore(
        path or os.path.join(tempfile.mkdtemp(prefix="solvio-preauth-"),
                             "proactive.sqlite3"))


def _stapel(*, store=None, security_entities=frozenset(), clock=None):
    """Router + Gate + Haustechnik + Speicher — verdrahtet wie im Betrieb.

    Der Freigabeweg ist bewusst vorhanden: ohne Erlaubnis soll ein Lauf als
    `APPROVAL_REQUIRED` enden und nicht mangels Kanal — sonst waere nicht zu
    unterscheiden, ob die Bindung fehlte oder die Verdrahtung.
    """
    haus = _PapierHaus()
    store = store if store is not None else _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    router = CapabilityRouter(approvals=ApprovalBroker(approver=_Approver()),
                              preauth=preauth)
    ha_register(router, HACapabilities(HAExposure(haus, ttl=0.0),
                                       frozenset(security_entities)))
    gate = CapabilityInvocationGate()
    caps = PRO.ProactiveCapabilities(store, gate=gate, preauth=preauth,
                                     router=router, clock=clock or (lambda: T1))
    return haus, router, gate, store, preauth, caps


def _turn(gate, *, origin=OriginClass.TRUSTED_INTERACTIVE_APP,
          said=AUFTRAG_SATZ, principal="gregor"):
    """Ein Turn mit einer HERKUNFT. Sie entscheidet, ob eine Erlaubnis entstehen darf."""
    gate.begin_turn(session_id="s-preauth", turn_id="t1", principal=principal,
                    trust=voice_trust(True), user_text=said, origin=origin)
    return gate.context()


async def _aufgabe(store, *, task_id="bt-aussenlicht", plan=None, arguments=None,
                   capability="ha_turn_on"):
    """Eine Automatisierung direkt im Speicher — ohne Umweg ueber die Sprache."""
    plan = plan or SCH.daily(20, 0)
    task = S.Task(
        task_id=task_id, owner="gregor", title="Aussenlicht abends",
        created_at=T1, created_from=AUFTRAG_SATZ, schedule=plan.as_dict(),
        action={"kind": "capability", "capability": capability,
                "arguments": dict(arguments or {"name": "Aussenlicht"})})
    await store.put_task(task)
    return task


async def _erlaubnis(preauth, task, *, capability="ha_turn_on",
                     targets=("light.aussen",), arguments=None):
    """Praegt die Bindung so, wie der Anlege-Pfad sie praegt."""
    return await preauth.mint_for_task(
        task, capability=capability, action_class=ActionClass.HA_NORMAL,
        targets=targets, arguments=dict(arguments or {"name": "Aussenlicht"}),
        created_origin="trusted_interactive_app", created_principal="gregor")


async def _abgelehnt(coro, was: str = "eine fremde Wirkung") -> str:
    """Der stabile Ablehnungsgrund — und Durchkommen ist selbst der Fehler.

    Ein `try/except`, das im Erfolgsfall stillschweigend weiterlaeuft, wuerde
    genau den Fall verschweigen, den diese Datei sucht.
    """
    durchgekommen = None
    try:
        durchgekommen = await coro
    except PA.PreauthorizationError as exc:
        return exc.reason
    require(False, f"{was} wurde autorisiert: {durchgekommen!r}")
    return ""


def _abgelehnt_sync(fn, **kwargs) -> str:
    durchgekommen = None
    try:
        durchgekommen = fn(**kwargs)
    except PA.PreauthorizationError as exc:
        return exc.reason
    require(False, f"eine fremde Wirkung wurde autorisiert: {durchgekommen!r}")
    return ""


async def _hintergrundlauf(router, task, *, capability="", arguments=None):
    """Ein Lauf so, wie `TaskRunner` ihn fuehrt — Herkunft und Bindung inklusive.

    Wichtig ist die Herkunft: dass der Mensch die Aufgabe einmal am Telefon
    angelegt hat, macht ihre Ausfuehrung heute Nacht nicht zu einer Handlung am
    Telefon.
    """
    name = capability or str((task.action or {}).get("capability", ""))
    args = (dict((task.action or {}).get("arguments") or {})
            if arguments is None else dict(arguments))
    return await router.execute(
        name, args, trust=background_trust(task.created_at),
        provenance={key: ArgumentSource.TRUSTED_CONTEXT for key in args},
        principal="proactive", origin=OriginClass.BACKGROUND_AUTOMATION,
        automation_id=task.task_id)


def _augenblicke(wert) -> list:
    """Alle Zahlen in einem Gebilde, die wie ein absoluter Zeitpunkt aussehen."""
    if isinstance(wert, dict):
        return [x for v in wert.values() for x in _augenblicke(v)]
    if isinstance(wert, (list, tuple)):
        return [x for v in wert for x in _augenblicke(v)]
    if isinstance(wert, bool):
        return []
    if isinstance(wert, (int, float)) and wert > 1_000_000_000:
        return [wert]
    return []


# =====================================================================
# 1. Der Weg, den der Nutzer bestellt hat
# =====================================================================

async def t_taegliche_hauslampe_bekommt_eine_gebundene_erlaubnis():
    """Der Anlegevorgang praegt eine Bindung — und zwar an das echte Geraet.

    Faellt das aus, kostet „jeden Abend um acht" jeden Abend eine Face-ID-Runde.
    Das ist genau die Gewoehnung ans gedankenlose Bestaetigen, die dieser
    Meilenstein abstellen sollte.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))

    require("vorab_freigabe" in ergebnis,
            "der Nutzer erfaehrt nicht, dass eine Vollmacht entstanden ist")
    grant = await store.get_preauth(ergebnis["id"])
    require(grant is not None, "es wurde keine Erlaubnis gespeichert")
    require_equal(grant.targets, ("light.aussen",),
                  "die Bindung haengt nicht am aufgeloesten Geraet")
    require_equal(grant.action_class, ActionClass.HA_NORMAL.value,
                  "eine andere Klasse als gewoehnliche Haustechnik wurde gebunden")
    require_equal(grant.revision, 1, "die erste Bindung ist nicht Revision 1")
    require(grant.active, "die frische Erlaubnis ist nicht aktiv")
    require_equal(grant.created_origin, OriginClass.TRUSTED_INTERACTIVE_APP.value,
                  "die Herkunft der Erlaubnis wurde nicht festgehalten")


async def t_die_identische_wirkung_loest_die_erlaubnis_ein():
    """Dieselbe Wirkung, dieselbe Kennung — sonst traegt keine Bindung je etwas.

    Das ist die Gegenprobe zu allen Ablehnungen weiter unten: eine Bindung, die
    auch die unveraenderte Wirkung ablehnt, waere nicht streng, sondern kaputt.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))
    grant = await store.get_preauth(ergebnis["id"])

    kennung = await preauth.verify(
        automation_id=ergebnis["id"], capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"})
    require_equal(kennung, grant.preauth_id,
                  "die unveraenderte Wirkung wurde nicht wiedererkannt")


async def t_der_hintergrundlauf_schaltet_ohne_freigabe_wenn_die_bindung_traegt():
    """Der Abend, um den es geht: das Licht geht an, ohne dass jemand wach wird.

    Gemessen am Papier-Haus, nicht am Rueckgabewert: nur ein wirklich
    abgesetzter Service-Aufruf beweist, dass die Zelle offen war.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))
    task = await store.get_task(ergebnis["id"])

    lauf = await _hintergrundlauf(router, task)
    require_equal(lauf.outcome, CapabilityOutcome.SUCCESS,
                  "der gebundene Abendlauf kam nicht durch")
    require_equal([c[:2] for c in haus.calls], [("light", "turn_on")],
                  "das Aussenlicht wurde nicht geschaltet")
    require_equal(haus.calls[0][2].get("entity_id"), "light.aussen",
                  "es wurde ein anderes Geraet geschaltet als gebunden")


async def t_die_zelle_oeffnet_sich_nur_ueber_die_vorab_autorisierung():
    """Die Entscheidung selbst — Hintergrund x gewoehnliche Haustechnik.

    Ohne Bindung ist diese Zelle `REQUIRE_FACE_ID`; erst die gepruefte Erlaubnis
    macht `EXECUTE_DIRECTLY` daraus, und der Grund muss sie benennen. Ein
    anderer Grund waere eine zweite, unbeabsichtigte Lockerung.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))
    task = await store.get_task(ergebnis["id"])
    spec = router.spec("ha_turn_on")

    mit = await router._classify_and_decide(
        spec, {"name": "Aussenlicht"}, OriginClass.BACKGROUND_AUTOMATION, None,
        task.task_id, "c-test", True)
    require_equal(mit.decision, Decision.EXECUTE_DIRECTLY,
                  "die gebundene Erlaubnis oeffnet die Zelle nicht")
    require_equal(mit.reason_code, "bounded_preauthorization",
                  "die Lockerung wurde nicht als Vorab-Autorisierung begruendet")
    require_equal(mit.preauthorization_id,
                  (await store.get_preauth(task.task_id)).preauth_id,
                  "die Entscheidung nennt eine andere Erlaubnis")

    ohne = await router._classify_and_decide(
        spec, {"name": "Aussenlicht"}, OriginClass.BACKGROUND_AUTOMATION, None,
        "", "c-test", True)
    require_equal(ohne.decision, Decision.REQUIRE_FACE_ID,
                  "der Hintergrund schaltet auch ohne jede Bindung durch")


async def t_ohne_erlaubnis_kostet_derselbe_lauf_eine_freigabe():
    """Dieselbe Aufgabe, dieselbe Nacht — nur ohne Bindung. Es passiert nichts.

    Das ist der ganze Beweis dafuer, dass die Lockerung an der Erlaubnis haengt
    und nicht daran, dass es sich um einen Zeitplan handelt.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    task = await _aufgabe(store)          # angelegt, aber nie gebunden

    lauf = await _hintergrundlauf(router, task)
    require_equal(lauf.outcome, CapabilityOutcome.APPROVAL_REQUIRED,
                  "ein ungebundener Hintergrundlauf lief ohne Freigabe")
    require_equal(haus.calls, [], "es wurde trotz fehlender Freigabe geschaltet")


# =====================================================================
# 2. Jede materielle Aenderung entwertet die Bindung
# =====================================================================

async def t_eine_andere_faehigkeit_traegt_die_bindung_nicht():
    """Aus „einschalten" wird „aufschliessen" — dieselbe Aufgabe, andere Wirkung.

    Das ist der Angriff in seiner einfachsten Form: die Automatisierung bleibt
    stehen, nur die Faehigkeit wird ausgetauscht.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_unlock",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "capability_mismatch",
                  "eine ausgetauschte Faehigkeit wurde nicht als solche benannt")


async def t_ein_anderes_zielgeraet_traegt_die_bindung_nicht():
    """Dieselbe Faehigkeit, ein anderes aufgeloestes Geraet.

    Gebunden ist die `entity_id`, nicht das Wort im Satz. Wer die Lampe
    umbenennt oder ein zweites Geraet gleich nennt, bekommt keine Erlaubnis
    geschenkt.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.wohnzimmer",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "binding_mismatch",
                  "ein fremdes Geraet wurde unter der alten Bindung geschaltet")


async def t_andere_argumente_tragen_die_bindung_nicht():
    """80 Prozent Helligkeit sind nicht 40 Prozent.

    Die Argumente stehen mit im Digest, weil sonst „Licht dimmen" zu „Licht auf
    voller Staerke um drei Uhr nachts" werden koennte, ohne dass irgendetwas
    nachgefragt wird.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store, capability="ha_set_brightness",
                          arguments={"name": "Aussenlicht", "brightness_pct": 80})
    await _erlaubnis(preauth, task, capability="ha_set_brightness",
                     arguments={"name": "Aussenlicht", "brightness_pct": 80})

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_set_brightness",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht", "brightness_pct": 40}))
    require_equal(grund, "binding_mismatch",
                  "ein veraenderter Argumentwert lief unter der alten Bindung")


async def t_eine_verschobene_uhrzeit_traegt_die_bindung_nicht():
    """Aus 20:00 wird 21:00. Der Mensch hat die Regel geaendert, also faellt sie.

    Eine Erlaubnis, die jede Uhrzeit mittraegt, waere eine Erlaubnis fuer „immer".
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    task.schedule = SCH.daily(21, 0).as_dict()
    await store.put_task(task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "binding_mismatch",
                  "die verschobene Uhrzeit lief unter der alten Bindung")


async def t_aus_taeglich_wird_woechentlich_und_die_bindung_faellt():
    """Dieselbe Uhrzeit, eine andere Zeitplanart.

    „Taeglich um 20:00" und „montags um 20:00" sind zwei verschiedene Zusagen;
    die Bindung muss die Art mitfuehren, nicht nur die Uhrzeit.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    task.schedule = SCH.weekly((0,), 20, 0).as_dict()
    await store.put_task(task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "binding_mismatch",
                  "ein Wechsel der Zeitplanart lief unter der alten Bindung")


async def t_ein_geaenderter_abstand_traegt_die_bindung_nicht():
    """Alle 15 Minuten ist nicht alle 30 Minuten.

    Bei Intervallen ist der Abstand die ganze Regel — wer ihn nicht bindet,
    bindet nichts.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store, plan=SCH.every(900))
    await _erlaubnis(preauth, task)

    task.schedule = SCH.every(1800).as_dict()
    await store.put_task(task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "binding_mismatch",
                  "ein veraenderter Intervallabstand lief unter der alten Bindung")


async def t_ein_zusaetzliches_zielgeraet_traegt_die_bindung_nicht():
    """Das gebundene Geraet ist noch dabei — und trotzdem faellt die Erlaubnis.

    Die Bindung gilt der ganzen Zielmenge. Sonst waere „auch noch das hier"
    der bequemste Weg, eine enge Erlaubnis auszuweiten.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL,
        targets=("light.aussen", "light.wohnzimmer"),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "binding_mismatch",
                  "ein zusaetzliches Geraet lief unter der alten Bindung mit")


async def t_ein_umklassifiziertes_geraet_traegt_die_bindung_nicht():
    """Aus der Lampe wurde ein Zutrittsgeraet — die Erlaubnis endet hier.

    Der Fall ist nicht theoretisch: derselbe Schalter kann nach einer Aenderung
    in Home Assistant oder nach einer Angabe des Besitzers ein Tor bedienen.
    Die Klasse wird bei jedem Lauf neu am Geraet gemessen, und eine gestiegene
    Klasse muss die alte Bindung entwerten.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_SECURITY, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "effect_reclassified",
                  "eine gestiegene Geraeteklasse lief unter der alten Bindung")


async def t_eine_pausierte_automatisierung_traegt_keine_erlaubnis():
    """Wer die Aufgabe anhaelt, haelt auch ihre Autoritaet an.

    Pausieren ist fuer den Nutzer die naheliegende Notbremse. Wenn sie die
    Erlaubnis nicht mitnimmt, hat er eine Bremse, die nichts bremst.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    task.state = S.PAUSED
    task.enabled = False
    await store.put_task(task)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "automation_disabled",
                  "eine ruhende Automatisierung trug weiter eine Vollmacht")


async def t_eine_unbekannte_automatisierung_traegt_keine_erlaubnis():
    """Eine frei erfundene Kennung darf nichts oeffnen.

    Die Kennung kommt aus dem Lauf und ist selbst keine Autoritaet; sie sagt nur,
    WELCHE Bindung nachzurechnen ist. Gibt es die Aufgabe nicht, gibt es nichts.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)

    grund = await _abgelehnt(preauth.verify(
        automation_id="bt-gibt-es-nicht", capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "automation_unknown",
                  "eine erfundene Automatisierungskennung wurde akzeptiert")


async def t_eine_widerrufene_erlaubnis_traegt_nichts_mehr():
    """Ein Widerruf ist endgueltig fuer diese Bindung — auch bei laufender Aufgabe.

    Die Aufgabe darf weiterlaufen; sie fragt dann eben wieder nach. Ein Widerruf,
    den man durch blosses Weiterlaufen aushebeln kann, ist keiner.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    require(await preauth.revoke(task.task_id, "vom nutzer zurueckgenommen"),
            "der Widerruf hat keine Zeile veraendert")

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "preauthorization_revoked",
                  "eine widerrufene Erlaubnis trug weiter")


async def t_der_nutzer_nimmt_die_vorab_freigabe_zurueck_und_die_aufgabe_bleibt():
    """„Mach das weiter, aber frag mich" — ohne die Automatisierung zu loeschen.

    Der Widerruf muss ohne Umweg erreichbar sein, sonst bleibt dem Nutzer nur
    das Loeschen. Danach laeuft dieselbe Aufgabe weiter und kostet wieder eine
    Freigabe; das Papier-Haus bleibt unberuehrt.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))
    task = await store.get_task(ergebnis["id"])

    zurueck = await caps.require_approval({"id": task.task_id})
    require(zurueck.get("widerrufen"), "der Widerruf blieb wirkungslos")
    require(task.running, "die Aufgabe wurde durch den Widerruf angehalten")

    lauf = await _hintergrundlauf(router, task)
    require_equal(lauf.outcome, CapabilityOutcome.APPROVAL_REQUIRED,
                  "der Lauf schaltete trotz zurueckgenommener Vorab-Freigabe")
    require_equal(haus.calls, [], "nach dem Widerruf wurde weiter geschaltet")


async def t_ohne_gepraegte_erlaubnis_gibt_es_keine():
    """Eine laufende Automatisierung ohne Bindung autorisiert nichts.

    „Die Aufgabe existierte" hat noch nie etwas autorisiert — das ist der
    Unterschied zwischen einer gebundenen Erlaubnis und einer Vollmacht auf
    einen Zeitplan.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)

    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "no_preauthorization",
                  "eine Automatisierung ohne Bindung wurde autorisiert")


# =====================================================================
# 3. Der Satz, an dem alles haengt
# =====================================================================

async def t_aussenlicht_um_acht_autorisiert_niemals_die_haustuer():
    """Die Kernaussage — durch die echte Pruefung, nicht durch Feldvergleich.

    Drei Wege fuehren vom „Aussenlicht an um 20 Uhr" zum „Haustuer auf um
    20 Uhr", und alle drei muessen an derselben Bindung enden:

    * andere Faehigkeit bei gleichem Zeitplan,
    * gleiche Faehigkeit auf dem Schloss, ehrlich als sicherheitsrelevant
      gemessen,
    * gleiche Faehigkeit auf dem Schloss, wobei die Klasse als gewoehnlich
      BEHAUPTET wird — der Fall, in dem ein Klassifizierer versagt haette.

    Der dritte ist der wichtige: er zeigt, dass die Bindung auch dann traegt,
    wenn die Klassenmessung nicht mehr traegt.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    require_equal(await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_unlock",
        action_class=ActionClass.HA_NORMAL, targets=("lock.haustuer",),
        arguments={"name": "Haustuer"})), "capability_mismatch",
        "eine Aufschliess-Faehigkeit lief unter der Lampen-Bindung")

    require_equal(await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_SECURITY, targets=("lock.haustuer",),
        arguments={"name": "Haustuer"})), "effect_reclassified",
        "das Schloss lief unter der Lampen-Bindung")

    require_equal(await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("lock.haustuer",),
        arguments={"name": "Haustuer"})), "binding_mismatch",
        "eine als harmlos behauptete Haustuer lief unter der Lampen-Bindung")


async def t_der_naechtliche_lauf_erreicht_die_haustuer_nicht():
    """Derselbe Satz, aber vollstaendig durch den Router — bis ins Papier-Haus.

    Die Erlaubnis fuer die Lampe ist echt und gilt; nur das Ziel des Laufs ist
    ein anderes. Am Ende darf Home Assistant keinen einzigen Aufruf gesehen
    haben.
    """
    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))
    task = await store.get_task(ergebnis["id"])

    lauf = await _hintergrundlauf(router, task, arguments={"name": "Haustuer"})
    require_equal(lauf.outcome, CapabilityOutcome.REJECTED_BY_POLICY,
                  "die Haustuer wurde nicht abgelehnt")
    require_equal(lauf.reason, "not_executable",
                  "die Ablehnung nennt nicht die Sicherheitswirkung des Geraets")
    require_equal(haus.calls, [],
                  "am Schloss wurde ein Service-Aufruf abgesetzt")


async def t_die_umgewidmete_lampe_faellt_aus_der_direktausfuehrung():
    """Der Besitzer erklaert das Aussenlicht zum Zutrittsgeraet — mitten im Betrieb.

    Die Bindung wurde gepraegt, als es eine Lampe war. Ab der Erklaerung misst
    der Klassifizierer `ha_security`, und der naechtliche Lauf endet, bevor
    irgendetwas geschaltet wird.
    """
    store = _speicher()
    haus, router, gate, _, preauth, caps = _stapel(store=store)
    _turn(gate)
    ergebnis = await caps.create(dict(AUFTRAG))
    task = await store.get_task(ergebnis["id"])

    # Dasselbe Haus, dieselbe Aufgabe, dieselbe Erlaubnis — nur sagt der
    # Besitzer jetzt, was dieses Geraet wirklich bedient.
    haus2, router2, _, _, _, _ = _stapel(store=store,
                                         security_entities={"light.aussen"})
    lauf = await _hintergrundlauf(router2, task)
    require_equal(lauf.outcome, CapabilityOutcome.REJECTED_BY_POLICY,
                  "das umgewidmete Geraet lief unter der alten Bindung weiter")
    require_equal(haus2.calls, [], "das umgewidmete Geraet wurde geschaltet")


# =====================================================================
# 4. Was gar nicht erst gepraegt werden darf
# =====================================================================

def t_nur_gewoehnliche_haustechnik_bekommt_ueberhaupt_eine_erlaubnis():
    """Jede andere Klasse scheitert beim Praegen, nicht erst beim Pruefen.

    Die Pruefung steht an der Stelle, die kein Aufrufer umgehen kann. Ein
    Schloss, ein Geldweg oder eine unklassifizierte Handlung kann damit gar
    keine dauerhafte Erlaubnis erzeugen — auch nicht durch einen Fehler weiter
    oben im Anlegepfad.
    """
    for klasse in ActionClass:
        if klasse is ActionClass.HA_NORMAL:
            continue
        grund = _abgelehnt_sync(
            PA.mint, automation_id="bt-x", capability="ha_turn_on",
            action_class=klasse, targets=("light.aussen",),
            arguments={}, schedule={"art": "daily", "uhrzeit": "20:00"},
            created_origin="trusted_interactive_app", created_principal="gregor")
        require_equal(grund, "class_not_eligible",
                      f"die Klasse {klasse.value} konnte eine Erlaubnis praegen")


def t_ohne_aufgeloestes_ziel_entsteht_keine_blankovollmacht():
    """Eine Erlaubnis ohne Zielgeraet waere die Vollmacht, die es nicht geben soll.

    Sie wuerde auf jedes Geraet passen, dessen Aufloesung spaeter leer bliebe —
    also genau dann, wenn niemand mehr weiss, was geschaltet wird.
    """
    for ziele in ((), None, ("   ",), ["", "  "]):
        grund = _abgelehnt_sync(
            PA.mint, automation_id="bt-x", capability="ha_turn_on",
            action_class=ActionClass.HA_NORMAL, targets=ziele,
            arguments={}, schedule={"art": "daily", "uhrzeit": "20:00"},
            created_origin="trusted_interactive_app", created_principal="gregor")
        require_equal(grund, "no_resolved_target",
                      f"die Zielmenge {ziele!r} ergab eine Erlaubnis")


# =====================================================================
# 5. Die Zeitplanbindung kennt keinen Augenblick
# =====================================================================

async def t_dieselbe_regel_bindet_zu_jeder_uhrzeit_gleich():
    """Die Lehre, an der ein frueherer Freigabe-Beschreiber gestorben ist.

    Dessen erster Entwurf band den AUSGERECHNETEN naechsten Augenblick; bei der
    Fortsetzung war es ein anderer, und der Lauf endete an `approval_drift`. Der
    Schutz hatte recht, die Beschreibung war falsch gebaut.

    Gebunden wird deshalb die REGEL. Hier wird dieselbe Regel an zwei
    verschiedenen Wanduhr-Momenten angelegt: die Aufgaben unterscheiden sich in
    ihrem naechsten Lauf, ihre Bindung darf sich nicht unterscheiden.
    """
    _, _, gate_a, store_a, _, caps_a = _stapel(clock=lambda: T1)
    _turn(gate_a)
    a = await store_a.get_task((await caps_a.create(dict(AUFTRAG)))["id"])

    _, _, gate_b, store_b, _, caps_b = _stapel(clock=lambda: T2)
    _turn(gate_b)
    b = await store_b.get_task((await caps_b.create(dict(AUFTRAG)))["id"])

    require(a.next_run_at != b.next_run_at,
            "die beiden Anlaeufe lagen gar nicht an verschiedenen Momenten")
    require_equal(PA.schedule_binding(a.schedule), PA.schedule_binding(b.schedule),
                  "dieselbe Regel ergab zwei verschiedene Bindungen")

    fest = dict(automation_id="bt-fest", capability="ha_turn_on",
                action_class="ha_normal", targets=("light.aussen",),
                arguments={"name": "Aussenlicht"})
    require_equal(
        PA.preauth_digest(schedule=PA.schedule_binding(a.schedule), **fest),
        PA.preauth_digest(schedule=PA.schedule_binding(b.schedule), **fest),
        "der Digest derselben Regel driftet mit der Wanduhr")


def t_die_zeitplanbindung_enthaelt_keinen_absoluten_zeitpunkt():
    """Kein Feld der Bindung darf wie ein Zeitstempel aussehen.

    Der einmalige Termin traegt in `as_dict()` ausdruecklich einen absoluten
    `zeitpunkt`; die Bindung muss ihn fallen lassen. Sonst waere jede Bindung
    nach genau einem Augenblick wertlos — und das faellt erst im Betrieb auf.
    """
    plaene = [SCH.daily(20, 0).as_dict(), SCH.weekly((0, 3), 7, 30).as_dict(),
              SCH.every(900).as_dict(), SCH.in_seconds(120, now=T1).as_dict()]
    for plan in plaene:
        bindung = PA.schedule_binding(plan)
        require_equal(_augenblicke(bindung), [],
                      f"die Bindung von {plan!r} traegt einen absoluten Zeitpunkt")
        require("zeitpunkt" not in bindung,
                f"der ausgerechnete Augenblick steht in der Bindung von {plan!r}")

    einmalig_frueh = PA.schedule_binding(SCH.in_seconds(120, now=T1).as_dict())
    einmalig_spaet = PA.schedule_binding(SCH.in_seconds(120, now=T2).as_dict())
    require_equal(einmalig_frueh, einmalig_spaet,
                  "derselbe einmalige Termin bindet zu zwei Zeiten verschieden")


# =====================================================================
# 6. Dauerhaftigkeit
# =====================================================================

async def t_die_erlaubnis_ueberlebt_einen_neustart():
    """Ein Neustart darf die Bequemlichkeit nicht kosten.

    Waere sie nach jedem Neustart weg, wuerde der Nutzer sie neu erteilen — und
    genau daran gewoehnt man sich das Hinsehen ab.
    """
    pfad = os.path.join(tempfile.mkdtemp(prefix="solvio-preauth-"), "p.sqlite3")
    store = _speicher(pfad)
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    grant = await _erlaubnis(preauth, task)

    wieder = _speicher(pfad)
    gelesen = await wieder.get_preauth(task.task_id)
    require(gelesen is not None, "die Erlaubnis war nach dem Neustart weg")
    require_equal(gelesen.digest, grant.digest,
                  "die Bindung hat sich beim Wiederlesen veraendert")
    require_equal(gelesen.targets, grant.targets,
                  "die Zielmenge hat sich beim Wiederlesen veraendert")
    require_equal(gelesen.revision, grant.revision,
                  "die Revision hat sich beim Wiederlesen veraendert")

    kennung = await PA.AutomationPreauthorizations(wieder).verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"})
    require_equal(kennung, grant.preauth_id,
                  "die wiedergelesene Erlaubnis traegt ihre eigene Wirkung nicht")


async def t_ein_widerruf_ueberlebt_einen_neustart():
    """Ein Widerruf, den ein Neustart aufhebt, ist keiner.

    Das ist die gefaehrlichere Haelfte der Dauerhaftigkeit: der Nutzer glaubt,
    zurueckgenommen zu haben, und der naechste Prozess weiss nichts davon.
    """
    pfad = os.path.join(tempfile.mkdtemp(prefix="solvio-preauth-"), "p.sqlite3")
    store = _speicher(pfad)
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)
    await preauth.revoke(task.task_id, "vom nutzer zurueckgenommen")

    wieder = _speicher(pfad)
    gelesen = await wieder.get_preauth(task.task_id)
    require(gelesen is not None and not gelesen.active,
            "der Widerruf hat den Neustart nicht ueberlebt")

    grund = await _abgelehnt(PA.AutomationPreauthorizations(wieder).verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "preauthorization_revoked",
                  "die widerrufene Erlaubnis lebte nach dem Neustart wieder auf")


async def t_eine_geloeschte_automatisierung_vererbt_keine_vollmacht():
    """Wer die Automatisierung loescht, loescht ihre Autoritaet mit.

    Der Speicher sagt das ausdruecklich zu (`ON DELETE CASCADE`, mit der
    Begruendung, ein verwaistes Recht solle gar nicht erst entstehen koennen).
    Diese Zusage traegt: eine spaetere Aufgabe unter derselben Kennung darf
    nichts erben, was jemand fuer eine ganz andere Automatisierung erteilt hat.

    Solange die Zeile ueberlebt, haengt die Sicherheit allein daran, dass
    ausgerechnet der Loeschpfad vorher widerruft — und eine Loeschung, die an
    ihrer eigenen Reihenfolge haengt, ist keine Loeschung.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    await _erlaubnis(preauth, task)

    require(await store.delete_task(task.task_id), "die Aufgabe wurde nicht geloescht")

    # Dieselbe Kennung, eine neue Aufgabe — nie gebunden. Sie faengt bei null an.
    neu = await _aufgabe(store)
    grund = await _abgelehnt(preauth.verify(
        automation_id=neu.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}),
        "die nie gebundene Nachfolgerin einer geloeschten Automatisierung")
    require_equal(grund, "no_preauthorization",
                  "eine neue Automatisierung erbte die Vollmacht ihrer Vorgaengerin")
    require_equal(await store.get_preauth(neu.task_id), None,
                  "die Vollmacht der geloeschten Automatisierung liegt noch im Speicher")


# =====================================================================
# 7. Wer ueberhaupt praegen darf
# =====================================================================

async def t_der_hintergrund_stellt_sich_keine_vollmacht_aus():
    """Ein Hintergrundlauf kann sich keine Autoritaet fuer die naechste Nacht geben.

    Sonst genuegte eine einzige Automatisierung, die weitere Automatisierungen
    anlegt, um aus einer engen Erlaubnis eine wachsende Vollmacht zu machen.
    Die Sperre ist strukturell: die Herkunft steht schlicht nicht in der Menge
    der Praegeberechtigten.
    """
    require(OriginClass.BACKGROUND_AUTOMATION.value not in PRO.MAY_GRANT_PREAUTH,
            "der Hintergrund steht in der Menge der Praegeberechtigten")

    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate, origin=OriginClass.BACKGROUND_AUTOMATION)
    ergebnis = await caps.create(dict(AUFTRAG))

    require("vorab_freigabe" not in ergebnis,
            "der Hintergrund hat sich eine Vollmacht ausgestellt")
    require_equal(await store.get_preauth(ergebnis["id"]), None,
                  "eine aus dem Hintergrund gepraegte Erlaubnis liegt im Speicher")


async def t_fremder_inhalt_und_fehlender_kontext_praegen_nichts():
    """Zwei Herkuenfte, denen keine Erlaubnis entstehen darf.

    Fremder Inhalt scheitert im Betrieb schon an der Autoritaetspruefung; hier
    wird die zweite Sperre gemessen, damit sie nicht unbemerkt wegfaellt. Und
    ein Aufruf OHNE Turn-Kontext ist der Fall, in dem niemand sagen kann, wer
    gefragt hat — dann entsteht nichts.
    """
    require(OriginClass.EXTERNAL_UNTRUSTED.value not in PRO.MAY_GRANT_PREAUTH,
            "fremder Inhalt steht in der Menge der Praegeberechtigten")
    require(OriginClass.UNSPECIFIED.value not in PRO.MAY_GRANT_PREAUTH,
            "eine unbekannte Herkunft darf keine Erlaubnis praegen")

    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate, origin=OriginClass.EXTERNAL_UNTRUSTED)
    fremd = await caps.create(dict(AUFTRAG))
    require_equal(await store.get_preauth(fremd["id"]), None,
                  "fremder Inhalt hat eine Erlaubnis gepraegt")

    gate.clear()
    ohne = await caps.create(dict(AUFTRAG, titel="Aussenlicht abends zwei"))
    require_equal(await store.get_preauth(ohne["id"]), None,
                  "ein Aufruf ohne Turn-Kontext hat eine Erlaubnis gepraegt")


async def t_ein_einmaliger_termin_bekommt_keine_erlaubnis():
    """Nur wiederkehrende Regeln tragen eine Bindung.

    Ein Termin, der genau einmal laeuft, spart keine Wiederholung ein — er
    wuerde nur eine dauerhafte Zeile hinterlassen, die niemand mehr ansieht.
    """
    require("one_shot" not in PA.RECURRING_KINDS,
            "der einmalige Termin gilt als wiederkehrend")

    haus, router, gate, store, preauth, caps = _stapel()
    _turn(gate, said="Mach in zwanzig Minuten das Aussenlicht an.")
    ergebnis = await caps.create(dict(AUFTRAG, wann="in 20 minuten"))

    task = await store.get_task(ergebnis["id"])
    require_equal(task.schedule.get("art"), "one_shot",
                  "der Zeitplan wurde nicht als einmalig verstanden")
    require("vorab_freigabe" not in ergebnis,
            "ein einmaliger Termin hat eine Vollmacht bekommen")
    require_equal(await store.get_preauth(ergebnis["id"]), None,
                  "fuer einen einmaligen Termin liegt eine Erlaubnis im Speicher")


# =====================================================================
# 8. Erneuern heisst ersetzen
# =====================================================================

async def t_eine_neue_bindung_zaehlt_hoch_und_entwertet_die_alte():
    """Je Automatisierung gibt es genau EINE gueltige Bindung.

    Wuerden Erlaubnisse sich ansammeln, waere die Summe der alten Bindungen
    breiter als jede einzelne davon — und niemand koennte mehr sagen, was
    gerade erlaubt ist. Die neue Revision ersetzt, sie ergaenzt nicht.
    """
    store = _speicher()
    preauth = PA.AutomationPreauthorizations(store)
    task = await _aufgabe(store)
    alt = await _erlaubnis(preauth, task)

    neu = await _erlaubnis(preauth, task, targets=("light.wohnzimmer",),
                           arguments={"name": "Wohnzimmerlicht"})
    require_equal(neu.revision, 2, "die erneute Bindung zaehlt nicht hoch")
    require(neu.digest != alt.digest, "die erneute Bindung hat denselben Digest")

    gespeichert = await store.get_preauth(task.task_id)
    require_equal(gespeichert.digest, neu.digest,
                  "gespeichert ist nicht die juengste Bindung")

    # Die alte Wirkung traegt nicht mehr — obwohl sie einmal erlaubt war.
    grund = await _abgelehnt(preauth.verify(
        automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.aussen",),
        arguments={"name": "Aussenlicht"}))
    require_equal(grund, "binding_mismatch",
                  "die abgeloeste Bindung trug weiter")

    # Und die alte Zeile selbst traegt auch die neue Wirkung nicht.
    require_equal(_abgelehnt_sync(
        PA.verify, stored=alt, automation_id=task.task_id, capability="ha_turn_on",
        action_class=ActionClass.HA_NORMAL, targets=("light.wohnzimmer",),
        arguments={"name": "Wohnzimmerlicht"},
        schedule=PA.schedule_binding(task.schedule)), "binding_mismatch",
        "die alte Zeile autorisierte die neue Wirkung")



async def t_eine_schaltbare_sicherheitsanlage_bekommt_keine_vollmacht():
    """Tiefenverteidigung an der Praegung, nicht nur an der Pruefung.

    Aus einem Mutationslauf: die Klassenpruefung in `_grant_preauth` liess sich
    entfernen, ohne dass etwas fehlschlug — weil `mint()` dahinter dieselbe
    Bedingung noch einmal stellt. Genau das ist gewollt, aber es muss auch
    gezeigt sein: fiele eine der beiden Bedingungen einmal weg, soll die andere
    sichtbar tragen, statt still zu ueberleben.
    """
    for klasse in (ActionClass.HA_SECURITY, ActionClass.NORMAL_WRITE,
                   ActionClass.CRITICAL, ActionClass.VERY_CRITICAL,
                   ActionClass.UNCLASSIFIED, ActionClass.READ_ONLY):
        try:
            PA.mint(automation_id="t-x", capability="ha_unlock",
                    action_class=klasse, targets=("lock.haustuer",),
                    arguments={"name": "Haustuer"},
                    schedule={"art": "daily", "uhrzeit": "20:00"},
                    created_origin="room_voice", created_principal="pi")
        except PA.PreauthorizationError as exc:
            require_equal(exc.reason, "class_not_eligible",
                          f"{klasse.value}: falscher Grund")
        else:
            require(False, f"{klasse.value} bekam eine Vorab-Autorisierung")



if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
