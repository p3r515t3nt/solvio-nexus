"""Accept Codex's bounded native project-trust metadata beside a fixed Home.

A project trust marker is not a SOLVIO tool or filesystem grant. No tool,
instruction, provider or permission layer is accepted by this exception.
The worker must still verify config/read, disabled project instructions and
skills, and the explicit native sandbox/NamedProfile on every dispatch.
This module never writes configuration or reads authentication material.
"""
from __future__ import annotations

import json
from pathlib import Path
import tomllib

MAX_CONFIG_BYTES = 128 * 1024
MAX_PROJECTS = 256
MAX_PATH_CHARS = 2000


def _canonical(value):
    if (type(value) is not str or not 0 < len(value) <= MAX_PATH_CHARS
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("native_projects_invalid")
    path = Path(value)
    try:
        if (not path.is_absolute() or str(path) != value
                or str(path.resolve(strict=False)) != value):
            raise ValueError("native_projects_invalid")
    except (OSError, RuntimeError):
        raise ValueError("native_projects_invalid") from None
    return path


def validate_projects(value, home) -> None:
    """Validate historical paths without requiring their workspaces to exist."""
    if type(value) is not dict or len(value) > MAX_PROJECTS:
        raise ValueError("native_projects_invalid")
    try:
        native_home = Path(home).resolve(strict=False)
        personal_home = Path.home().resolve(strict=False)
    except (TypeError, ValueError, OSError, RuntimeError):
        raise ValueError("native_projects_invalid") from None
    if not Path(home).is_absolute():
        raise ValueError("native_projects_invalid")
    for name, settings in value.items():
        path = _canonical(name)
        if (type(settings) is not dict or set(settings) != {"trust_level"}
                or type(settings["trust_level"]) is not str or settings["trust_level"] != "trusted"
                or native_home.is_relative_to(path) or personal_home.is_relative_to(path)):
            raise ValueError("native_projects_invalid")


def _parse(raw):
    try:
        if type(raw) is not str or len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("native_config_mismatch")
        return tomllib.loads(raw)
    except (tomllib.TOMLDecodeError, UnicodeError):
        raise ValueError("native_config_mismatch") from None


def validate_config_text(raw, expected_template, home) -> None:
    """Exact semantic template plus only the native projects metadata map.

    JSON comparison preserves boolean/integer distinctions; TOML dates, NaN,
    duplicate keys, unknown settings and malformed input remain rejected.
    """
    actual, expected = _parse(raw), _parse(expected_template)
    if "projects" in expected:
        raise ValueError("native_config_mismatch")
    if "projects" in actual:
        validate_projects(actual.pop("projects"), home)
    try:
        frozen_actual = json.dumps(actual, sort_keys=True, ensure_ascii=False, allow_nan=False)
        frozen_expected = json.dumps(expected, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("native_config_mismatch") from None
    if frozen_actual != frozen_expected:
        raise ValueError("native_config_mismatch")
