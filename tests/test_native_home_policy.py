"""Native trust metadata stays separate from tool/instruction permissions."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.specialists import native_home_policy as H

TEMPLATE = 'model="gpt-5.6-sol"\napproval_policy="never"\n[features]\nshell_tool=false\n'


@contextmanager
def world():
    with tempfile.TemporaryDirectory(prefix="solvio-home-policy-") as folder:
        root = Path(folder).resolve()
        for name in ("home", "workspace", "other", "core", "ipc"):
            (root / name).mkdir(mode=0o700)
        yield root


def refused(fn):
    try:
        fn()
    except ValueError as exc:
        require(str(exc).startswith("native_"), str(exc))
        return
    require(False, "invalid native Home metadata accepted")


def project_text(path, extra=""):
    return "\n[projects." + json.dumps(str(path)) + ']\ntrust_level="trusted"\n' + extra


def research_layers(response, home):
    """Exact proposed wiring: validate only projects, then run existing guard."""
    from solvio.specialists.hermes_native_worker import _validate_layers
    projected = deepcopy(response)
    for layer in projected["layers"]:
        if "projects" in layer["config"]:
            H.validate_projects(layer["config"].pop("projects"), str(home))
    _validate_layers(projected)


def t_semantic_template_accepts_native_trust_and_preserves_historical_paths():
    with world() as root:
        actual = '# formatting is not authority\n' + TEMPLATE + project_text(root / "workspace")
        H.validate_config_text(actual, TEMPLATE, str(root / "home"))
        (root / "workspace").rmdir()
        H.validate_config_text(actual, TEMPLATE, str(root / "home"))
        H.validate_projects({}, str(root / "home"))
        H.validate_config_text(TEMPLATE, TEMPLATE, str(root / "home"))


def t_projects_rejects_noncanonical_broad_and_symlinked_paths():
    with world() as root:
        alias = root / "alias"
        alias.symlink_to(root / "workspace")
        invalid = ("relative", "/", str(root / "home"), str(root), str(root.parent),
            str(Path.home().resolve()), str(alias), str(root / "workspace") + "/", str(root) + "/other/../workspace",
            str(root) + "//workspace", str(root) + "/bad\npath", str(root) + "/" + "a" * 2001)
        for path in invalid:
            refused(lambda: H.validate_projects({path: {"trust_level": "trusted"}}, str(root / "home")))
        for settings in (None, "trusted", [], {}, {"trust_level": True}, {"trust_level": "untrusted"},
                {"trust_level": "trusted", "sandbox_mode": "danger-full-access"},
                {"trust_level": "trusted", "developer_instructions": "injected"}):
            refused(lambda: H.validate_projects({str(root / "workspace"): settings}, str(root / "home")))
        for value in (None, [], ""):
            refused(lambda: H.validate_projects(value, str(root / "home")))


def t_projects_and_utf8_config_size_are_bounded():
    with world() as root:
        entries = {str(root / ("old-" + str(i))): {"trust_level": "trusted"} for i in range(256)}
        H.validate_projects(entries, str(root / "home"))
        entries[str(root / "extra")] = {"trust_level": "trusted"}
        refused(lambda: H.validate_projects(entries, str(root / "home")))
        # UTF-8 byte limit, not character count. A comment still counts.
        too_big = TEMPLATE + "#" + "ö" * (H.MAX_CONFIG_BYTES // 2)
        require(len(too_big) < H.MAX_CONFIG_BYTES)
        refused(lambda: H.validate_config_text(too_big, TEMPLATE, str(root / "home")))


def t_trust_exception_never_accepts_new_tools_instructions_or_template_type_changes():
    with world() as root:
        for raw in (TEMPLATE.replace("shell_tool=false", "shell_tool=0"),
                TEMPLATE.replace('approval_policy="never"', 'approval_policy="on-request"'),
                TEMPLATE + '\n[mcp_servers.evil]\ncommand="/never-executed"\n',
                'developer_instructions="injected"\n' + TEMPLATE,
                TEMPLATE + '\n[permissions.bad.filesystem]\n":root"="write"\n',
                TEMPLATE + project_text(root / "workspace", 'tools=["shell"]\n'),
                TEMPLATE + project_text(root / "workspace") + project_text(root / "workspace"),
                TEMPLATE + '\nunknown_date=2026-09-13\n',
                TEMPLATE + '\nunknown_number=nan\n', "[broken"):
            refused(lambda: H.validate_config_text(raw, TEMPLATE, str(root / "home")))
        refused(lambda: H.validate_config_text(None, TEMPLATE, str(root / "home")))


def t_real_named_thread_native_mutation_then_readonly_home_and_layer_guards():
    from solvio.specialists.hermes_native_worker import native_config_text
    from solvio.specialists.native_work_policy import NativeWorkPolicy, PROFILE
    from solvio.specialists.launcher import child_environment
    sys.path.insert(0, str(Path.home() / ".solvio-hermes/src"))
    from agent.transports.codex_app_server import CodexAppServerClient
    binary = "/opt/homebrew/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex"
    require(Path(binary).is_file(), "local native Codex runtime required")
    with world() as root:
        home = root / "home"
        workspace = root / "workspace"
        template = native_config_text("gpt-5.6-sol")
        (home / "config.toml").write_text(template)
        policy = NativeWorkPolicy(str(workspace), tuple(str(root / n) for n in ("home", "core", "ipc")))
        def client(extra=()):
            with patch.dict(os.environ, child_environment(), clear=True):
                return CodexAppServerClient(codex_bin=binary, codex_home=str(home),
                    extra_args=["--stdio", "--strict-config", *extra])
        first = client(policy.cli_args())
        try:
            first.initialize(client_name="solvio_home_test", client_version="1", capabilities={"experimentalApi": True})
            policy.verify_config(first.request("config/read", {"cwd": str(workspace), "includeLayers": True}, timeout=8))
            policy.verify_thread(first.request("thread/start", {"cwd": str(workspace), "permissions": PROFILE,
                "ephemeral": True, "approvalPolicy": "never"}, timeout=8))
        finally:
            first.close()
        after = (home / "config.toml").read_text()
        require(after != template, "expected native project trust persistence")
        require_equal(tomllib.loads(after)["projects"], {str(workspace): {"trust_level": "trusted"}})
        H.validate_config_text(after, template, str(home))
        second = client()
        try:
            second.initialize(client_name="solvio_readonly_test", client_version="1")
            readback = second.request("config/read", {"cwd": str(workspace), "includeLayers": True}, timeout=8)
            research_layers(readback, home)
            require_equal(readback["config"]["sandbox_mode"], "read-only")
            require_equal(readback["config"]["features"]["shell_tool"], False)
            require_equal(readback["config"].get("permissions"), None)
            thread = second.request("thread/start", {"cwd": str(workspace), "sandbox": "read-only",
                "approvalPolicy": "never", "ephemeral": True}, timeout=8)
            require_equal(thread["sandbox"], {"type": "readOnly", "networkAccess": False})
        finally:
            second.close()
        # A trusted directory may contain native project config. The trust
        # exception must not make its instruction layer an allowed Core input.
        (workspace / ".codex").mkdir(mode=0o700)
        (workspace / ".codex/config.toml").write_text('developer_instructions="synthetic injection"\n')
        third = client()
        try:
            third.initialize(client_name="solvio_layer_test", client_version="1")
            readback = third.request("config/read", {"cwd": str(workspace), "includeLayers": True}, timeout=8)
            require(any("developer_instructions" in layer.get("config", {}) for layer in readback["layers"]),
                    "project config injection was not exercised")
            refused(lambda: research_layers(readback, home))
            H.validate_config_text((home / "config.toml").read_text(), template, str(home))
        finally:
            third.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
