"""Gemeinsames Werkzeug fuer die Zahlungs-Suiten — ein ECHTER Anbieter je Fall.

Warum ein laufender Dienst und kein Attrappenobjekt: die Fragen, die diese
Suiten beantworten muessen, entstehen ZWISCHEN zwei Prozessen. Eine Buchung,
deren Antwort nie ankommt. Eine Idempotenzkennung, die den zweiten Versuch auf
den ersten Vorgang zeigen laesst. Ein Anbieter, der eine Bestaetigung der Bank
verlangt. Eine Attrappe kann keinen dieser Faelle beweisen, weil sie sie nur
behauptet.

Jeder Fall bekommt: eigenen Tresor, eigenen Hauptschluessel, eigene
Zahlungsablage, eigenen Anbieter auf einem eigenen Port. Nichts davon beruehrt
den produktiven Stand.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import secrets
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions  # noqa: E402

enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-payment-suite-")
os.environ.setdefault("SOLVIO_VAULT_DIR", os.path.join(_SANDBOX, "vault"))
os.environ.setdefault("SOLVIO_VAULT_TEST_KEYSTORE", os.path.join(_SANDBOX, "keys"))
os.environ.setdefault("SOLVIO_PAYMENT_DB", os.path.join(_SANDBOX, "payments.sqlite3"))
os.environ.setdefault("SOLVIO_PAYMENT_CONFIG", os.path.join(_SANDBOX, "payment.json"))
atexit.register(shutil.rmtree, _SANDBOX, True)

import payment_sandbox as SBX                                       # noqa: E402
from solvio.capabilities import execution_identity as EI            # noqa: E402
from solvio.capabilities import policy as AP                        # noqa: E402
from solvio.capabilities.payment import PaymentCapabilitiesFull     # noqa: E402
from solvio.payment.executor import PaymentExecutor                 # noqa: E402
from solvio.payment.instruments import Instrument, InstrumentKind   # noqa: E402
from solvio.payment.store import PaymentStore                       # noqa: E402
from solvio.secret_vault import admin as VA                         # noqa: E402
from solvio.secret_vault import context as SC                       # noqa: E402
from solvio.secret_vault import keyring as K                        # noqa: E402
from solvio.secret_vault import policy as VP                        # noqa: E402
from solvio.secret_vault.broker import SecretBroker                 # noqa: E402
from solvio.secret_vault.store import VaultStore                    # noqa: E402
from solvio.security.mobile_approval.execution import (              # noqa: E402
    execution_id_for, idempotency_key_for)

CHARGE_REF = "secret://payment-provider/sandbox-charge"
REFUND_REF = "secret://payment-provider/sandbox-refund"
READ_REF = "secret://payment-provider/sandbox-read"
METHOD = "payment://shopping/default"
MERCHANT = "sandbox-shop"


class Rig:
    """Ein vollstaendiger Zahlungsaufbau fuer EINEN Testfall."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="solvio-payment-case-", dir=_SANDBOX)
        self.runner = None
        self.base_url = ""
        self.admin_secret = "admin-" + secrets.token_hex(8)
        self.provider_secret = "charge-" + secrets.token_hex(8)
        # Drei WIRKLICH verschiedene Werte. Legte der Aufbau sie auf denselben,
        # bewiese kein Test, dass die Trennung traegt.
        self.refund_secret = "refund-" + secrets.token_hex(8)
        self.read_secret = "read-" + secrets.token_hex(8)
        self.store: PaymentStore | None = None
        self.caps: PaymentCapabilitiesFull | None = None
        self.vault: VaultStore | None = None

    async def start(self, *, instrument: Instrument | None = None,
                    charge_capabilities=("purchase_place",),
                    refund_capabilities=("refund_request", "purchase_cancel"),
                    read_capabilities=("payment_intent_prepare", "payment_reconcile",
                                       "payment_health"),
                    allow_background: bool = False,
                    requires_user_presence: bool = True) -> "Rig":
        from aiohttp import web

        os.environ["SOLVIO_VAULT_DIR"] = os.path.join(self.root, "vault")
        os.environ["SOLVIO_VAULT_TEST_KEYSTORE"] = os.path.join(self.root, "keys")
        os.environ["SOLVIO_PAYMENT_DB"] = os.path.join(self.root, "payments.sqlite3")
        os.environ["SOLVIO_PAYMENT_CONFIG"] = os.path.join(self.root, "payment.json")
        K.forget_kek()
        K.initialize_kek(allow_overwrite=True)

        app = SBX.build_app(provider_secret=self.provider_secret,
                            admin_secret=self.admin_secret,
                            refund_secret=self.refund_secret,
                            read_secret=self.read_secret)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"
        with open(os.environ["SOLVIO_PAYMENT_CONFIG"], "w", encoding="utf-8") as fh:
            json.dump({"providers": {"sandbox": {"kind": "sandbox",
                                                 "base_url": self.base_url}}}, fh)

        self.vault = VaultStore()
        VA.add(secret_ref=CHARGE_REF, kind=VP.SecretKind.API_KEY,
               plaintext=self.provider_secret.encode("utf-8"),
               allowed_capabilities=charge_capabilities,
               allowed_targets=(self.base_url,),
               allowed_executors=(VP.ExecutorId.PAYMENT,),
               display_name="Pruefanbieter — Belastung",
               allow_background=allow_background,
               requires_user_presence=requires_user_presence, store=self.vault)
        VA.add(secret_ref=REFUND_REF, kind=VP.SecretKind.API_KEY,
               plaintext=self.refund_secret.encode("utf-8"),
               allowed_capabilities=refund_capabilities,
               allowed_targets=(self.base_url,),
               allowed_executors=(VP.ExecutorId.PAYMENT,),
               display_name="Pruefanbieter — zurueck",
               allow_background=allow_background,
               requires_user_presence=False, store=self.vault)
        VA.add(secret_ref=READ_REF, kind=VP.SecretKind.API_KEY,
               plaintext=self.read_secret.encode("utf-8"),
               allowed_capabilities=read_capabilities,
               allowed_targets=(self.base_url,),
               allowed_executors=(VP.ExecutorId.PAYMENT,),
               display_name="Pruefanbieter — lesend",
               allow_background=allow_background,
               requires_user_presence=False, store=self.vault)

        self.store = PaymentStore()
        self.store.put_instrument(instrument or default_instrument())
        self.caps = PaymentCapabilitiesFull(
            self.store,
            executor=PaymentExecutor(self.store, broker=SecretBroker(self.vault)))
        return self

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    # -- Anbieter steuern (ueber den VERWALTUNGSweg, nie ueber den Zahlweg) ---
    async def scenario(self, name: str) -> None:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(self.base_url + "/admin/scenario",
                                    json={"scenario": name},
                                    headers={"X-Sandbox-Admin": self.admin_secret}) as r:
                assert r.status == 200, await r.text()

    async def stats(self) -> dict:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(self.base_url + "/admin/stats",
                                   headers={"X-Sandbox-Admin": self.admin_secret}) as r:
                return await r.json()

    async def complete_sca(self, idempotency_key: str) -> dict:
        """Der Mensch bestaetigt in seiner Bank-App. Kein Umgehungsweg."""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(self.base_url + "/v1/sca/complete",
                                    json={"idempotency_key": idempotency_key},
                                    headers={"X-Sandbox-Admin": self.admin_secret}) as r:
                return await r.json()

    # -- Bequemlichkeiten -----------------------------------------------------
    def charged_rows(self, payment_intent_id: str = "") -> list[dict]:
        rows = self.store.ledger(payment_intent_id=payment_intent_id, limit=500)
        return [r for r in rows if r["event"] == "charged"]


def default_instrument(**overrides) -> Instrument:
    fields = dict(payment_ref=METHOD, kind=InstrumentKind.VIRTUAL_CARD,
                  provider="sandbox", max_single_minor=15000,
                  daily_total_minor=30000, allowed_currencies=("EUR",),
                  allowed_merchant_ids=(MERCHANT,),
                  provider_secret_ref=CHARGE_REF, provider_readonly_ref=READ_REF,
                  provider_refund_ref=REFUND_REF,
                  provider_token="pm_sandbox_solvio_shopping",
                  display_name="SOLVIO Shopping", display_hint="•••• 4242")
    fields.update(overrides)
    return Instrument(**fields)


def use_context(origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP, *,
                capability: str = "payment_intent_prepare",
                user_present: bool = False, approval_id: str = "",
                automation_id: str = ""):
    return SC.bound(SC.UseContext(origin=origin, capability=capability,
                                  user_present=user_present,
                                  approval_id=approval_id,
                                  automation_id=automation_id))


def identity(approval_id: str = "ap-1", capability: str = "purchase_place"):
    """Die Ausfuehrungsidentitaet, wie der eingefrorene Pfad sie ableitet."""
    execution_id = execution_id_for("core-test-instance", approval_id)
    return EI.ExecutionIdentity(
        execution_id=execution_id,
        idempotency_key=idempotency_key_for(execution_id, capability),
        approval_id=approval_id, capability=capability,
        semantics="NON_IDEMPOTENT_WRITE")


async def prepare_intent(rig: Rig, *, posten: str = "MagSafe Stativ|1|8499",
                         erwartet: str = "8499", waehrung: str = "EUR",
                         haendler: str = MERCHANT,
                         zahlungsmittel: str = METHOD,
                         origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP) -> dict:
    with use_context(origin, capability="payment_intent_prepare"):
        return await rig.caps.prepare({
            "zahlungsmittel": zahlungsmittel, "haendler": haendler,
            "zweck": "MagSafe Stativ", "posten": posten, "waehrung": waehrung,
            "erwarteter_betrag": erwartet})


async def pay(rig: Rig, view: dict, *, approval_id: str = "ap-1",
              origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP) -> dict:
    """Der Weg, den der freigegebene Kauf nimmt — mit Identitaet und Praesenz."""
    with use_context(origin, capability="purchase_place", user_present=True,
                     approval_id=approval_id), EI.bound(identity(approval_id)):
        return await rig.caps.purchase({"vorgang": view["payment_intent_id"],
                                        "pruefsumme": view["pruefsumme"]})


def run(coro):
    return asyncio.run(coro)


# =====================================================================
# Ein Freigabepfad aus Papier — mit denselben Regeln wie der echte
# =====================================================================
#
# Nachgebaut wird nur, was der Router und der Zahlungsteil wirklich beruehren:
# eine durable Anfrage, ihr Digest, die Einmaligkeit der Bestaetigung und die
# Nutzlast, die der eingefrorene Pfad an den Adapter reicht — einschliesslich
# `execution_id` und `idempotency_key`. Genau die zwei Werte sind es, an denen
# die Doppelbuchungsfrage haengt; ein Papier-Pfad ohne sie prueft an der Sache
# vorbei.

from solvio.capabilities.approval_gateway import CapabilityApprovals    # noqa: E402
from solvio.capabilities.payment import register as register_payment    # noqa: E402
from solvio.capabilities.router import CapabilityRouter                 # noqa: E402
from solvio.security.approval import action_digest                      # noqa: E402
from solvio.security.mobile_approval import execution as X              # noqa: E402
from solvio.security.mobile_approval import store as S                  # noqa: E402


class FakeStore:
    def __init__(self) -> None:
        self.requests: dict[str, dict] = {}

    async def get_request(self, approval_id):
        return self.requests.get(approval_id)

    async def list_pending(self):
        return [r for r in self.requests.values() if r["state"] == S.PENDING]

    async def transition(self, approval_id, state, error=""):
        request = self.requests.get(approval_id)
        if request is None or request["state"] != S.PENDING:
            raise S.IllegalTransition(f"{approval_id} is not pending")
        request["state"] = state
        request["error"] = error


class FakeControlPlane:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.core_instance_id = "core-test"
        self._counter = 0

    async def create_request(self, *, principal, tool, mode, task, workspace,
                             human_summary):
        self._counter += 1
        approval_id = f"ap-{self._counter:04d}"
        self.store.requests[approval_id] = {
            "approval_id": approval_id, "principal": principal, "tool": tool,
            "mode": mode, "task": task, "workspace": workspace,
            "human_summary": human_summary, "state": S.PENDING,
            "action_digest": action_digest(tool_id=tool, mode=mode, task=task,
                                           workspace=workspace),
            "decided_device": None}
        return approval_id

    def approve(self, approval_id, device="dev-test"):
        """Steht fuer die verifizierte iPhone-Entscheidung."""
        self.store.requests[approval_id]["state"] = S.APPROVED
        self.store.requests[approval_id]["decided_device"] = device

    def reject(self, approval_id):
        """Steht fuer eine ABGELEHNTE Entscheidung. Sie ist endgueltig."""
        self.store.requests[approval_id]["state"] = S.DENIED


class FakeCoordinator:
    """Fuehrt nur aus, was freigegeben ist — und genau einmal."""

    def __init__(self, control_plane) -> None:
        self.cp = control_plane
        self.executions: list[str] = []

    async def execute_approved(self, approval_id, executor):
        request = self.cp.store.requests.get(approval_id)
        if request is None or request["state"] != S.APPROVED:
            return None, "not_approved"
        request["state"] = S.CONSUMED
        execution_id = X.execution_id_for(self.cp.core_instance_id, approval_id)
        payload = {"tool": request["tool"], "mode": request["mode"],
                   "task": request["task"], "workspace": request["workspace"],
                   "action_digest": request["action_digest"],
                   "execution_id": execution_id,
                   "idempotency_key": X.idempotency_key_for(execution_id,
                                                            request["tool"]),
                   "semantics": X.semantics_for(request["tool"])}
        try:
            ok, info = await executor(payload)
        except X.SafeExecutionFailure as exc:
            return None, "failed_safe"
        except Exception:  # noqa: BLE001 - mehrdeutig, wie im echten Pfad
            return None, "unknown_outcome"
        self.executions.append(approval_id)
        return ({"info": info, "execution_id": execution_id}, "ok") if ok \
            else (None, "unknown_outcome")


class Stack:
    """Router, Freigabepfad und echte Zahlungsfaehigkeiten in einem Griff."""

    def __init__(self, rig: Rig, *, policy_mode: str = "enforce") -> None:
        self.rig = rig
        self.cp = FakeControlPlane()
        self.coordinator = FakeCoordinator(self.cp)
        self.approvals = CapabilityApprovals(self.coordinator,
                                             owner_principal="local-owner")
        self.router = CapabilityRouter(mobile=self.approvals,
                                       policy_mode=policy_mode)
        register_payment(self.router, rig.caps)

    @property
    def requests(self) -> list[dict]:
        return list(self.cp.store.requests.values())

    async def execute(self, name: str, arguments: dict, *,
                      origin=AP.OriginClass.TRUSTED_INTERACTIVE_APP,
                      trust=None, provenance=None, commanded: bool = True):
        from solvio.contracts.trust import TrustContext, TrustLevel
        if trust is None:
            trust = TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                                 user_authorized=True, note="test")
        return await self.router.execute(name, arguments, trust=trust,
                                         provenance=provenance,
                                         principal="local-owner", origin=origin,
                                         commanded=commanded)
