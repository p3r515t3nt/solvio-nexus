"""Was es wirklich gibt — und was SOLVIO ueber sich selbst NICHT weiss.

Ein Planer, der Faehigkeiten erfinden kann, ist schlimmer als keiner. Er
produziert Vorschlaege, die plausibel klingen und ins Leere greifen, und der
Fehler faellt erst auf, wenn jemand darauf baut. Deshalb kommen beide Inventare
hier aus **dem Core**, nicht aus dem Modell: die Faehigkeiten aus dem Register des
Routers, die Laufzeitfakten aus Konfiguration und gemessenem Zustand.

Der zweite Teil ist der unbequemere: **was nicht geprueft wurde, heisst
`unbekannt`.** Nicht „vermutlich Debian", nicht „vermutlich ARM64". Beim
Raspberry Pi ist das keine Spitzfindigkeit — die Frage, welches Browserpaket
passt, haengt genau daran, und eine erfundene Architektur ergibt einen
selbstbewussten falschen Rat. Ein `unknown` im Inventar ist eine Aufforderung zu
recherchieren; ein geratener Wert ist es nicht, weil niemand ihn mehr hinterfragt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from solvio.capabilities.contract import (
    CapabilitySpec, ExecutionClass, RiskLevel, requires_approval,
)

UNKNOWN = "unbekannt"


@dataclass(frozen=True)
class CapabilityFact:
    """Eine registrierte Faehigkeit, wie der Core sie kennt."""

    name: str
    execution_class: str
    base_risk: str
    semantics: str
    read_only: bool
    needs_approval: bool
    executor: str
    description: str
    #: Ob der Ausfuehrende gerade erreichbar ist. `None` = nicht gemessen.
    available: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        entry = {"name": self.name, "execution_class": self.execution_class,
                 "risk": self.base_risk, "semantics": self.semantics,
                 "read_only": self.read_only, "needs_approval": self.needs_approval,
                 "executor": self.executor}
        if self.description:
            entry["description"] = self.description[:160]
        entry["available"] = UNKNOWN if self.available is None else self.available
        return entry


class CapabilityInventory:
    """Der Blick des Core auf seine eigenen Faehigkeiten.

    Bewusst eine duenne Schicht ueber dem Router statt einer zweiten Liste. Zwei
    Listen laufen auseinander, und die falsche ist immer die, die gerade gelesen
    wird.
    """

    def __init__(self, router: Any, *, probes: dict[str, Any] | None = None,
                 descriptions: dict[str, str] | None = None) -> None:
        self.router = router
        #: name -> callable() -> bool. Wird gefragt, statt geraten.
        self.probes = probes or {}
        #: name -> deutschsprachige Beschreibung. Notwendig, weil `CapabilitySpec.
        #: description` in keiner einzigen Faehigkeit gesetzt ist: ohne diese
        #: Texte liesse sich ein deutscher Satz nur gegen englische Bezeichner
        #: vergleichen, und „Trag mir einen Termin ein" faende `calendar_create_
        #: event` nie. Die Texte sind Core-eigen — dieselben, die das Modell
        #: ohnehin sieht.
        self.descriptions = descriptions or {}

    def names(self) -> list[str]:
        return list(self.router.names()) if self.router is not None else []

    def has(self, name: str) -> bool:
        return name in self.names()

    def fact(self, name: str) -> CapabilityFact | None:
        spec: CapabilitySpec | None = (self.router.spec(name)
                                       if self.router is not None else None)
        if spec is None:
            return None
        return CapabilityFact(
            name=spec.name,
            execution_class=_name_of(spec.execution_class, ExecutionClass),
            base_risk=_name_of(spec.base_risk, RiskLevel),
            semantics=_name_of(spec.semantics, None),
            read_only=bool(spec.is_read_only()),
            # Aus dem Vertrag abgeleitet, nicht behauptet: dieselbe Funktion, die
            # auch der Router benutzt, damit ein Vorschlag nie eine andere
            # Freigabepflicht nennt als die spaetere Ausfuehrung.
            needs_approval=bool(requires_approval(spec.base_risk)),
            executor=str(getattr(spec, "executor", "") or ""),
            description=(self.descriptions.get(spec.name)
                         or str(getattr(spec, "description", "") or "")),
            available=self._available(spec))

    def facts(self) -> list[CapabilityFact]:
        return [f for f in (self.fact(n) for n in self.names()) if f is not None]

    def as_dict(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.facts()]

    def read_only_names(self) -> list[str]:
        return [f.name for f in self.facts() if f.read_only]

    def _available(self, spec: CapabilitySpec) -> bool | None:
        """Ob der Ausfuehrende erreichbar ist — gemessen oder `None`.

        `None` ist hier eine echte Antwort und keine Luecke: „ich habe nicht
        nachgesehen" unterscheidet sich von „nicht erreichbar", und nur die
        zweite rechtfertigt einen anderen Weg.
        """
        probe = self.probes.get(spec.name) or self.probes.get(
            str(getattr(spec, "executor", "") or ""))
        if probe is None:
            return None
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - eine Sonde faellt nie nach oben durch
            return None


def _name_of(value: Any, enum_type: Any) -> str:
    return getattr(value, "name", None) or str(value)


@dataclass
class RuntimeFact:
    """Ein Geraet oder eine Laufzeit — mit dem, was wirklich bekannt ist."""

    key: str
    kind: str
    #: Freitext-Attribute. Was fehlt, fehlt — es wird nicht mit UNKNOWN gefuellt,
    #: damit ein Leser den Unterschied zwischen „nicht gefragt" und „gefragt,
    #: nichts gefunden" nicht verliert.
    attributes: dict[str, str] = field(default_factory=dict)
    #: Wurde die Erreichbarkeit in DIESEM Lauf gemessen?
    reachable: bool | None = None
    #: Woher die Angaben stammen. Ohne Herkunft ist ein Fakt eine Behauptung.
    source: str = ""
    #: Wie der Mensch dieses Geraet nennt. „pi-wohnzimmer" sagt niemand; gesagt
    #: wird „mein Raspberry Pi". Ohne diese Liste findet der Resolver das Ziel
    #: eines Satzes nicht und schlaegt am Ende auf dem falschen Geraet etwas vor.
    aliases: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        """Ob dieser Satz dieses Geraet meint.

        Leere Aliasse werden ausdruecklich uebersprungen: `"" in text` ist immer
        wahr, und ein Eintrag ohne Alias hat damit frueher jedes Ziel „getroffen"
        — der erste in der Liste gewann, und der Vorschlag landete auf dem
        falschen Geraet.
        """
        low = (text or "").lower()
        if not low:
            return False
        candidates = [self.key.lower(), *(a.lower() for a in self.aliases)]
        candidates += [part for part in self.key.lower().split("-") if len(part) > 3]
        return any(c and c in low for c in candidates)

    def get(self, attribute: str) -> str:
        return self.attributes.get(attribute, UNKNOWN)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "source": self.source,
                "reachable": UNKNOWN if self.reachable is None else self.reachable,
                **{k: v for k, v in sorted(self.attributes.items())}}


class RuntimeInventory:
    """Die kleine, wahrheitsgemaesse Geraeteliste. Ausdruecklich keine CMDB."""

    def __init__(self) -> None:
        self._facts: dict[str, RuntimeFact] = {}

    def add(self, fact: RuntimeFact) -> None:
        self._facts[fact.key] = fact

    def get(self, key: str) -> RuntimeFact | None:
        return self._facts.get(key)

    def keys(self) -> list[str]:
        return sorted(self._facts)

    def facts(self) -> list[RuntimeFact]:
        return [self._facts[k] for k in self.keys()]

    def as_dict(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.facts()]

    def unknowns(self) -> list[str]:
        """Was fuer eine Entscheidung fehlt — die Recherche-Liste.

        Diese Methode ist der Grund, warum `unknown` nicht aufgefuellt wird: sie
        macht aus Nichtwissen eine Arbeitsliste statt einer stillen Annahme.
        """
        missing: list[str] = []
        for fact in self.facts():
            if fact.reachable is None:
                missing.append(f"{fact.key}: Erreichbarkeit nicht gemessen")
            for attribute in ("os", "architektur"):
                if fact.kind in ("host", "satellite") and attribute not in fact.attributes:
                    missing.append(f"{fact.key}: {attribute} unbekannt")
        return missing
