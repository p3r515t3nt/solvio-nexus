"""Core-owned configuration for Codex's native task filesystem sandbox.

This helper validates only its permission/feature projection. The existing
worker still verifies provider, account, instructions, skills, Home and costs.
No provider calls, config writes, sandbox implementation or agent loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

PROFILE = "solvio-task"
VERSION = "solvio-native-work-v1"
_NETWORK_NULLS = ("proxy_url", "enable_socks5", "socks_url", "enable_socks5_udp",
    "allow_upstream_proxy", "dangerously_allow_non_loopback_proxy",
    "dangerously_allow_all_unix_sockets", "mode", "domains", "unix_sockets",
    "allow_local_binding", "mitm")
_SANDBOX = {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
            "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
_FEATURE_DEFAULTS = {"network_proxy": None, "auth_elicitation": True,
    "mentions_v2": True, "mcp_2026_07_28": False, "remote_control": False}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _same(left, right):
    # JSON booleans are not interchangeable with Python's integer 0/1.
    return _json(left) == _json(right)


def _path(value, *, private=False):
    if (type(value) is not str or not 0 < len(value) <= 2000
            or any(ord(c) < 32 for c in value)):
        raise ValueError("native_work_path_invalid")
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or str(path.resolve()) != value
            or not path.exists() or not (path.is_dir() or path.is_file())):
        raise ValueError("native_work_path_invalid")
    stat = path.stat()
    if private and (not path.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077):
        raise ValueError("native_work_workspace_not_private")
    return path, (stat.st_dev, stat.st_ino)


def _overlap(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


@dataclass(frozen=True)
class NativeWorkPolicy:
    """Constructor arguments are trusted Core configuration, never model/HTTP input.

    Optional runtime roots are pinned to canonical paths and filesystem identity;
    their software contents remain managed by the Core, outside this sandbox.
    """
    workspace: str
    denied_paths: tuple[str, ...]
    readonly_runtime_paths: tuple[str, ...] = ()
    _identities: tuple = field(init=False, repr=False)

    def __post_init__(self):
        workspace, identity = _path(self.workspace, private=True)
        groups = []
        for values in (self.denied_paths, self.readonly_runtime_paths):
            if type(values) not in (tuple, list) or any(type(v) is not str for v in values):
                raise ValueError("native_work_path_invalid")
            groups.append(tuple(sorted(set(values))))
        denied, runtime = groups
        if not denied or len(denied) > 32 or len(runtime) > 16:
            raise ValueError("native_work_path_invalid")
        denied_details = [_path(value) for value in denied]
        runtime_details = [_path(value) for value in runtime]
        if any(_overlap(workspace, path) for path, _ in denied_details + runtime_details):
            raise ValueError("native_work_path_overlap")
        if any(_overlap(read, deny) for read, _ in runtime_details for deny, _ in denied_details):
            raise ValueError("native_work_path_overlap")
        # Never turn :minimal into a broad root or personal Home read grant.
        home = Path.home().resolve()
        if any(home.is_relative_to(path) for path, _ in runtime_details):
            raise ValueError("native_work_runtime_too_broad")
        object.__setattr__(self, "denied_paths", denied)
        object.__setattr__(self, "readonly_runtime_paths", runtime)
        object.__setattr__(self, "_identities", ((self.workspace, identity),) + tuple(
            (str(path), inode) for path, inode in denied_details + runtime_details))

    def _physical(self):
        for value, identity in self._identities:
            _, current = _path(value, private=value == self.workspace)
            if current != identity:
                raise ValueError("native_work_path_changed")

    def _permissions(self):
        return {PROFILE: {"filesystem": {":minimal": "read", self.workspace: "write",
            **{value: "deny" for value in self.denied_paths},
            **{value: "read" for value in self.readonly_runtime_paths}}, "network": {"enabled": False}}}

    @property
    def digest(self):
        return hashlib.sha256(_json({"version": VERSION, "permissions": self._permissions(),
            "features": {"shell_tool": True}, "paths": self._identities}).encode("utf-8")).hexdigest()

    def cli_args(self):
        """Append to the existing reviewed app-server arguments, no shell."""
        self._physical()
        fs = self._permissions()[PROFILE]["filesystem"]
        table = "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in sorted(fs.items())) + "}"
        return ("--enable", "shell_tool", "-c", f"permissions.{PROFILE}.filesystem={table}",
                "-c", f"permissions.{PROFILE}.network.enabled=false")

    def verify_config(self, response):
        """Check our raw CLI layer and the exact native0.147.0 normalization.

        Other top-level configuration keys are intentionally owned by the
        worker's existing config/Home guards, which must still run.
        """
        self._physical()
        if type(response) is not dict or type(response.get("layers")) is not list:
            raise ValueError("native_work_config_unverified")
        saw_permissions = saw_shell = False
        raw_features = {}
        for layer in response["layers"]:
            config = layer.get("config") if type(layer) is dict else None
            if type(config) is not dict or type(layer.get("name")) is not dict:
                raise ValueError("native_work_config_unverified")
            session_flags = layer["name"].get("type") == "sessionFlags"
            if "default_permissions" in config:
                raise ValueError("native_work_config_unverified")
            if "permissions" in config:
                if saw_permissions or not session_flags or not _same(config["permissions"], self._permissions()):
                    raise ValueError("native_work_config_unverified")
                saw_permissions = True
            features = config.get("features", {})
            if type(features) is not dict:
                raise ValueError("native_work_config_unverified")
            for name, value in features.items():
                if name == "shell_tool" and value is True and session_flags:
                    saw_shell = True
                elif value is not False:
                    raise ValueError("native_work_config_unverified")
                # Native config/read returns layers in descending precedence.
                raw_features.setdefault(name, value)
        effective = response.get("config")
        if not saw_permissions or not saw_shell or type(effective) is not dict:
            raise ValueError("native_work_config_unverified")
        expected = self._permissions()
        profile = expected[PROFILE]
        profile.update(description=None, extends=None, workspace_roots=None)
        profile["filesystem"]["glob_scan_max_depth"] = None
        profile["network"].update({key: None for key in _NETWORK_NULLS})
        if (not _same(effective.get("permissions"), expected)
                or effective.get("default_permissions") is not None
                or raw_features.get("shell_tool") is not True
                or not _same(effective.get("features"), {**_FEATURE_DEFAULTS, **raw_features})):
            raise ValueError("native_work_config_mismatch")

    def verify_thread(self, response):
        """Shared start/resume projection; existing thread/model guards remain."""
        self._physical()
        if (type(response) is not dict
                or not _same(response.get("activePermissionProfile"), {"id": PROFILE, "extends": None})
                or response.get("cwd") != self.workspace
                or not _same(response.get("sandbox"), _SANDBOX)):
            raise ValueError("native_work_policy_mismatch")
