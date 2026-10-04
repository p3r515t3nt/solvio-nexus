"""Lokaler Health-Check. Fuehrt bewusst KEINE Netzwerkverbindungen aus."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field

from solvio import __version__
from solvio.config import Settings, load_settings


@dataclass
class HealthReport:
    status: str
    python: str
    platform: str
    config_ok: bool
    checks: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            "SOLVIO Core",
            f"Version: {__version__}",
            f"Status: {self.status}",
            f"Python: {self.python}",
            f"Platform: {self.platform}",
            f"Config: {'OK' if self.config_ok else 'FEHLER'}",
        ]
        lines += [f"{k}: {v}" for k, v in self.checks.items()]
        return "\n".join(lines)


def collect_health(settings: Settings | None = None) -> HealthReport:
    config_ok = True
    try:
        settings = settings or load_settings()
    except Exception:
        config_ok = False
        settings = None

    checks: dict[str, str] = {}
    if settings is None:
        for key in ("OpenAI Key", "Realtime", "Home Assistant", "Voice Satellite"):
            checks[key] = "UNKNOWN"
    else:
        checks["OpenAI Key"] = "CONFIGURED" if settings.has_openai_key else "NOT CONFIGURED"
        checks["Realtime"] = (
            f"CONFIGURED ({settings.openai_realtime_model})"
            if settings.has_realtime
            else "NOT CONFIGURED"
        )
        checks["Home Assistant"] = "CONFIGURED" if settings.has_home_assistant else "NOT CONNECTED"
        checks["Voice Satellite"] = "CONFIGURED" if settings.has_voice_satellite else "NOT CONNECTED"
    checks["Memory"] = "NOT INITIALIZED"

    return HealthReport(
        status="READY" if config_ok else "DEGRADED",
        python=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        config_ok=config_ok,
        checks=checks,
    )
