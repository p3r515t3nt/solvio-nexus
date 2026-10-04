"""Named native permissions: narrow contract and real local sandbox, no model."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import socket
import sys
import tomllib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from _private_temp import private_folder
from solvio.specialists.native_work_policy import NativeWorkPolicy, PROFILE


@contextmanager
def world():
    # Outside /tmp on purpose: the native sandbox reaches /tmp (measured
    # 2026-09-17), which would turn every deny case below green-by-accident.
    with private_folder("solvio-work-policy-") as root:
        for name in ("workspace", "core", "home", "ipc", "other", "runtime"):
            path = root / name
            path.mkdir(mode=0o700)
            (path / "synthetic.txt").write_text("synthetic data without credentials")
        policy = NativeWorkPolicy(str(root / "workspace"), tuple(str(root / n) for n in ("core", "home", "ipc")))
        yield root, policy


def refused(fn):
    try:
        fn()
    except ValueError as exc:
        require(str(exc).startswith("native_work_"), str(exc))
        return
    require(False, "unsafe native policy accepted")


def config_for(policy):
    # Parse actual CLI TOML rather than guessing what its quoting produces.
    args = policy.cli_args()
    merged = {}
    for index, value in enumerate(args):
        if value == "-c":
            parsed = tomllib.loads(args[index + 1])["permissions"][PROFILE]
            merged.update(parsed)
    raw = {"permissions": {PROFILE: merged}, "features": {"shell_tool": True}}
    effective = deepcopy(raw)
    effective["features"].update(apps=False, network_proxy=None, auth_elicitation=True,
        mentions_v2=True, mcp_2026_07_28=False, remote_control=False)
    p = effective["permissions"][PROFILE]
    p.update(description=None, extends=None, workspace_roots=None)
    p["filesystem"]["glob_scan_max_depth"] = None
    p["network"].update({name: None for name in ("proxy_url", "enable_socks5", "socks_url",
        "enable_socks5_udp", "allow_upstream_proxy", "dangerously_allow_non_loopback_proxy",
        "dangerously_allow_all_unix_sockets", "mode", "domains", "unix_sockets",
        "allow_local_binding", "mitm")})
    return {"layers": [{"name": {"type": "sessionFlags"}, "config": raw},
        {"name": {"type": "user"}, "config": {"features": {"shell_tool": False, "apps": False}}}],
        "config": effective}


def thread_for(policy):
    return {"activePermissionProfile": {"id": PROFILE, "extends": None}, "cwd": policy.workspace,
        "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                    "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}}


def t_policy_is_frozen_canonical_and_explicit_runtime_changes_digest():
    with world() as (root, policy):
        require_equal(policy.digest, NativeWorkPolicy(policy.workspace, list(reversed(policy.denied_paths))).digest)
        require(len(policy.digest) == 64)
        with_runtime = NativeWorkPolicy(policy.workspace, policy.denied_paths, (str(root / "runtime"),))
        require(with_runtime.digest != policy.digest)
        require_equal(config_for(with_runtime)["layers"][0]["config"]["permissions"][PROFILE]
                      ["filesystem"][str(root / "runtime")], "read")
        try:
            policy.workspace = "/"
            require(False, "mutable policy")
        except FrozenInstanceError:
            pass
        policy.verify_config(config_for(policy))
        policy.verify_thread(thread_for(policy))


def t_canonical_private_disjoint_paths_and_runtime_inode_are_required():
    with world() as (root, policy):
        alias = root / "alias"
        alias.symlink_to(root / "workspace")
        for workspace in ("relative", str(alias), str(root / "workspace") + "/", str(root / "workspace" / "missing")):
            refused(lambda: NativeWorkPolicy(workspace, policy.denied_paths))
        for denied, runtime in (((policy.workspace,), ()), ((str(root),), ()),
                (policy.denied_paths, (str(root),)), (policy.denied_paths, (str(Path.home().resolve()),)),
                (policy.denied_paths, (str(root / "core"),))):
            refused(lambda: NativeWorkPolicy(policy.workspace, denied, runtime))
        (root / "workspace").chmod(0o755)
        refused(policy.cli_args)
        (root / "workspace").chmod(0o700)
        pinned = NativeWorkPolicy(policy.workspace, policy.denied_paths, (str(root / "runtime"),))
        (root / "runtime").rename(root / "old-runtime")
        (root / "runtime").mkdir(mode=0o700)
        refused(pinned.cli_args)


def t_raw_layers_cannot_inherit_permissions_defaults_or_extra_enabled_features():
    with world() as (_, policy):
        for change in ("foreign_source", "extra_profile", "raw_null", "default", "network",
                       "extra_feature", "missing_shell", "duplicate", "no_layers"):
            config = config_for(policy)
            raw = config["layers"][0]["config"]
            if change == "foreign_source": config["layers"][0]["name"]["type"] = "user"
            elif change == "extra_profile": raw["permissions"]["foreign"] = {}
            elif change == "raw_null": raw["permissions"][PROFILE]["extends"] = None
            elif change == "default": raw["default_permissions"] = PROFILE
            elif change == "network": raw["permissions"][PROFILE]["network"]["enabled"] = True
            elif change == "extra_feature": raw["features"]["apps"] = True
            elif change == "missing_shell": raw["features"]["shell_tool"] = False
            elif change == "duplicate": config["layers"].append(deepcopy(config["layers"][0]))
            else: config.pop("layers")
            refused(lambda: policy.verify_config(config))


def t_effective_config_rejects_widening_unknown_fields_and_boolean_substitution():
    with world() as (root, policy):
        for change in ("extra_profile", "read_root", "other_write", "extends", "new_field",
                       "socket", "network_zero", "missing_null", "glob_depth", "default", "shell_int"):
            config = config_for(policy)
            effective = config["config"]
            p = effective["permissions"][PROFILE]
            if change == "extra_profile": effective["permissions"]["other"] = {}
            elif change == "read_root": p["filesystem"][":root"] = "read"
            elif change == "other_write": p["filesystem"][str(root / "other")] = "write"
            elif change == "extends": p["extends"] = ":workspace"
            elif change == "new_field": p["network"]["future_permission"] = None
            elif change == "socket": p["network"]["unix_sockets"] = ["/tmp/core.sock"]
            elif change == "network_zero": p["network"]["enabled"] = 0
            elif change == "missing_null": p.pop("description")
            elif change == "glob_depth": p["filesystem"]["glob_scan_max_depth"] = 50
            elif change == "default": effective["default_permissions"] = PROFILE
            else: effective["features"]["shell_tool"] = 1
            refused(lambda: policy.verify_config(config))


def t_start_resume_projection_rejects_wrong_profile_workspace_and_sandbox():
    with world() as (_, policy):
        for change in ("id", "extends", "cwd", "network", "roots", "tmp", "read_only", "extra", "bool_int"):
            response = thread_for(policy)
            if change == "id": response["activePermissionProfile"]["id"] = ":workspace"
            elif change == "extends": response["activePermissionProfile"]["extends"] = ":workspace"
            elif change == "cwd": response["cwd"] += "/"
            elif change == "network": response["sandbox"]["networkAccess"] = True
            elif change == "roots": response["sandbox"]["writableRoots"] = ["/tmp"]
            elif change == "tmp": response["sandbox"]["excludeSlashTmp"] = False
            elif change == "read_only": response["sandbox"]["type"] = "readOnly"
            elif change == "extra": response["activePermissionProfile"]["new"] = None
            else: response["sandbox"]["networkAccess"] = 0
            refused(lambda: policy.verify_thread(response))


def t_real_codex_helper_config_and_native_filesystem_without_account_or_model():
    import hashlib
    import shutil
    from solvio.specialists.hermes_native_worker import native_config_text
    from solvio.specialists.launcher import child_environment
    hermes = Path.home() / ".solvio-hermes/src"
    sys.path.insert(0, str(hermes))
    from agent.transports.codex_app_server import CodexAppServerClient
    binary = "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
    require(Path(binary).is_file(), "local native Codex runtime required")
    python = Path("/Library/Developer/CommandLineTools/usr/bin/python3").resolve(strict=True)
    require(python.is_file() and os.access(python, os.X_OK), "local CLT Python runtime required")
    with world() as (root, policy):
        # Preserve Versions/3.9 and its framework links inside the already
        # allowed workspace; the loader receives no extra sandbox read grant.
        framework = python.parents[3]
        runtime = root / "workspace/runtime/Python3.framework"
        shutil.copytree(framework, runtime, symlinks=True)
        for copied in runtime.rglob("*"):
            original = framework / copied.relative_to(runtime)
            require(copied.resolve(strict=True).is_relative_to(runtime),
                    "copied runtime link leaves the workspace fixture")
            if copied.is_symlink():
                require_equal(copied.readlink(), original.readlink())
            elif copied.is_file():
                require_equal(hashlib.sha256(copied.read_bytes()).digest(),
                              hashlib.sha256(original.read_bytes()).digest(),
                              "copied runtime bytes changed")
        python = runtime / python.relative_to(framework)
        require(python.is_file() and os.access(python, os.X_OK), "copied CLT Python runtime required")
        (root / "home/config.toml").write_text(native_config_text("gpt-5.6-sol"))
        (root / "workspace/escape").symlink_to(root / "other/synthetic.txt")
        with patch.dict(os.environ, child_environment(), clear=True):
            cli = CodexAppServerClient(codex_bin=binary, codex_home=str(root / "home"),
                extra_args=["--stdio", "--strict-config", *policy.cli_args()])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            cli.initialize(client_name="solvio_policy_test", client_version="1", capabilities={"experimentalApi": True})
            policy.verify_config(cli.request("config/read", {"cwd": policy.workspace, "includeLayers": True}, timeout=8))
            response = cli.request("thread/start", {"cwd": policy.workspace, "permissions": PROFILE, "ephemeral": True}, timeout=8)
            policy.verify_thread(response)
            # "other" is deliberately NOT on the deny list: it stands for the
            # ledger folder and the socket root beside it, which :minimal must
            # shield without an explicit entry (the bridge relies on this).
            cases = [("write", ["/usr/bin/touch", str(root / "workspace/created")], True),
                ("read", ["/bin/cat", str(root / "workspace/synthetic.txt")], True),
                *[(name, ["/bin/cat", str(root / name / "synthetic.txt")], False) for name in ("core", "home", "ipc", "other")],
                *[("list_" + name, ["/bin/ls", str(root / name)], False) for name in ("ipc", "other")],
                ("symlink", ["/bin/cat", str(root / "workspace/escape")], False),
                ("outside_write", ["/usr/bin/touch", str(root / "other/created")], False),
                ("socket_dir_write", ["/usr/bin/touch", str(root / "ipc/core.sock")], False),
                # Run the byte-identical CLT fixture without Apple's xcrun
                # launcher; keep the exact sandbox and every deny probe intact.
                ("stdlib", [str(python), "-I", "-B", "-c", "import json,pathlib; print('ok')"], True),
                ("network", ["/usr/bin/curl", "--max-time", "1", "http://127.0.0.1:" + str(listener.getsockname()[1])], False)]
            for label, command, expected in cases:
                result = cli.request("command/exec", {"command": command, "cwd": policy.workspace,
                    "permissionProfile": PROFILE, "timeoutMs": 5000}, timeout=8)
                require_equal(result.get("exitCode") == 0, expected,
                    label + (" exit=" + str(result.get("exitCode")) + " stderr="
                             + str(result.get("stderr", ""))[-1600:] if label == "stdlib" else ""))
            require((root / "workspace/created").is_file())
            require(not (root / "other/created").exists())
            require(not (root / "ipc/core.sock").exists())
        finally:
            cli.close()
            listener.close()


def t_real_codex_sandbox_under_the_e1_workspace_root_shields_neighbours_jail_ledger_and_home():
    """N8/C4 §5.1/§6.2 E1 — the physical probe repeated under the new root form
    `<root>/workspaces/<task_id>` with the Claude jail beside it under
    `<root>/claude-jails/<task_id>` (N8_C4_API.md §1.2). Measured 2026-09-18
    (13/13). File reads use cat/wc -c: a metadata stat (`ls -ld`) on temp files
    is allowed by the Codex sandbox and would turn deny cases green by accident.
    The real sealed targets of the owner home are probed read-only (byte count)
    only when they exist; every one of them must be refused by the kernel."""
    from solvio.specialists.hermes_native_worker import native_config_text
    from solvio.specialists.launcher import child_environment
    hermes = Path.home() / ".solvio-hermes/src"
    sys.path.insert(0, str(hermes))
    from agent.transports.codex_app_server import CodexAppServerClient
    binary = "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
    require(Path(binary).is_file(), "local native Codex runtime required")
    home = Path.home()
    real_targets = [home / ".solvio", home / ".solvio-nexus", home / ".solvio-nexus/codex/auth.json", home / ".claude.json"]
    with private_folder("solvio-c4-e1-") as base:
        ws_root, jail_root = base / "workspaces", base / "claude-jails"
        for folder in (ws_root, jail_root):
            folder.mkdir(mode=0o700)
        task, neighbour = "at-0123456789abcdef", "at-fedcba9876543210"
        workspace = ws_root / task
        workspace.mkdir(mode=0o700)
        other = ws_root / neighbour
        other.mkdir(mode=0o700)
        (other / "witness.txt").write_text("synthetic neighbour result")
        jail = jail_root / task
        jail.mkdir(mode=0o700)
        (jail / "sock").mkdir(mode=0o700)
        other_jail = jail_root / neighbour
        other_jail.mkdir(mode=0o700)
        (other_jail / "mcp.json").write_text("{}")
        codex_home = base / "home"
        codex_home.mkdir(mode=0o700)
        (codex_home / "config.toml").write_text(native_config_text("gpt-5.6-sol"))
        ledger = base / "ledger"
        ledger.mkdir(mode=0o700)
        (ledger / "agent_runs.sqlite3").write_text("synthetic ledger bytes")
        core = base / "core"
        core.mkdir(mode=0o700)
        (core / "state.txt").write_text("synthetic core state")
        policy = NativeWorkPolicy(str(workspace), (str(codex_home), str(jail / "sock"), str(core)))
        with patch.dict(os.environ, child_environment(), clear=True):
            cli = CodexAppServerClient(codex_bin=binary, codex_home=str(codex_home),
                extra_args=["--stdio", "--strict-config", *policy.cli_args()])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            cli.initialize(client_name="solvio_c4_e1_probe", client_version="1", capabilities={"experimentalApi": True})
            policy.verify_config(cli.request("config/read", {"cwd": policy.workspace, "includeLayers": True}, timeout=8))
            policy.verify_thread(cli.request("thread/start", {"cwd": policy.workspace, "permissions": PROFILE,
                                                              "ephemeral": True}, timeout=8))
            cases = [("own_write", ["/usr/bin/touch", str(workspace / "created")], True),
                     ("own_read", ["/bin/cat", str(workspace / "created")], True),
                     ("neighbour_workspace_read", ["/bin/cat", str(other / "witness.txt")], False),
                     ("neighbour_workspace_list", ["/bin/ls", str(other)], False),
                     ("neighbour_jail_read", ["/bin/cat", str(other_jail / "mcp.json")], False),
                     ("own_jail_socket_write", ["/usr/bin/touch", str(jail / "sock/core.sock")], False),
                     ("workspace_root_list", ["/bin/ls", str(ws_root)], False),
                     ("ledger_read", ["/bin/cat", str(ledger / "agent_runs.sqlite3")], False),
                     ("core_read", ["/bin/cat", str(core / "state.txt")], False),
                     ("codex_home_read", ["/bin/cat", str(codex_home / "config.toml")], False),
                     ("network", ["/usr/bin/curl", "--max-time", "1",
                                  "http://127.0.0.1:%d" % listener.getsockname()[1]], False)]
            for target in real_targets:
                if target.exists():
                    command = ["/usr/bin/wc", "-c", str(target)] if target.is_file() else ["/bin/ls", str(target)]
                    cases.append(("owner_home:" + target.name, command, False))
            observed = {}
            for label, command, expected in cases:
                result = cli.request("command/exec", {"command": command, "cwd": policy.workspace,
                    "permissionProfile": PROFILE, "timeoutMs": 5000}, timeout=8)
                observed[label] = (result.get("exitCode") == 0, str(result.get("stderr", ""))[:400])
                require_equal(observed[label][0], expected, label + ": " + observed[label][1])
                if not expected and label != "network":
                    require("Operation not permitted" in observed[label][1] or "Permission denied" in observed[label][1],
                            label + " was refused by something other than the kernel: " + observed[label][1])
            require((workspace / "created").is_file())
            require(not (jail / "sock/core.sock").exists())
            require(len(cases) >= 12, "the probe must cover the E1 neighbours and the owner home")
        finally:
            cli.close()
            listener.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
