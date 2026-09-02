from __future__ import annotations

import re
import sys
import types
import os
from pathlib import Path

import pytest

import runtime
from runtime.learning_store.shadow_hooks import ShadowHooks


ROOT = Path(__file__).resolve().parents[1]


def _configured_hermes_root() -> Path:
    value = os.environ.get("HERMES_ROOT")
    if not value:
        pytest.skip("set HERMES_ROOT to run the host-loader integration probe")
    hermes_root = Path(value).expanduser()
    registry = hermes_root / "tools" / "registry.py"
    if not registry.is_file() or not os.access(registry, os.R_OK):
        pytest.skip("configured Hermes source tree is unavailable")
    return hermes_root


def _declared_version(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    for pattern in (
        r"^version:\s*([0-9A-Za-z_.-]+)\s*$",
        r"^__version__\s*=\s*[\"']([^\"']+)[\"']\s*$",
        r"^version\s*=\s*[\"']([^\"']+)[\"']\s*$",
    ):
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            return match.group(1)
    raise AssertionError(f"no release version found in {path}")


def test_all_release_surfaces_use_v017():
    paths = [
        ROOT / "pyproject.toml",
        ROOT / "plugin.yaml",
        ROOT / "runtime" / "plugin.yaml",
        ROOT / "__about__.py",
        ROOT / "runtime" / "__about__.py",
        ROOT / "src" / "toolshed" / "__about__.py",
        ROOT / "src" / "toolshed" / "plugin.yaml",
    ]
    assert {_declared_version(path) for path in paths} == {"0.1.7"}


def test_config_surfaces_are_valid_and_identical():
    import yaml

    paths = [
        ROOT / "config.yaml",
        ROOT / "runtime" / "config.yaml",
        ROOT / "src" / "toolshed" / "config.yaml",
        ROOT / "config.template.yaml",
        ROOT / "runtime" / "config.template.yaml",
        ROOT / "src" / "toolshed" / "config.template.yaml",
    ]
    parsed = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]
    assert all(isinstance(value, dict) for value in parsed)
    assert all(value == parsed[0] for value in parsed[1:])
    assert parsed[0]["global"]["mode"] == "active"
    assert parsed[0]["profiles"] == {}


def test_ci_secret_scan_excludes_non_shipped_adr_documentation():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "src/ README.md adr/" not in workflow
    assert "src/ README.md" in workflow


def test_wheel_runtime_dependencies_and_package_relative_shadow_import():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    shadow = (ROOT / "src" / "toolshed" / "learning_store" / "shadow_hooks.py").read_text(
        encoding="utf-8"
    )
    assert "dependencies = [\"PyYAML>=6\"]" in pyproject
    assert "from ..telemetry_store.events import" in shadow


def test_install_and_update_json_output_is_a_valid_array():
    for name in ("install.sh", "update.sh"):
        script = (ROOT / "runtime" / name).read_text(encoding="utf-8")
        assert 'RESULT_LOG="${RESULT_LOG},\\n"' in script
        assert 'printf "[\\n%b\\n]\\n" "$RESULT_LOG"' in script


def test_profile_resolution_uses_context_local_hermes_home(monkeypatch, tmp_path):
    from runtime import config

    profile_home = tmp_path / "profiles" / "context-agent"
    profile_home.mkdir(parents=True)
    hermes_constants = types.ModuleType("hermes_constants")
    hermes_constants.get_hermes_home = lambda: profile_home
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes_constants)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_ACTIVE_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    resolved = config._get_profile_config({
        "global": {"enabled": False},
        "profiles": {"context-agent": {"enabled": True}},
    })
    assert resolved["_profile_name"] == "context-agent"
    assert resolved["enabled"] is True


def test_session_end_finalizes_turn_block_without_dropping_live_agent(monkeypatch):
    calls = []
    dropped = []
    monkeypatch.setattr(runtime, "get_shadow", lambda: types.SimpleNamespace(
        finalize=lambda *args, **kwargs: calls.append((args, kwargs))
    ))
    monkeypatch.setattr(runtime, "_drop_agent_ref", lambda session_id: dropped.append(session_id))

    runtime.on_session_end(session_id="session", turn_id="turn-1")

    assert calls == [(('session',), {"block_id": "turn-1"})]
    assert dropped == []


def test_doctor_checks_shadow_state_under_active_profile_home():
    doctor = (ROOT / "runtime" / "doctor.sh").read_text(encoding="utf-8")
    assert 'if [ "$PROFILE" = "default" ]; then SHADOW_HOME="$HERMES_DIR"' in doctor
    assert 'SHADOW_HOME="$HERMES_DIR/profiles/$PROFILE"' in doctor
    assert 'STATEDIR="$SHADOW_HOME/learning"' in doctor


def test_shadow_mode_never_mutates_effective_tool_surface(monkeypatch):
    agent = types.SimpleNamespace(
        session_id="shadow-session",
        tools=[{"function": {"name": "terminal"}}],
        valid_tool_names={"terminal"},
        enabled_toolsets=["terminal"],
    )
    original_tools = list(agent.tools)
    attempted_apply = False

    def must_not_apply(*args, **kwargs):
        nonlocal attempted_apply
        attempted_apply = True
        raise AssertionError("shadow mode applied a route")

    monkeypatch.setattr(runtime, "_load_config", lambda: {"global": {"enabled": True}})
    monkeypatch.setattr(runtime, "_is_router_active", lambda cfg: True)
    monkeypatch.setattr(
        runtime,
        "_get_profile_config",
        lambda cfg: {
            "enabled": True,
            "mode": "shadow",
            "floor_toolsets": [],
            "deterministic_rules_enabled": True,
            "_profile_name": "default",
        },
    )
    monkeypatch.setattr(runtime, "_get_available_toolsets", lambda: {"web"})
    monkeypatch.setattr(
        runtime,
        "_predict_toolsets_by_rules",
        lambda message, available: ({"web"}, "test"),
    )
    monkeypatch.setattr(runtime, "get_shadow", lambda: None)
    monkeypatch.setattr(runtime, "_apply_predicted_tools", must_not_apply)

    runtime._route_tool_surface(
        "pre_llm_call",
        agent=agent,
        session_id=agent.session_id,
        user_message="research this",
    )

    assert attempted_apply is False
    assert agent.tools == original_tools
    assert agent.valid_tool_names == {"terminal"}


def test_recovery_reports_failure_when_registry_is_unavailable(monkeypatch):
    class BrokenRegistry:
        def get_registered_toolset_names(self):
            raise RuntimeError("registry unavailable")

        def get_tool_names_for_toolset(self, name):
            raise RuntimeError("registry unavailable")

    fake_registry_module = types.ModuleType("tools.registry")
    fake_registry_module.registry = BrokenRegistry()
    monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_module)

    agent = types.SimpleNamespace(
        session_id="recovery-session",
        tools=[{"function": {"name": "terminal"}}],
        valid_tool_names={"terminal"},
        enabled_toolsets=["terminal"],
    )
    runtime._store_agent_ref(agent, agent.session_id)

    result = runtime.request_toolset_handler(
        {"toolset": "web"},
        session_id=agent.session_id,
    )

    assert '"ok": false' in result
    assert '"available_after": false' in result


def test_recovery_rejects_toolset_outside_original_agent_grant(monkeypatch):
    class Entry:
        toolset = "web"

    class Registry:
        def get_registered_toolset_names(self):
            return ["terminal", "web"]

        def get_toolset_alias_target(self, name):
            return None

        def get_entry(self, name):
            return Entry() if name == "web_search" else None

        def get_tool_names_for_toolset(self, name):
            return ["web_search"] if name == "web" else ["terminal_exec"]

    fake_registry_module = types.ModuleType("tools.registry")
    fake_registry_module.registry = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", fake_registry_module)

    agent = types.SimpleNamespace(
        session_id="restricted-session",
        tools=[{"function": {"name": "terminal_exec"}}],
        valid_tool_names={"terminal_exec"},
        enabled_toolsets=["terminal"],
    )
    runtime._store_agent_ref(agent, agent.session_id)

    result = runtime.request_toolset_handler(
        {"toolset": "web"},
        session_id=agent.session_id,
    )

    assert '"ok": false' in result
    assert '"reason": "authorization_denied"' in result
    assert agent.valid_tool_names == {"terminal_exec"}


def test_shadow_records_one_session_usage_for_repeated_same_toolset(tmp_path):
    hooks = ShadowHooks(
        enabled=True,
        store_path=tmp_path / "profiles.json",
        events_path=tmp_path / "events.jsonl",
    )
    calls = []
    hooks.store.record = lambda *args, **kwargs: calls.append((args, kwargs))
    hooks.store.save = lambda: None
    hooks.predictor.predict = lambda signature: {
        "predicted": [],
        "profile_hits": 0,
    }

    hooks.on_turn("session", intent="research")
    hooks.on_tool_used("session", "web")
    hooks.on_tool_used("session", "web")
    hooks.finalize("session")

    usage_records = [kwargs for _, kwargs in calls if "actually_used" in kwargs]
    assert len(usage_records) == 1
    assert usage_records[0]["actually_used"] == ["web"]


def test_shadow_ignores_failed_tool_calls_and_tracks_blocks(tmp_path):
    hooks = ShadowHooks(
        enabled=True,
        store_path=tmp_path / "profiles.json",
        events_path=tmp_path / "events.jsonl",
    )
    hooks.predictor.predict = lambda signature: {
        "predicted": ["web"],
        "profile_hits": 0,
    }
    hooks.on_turn("session", block_id="turn-1", intent="research")
    hooks.on_tool_used("session", "web", block_id="turn-1", status="error")
    hooks.finalize("session", block_id="turn-1")

    event = hooks.events.read()[0]
    assert event.actual == []


def test_shadow_enabled_without_confined_paths_fails_closed():
    hooks = ShadowHooks(enabled=True)
    assert hooks.enabled is False


def test_runtime_payload_has_valid_self_contained_config():
    import yaml

    template = ROOT / "runtime" / "config.template.yaml"
    config = ROOT / "runtime" / "config.yaml"
    assert config.exists()
    template_data = yaml.safe_load(template.read_text(encoding="utf-8"))
    config_data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert template_data["global"]["mode"] == "active"
    assert config_data["global"]["enabled"] is False
    assert config_data["global"]["mode"] == "active"


def test_runtime_installer_targets_runtime_payload_and_checks_install():
    installer = (ROOT / "runtime" / "install.sh").read_text(encoding="utf-8")
    assert 'REPO="Huy3ko/toolshed/runtime"' in installer
    assert 'set -u -o pipefail' in installer
    assert 'if OUT=$("$HERMES_BIN"' in installer
    assert 'INSTALL_RC=$?' in installer
    assert '/home/$(whoami)' not in installer
    assert "validate_profile" in installer


def test_runtime_manifest_declares_only_current_hermes_hook_surface():
    manifest = (ROOT / "runtime" / "plugin.yaml").read_text(encoding="utf-8")
    assert "  - pre_turn_context_build" not in manifest
    assert "  - pre_llm_call" in manifest
    assert "  - post_tool_call" in manifest
    assert "  - on_session_end" in manifest


def test_package_import_does_not_shadow_hermes_tools_package():
    import subprocess
    import sys

    hermes_root = _configured_hermes_root()
    script = "import runtime; import tools.registry; print('ok')"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(hermes_root), str(ROOT)))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_router_registration_uses_current_hermes_fallback_hook(monkeypatch):
    registered = []

    class Context:
        def register_hook(self, name, callback):
            registered.append((name, callback))

        def register_middleware(self, name, callback):
            registered.append((name, callback))

    fake_plugins = types.ModuleType("hermes_cli.plugins")
    fake_plugins.VALID_HOOKS = {"pre_llm_call", "post_tool_call", "on_session_end"}
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", fake_plugins)
    monkeypatch.setattr(runtime, "build_recovery_tool_schema", lambda names: {})
    monkeypatch.setattr(runtime, "get_shadow", lambda: None)

    class BrokenRegistry:
        def get_registered_toolset_names(self):
            return []

        def register(self, **kwargs):
            pass

    fake_registry = types.ModuleType("tools.registry")
    fake_registry.registry = BrokenRegistry()
    monkeypatch.setitem(sys.modules, "tools.registry", fake_registry)

    runtime._registration_checked = False
    runtime.register(Context())

    names = [name for name, _ in registered]
    assert "pre_turn_context_build" not in names
    assert "pre_llm_call" in names
    assert "tool_request" in names


def test_doctor_accepts_all_router_modes():
    doctor = (ROOT / "runtime" / "doctor.sh").read_text(encoding="utf-8")
    assert 'MODE" = "off"' in doctor
    assert 'MODE" = "active"' in doctor
    assert 'MODE" = "shadow"' in doctor


def test_updater_preserves_router_mode_and_fails_closed_on_pipeline_errors():
    updater = (ROOT / "runtime" / "update.sh").read_text(encoding="utf-8")
    assert "set -u -o pipefail" in updater
    assert 'OLD_MODE=$(grep -m1 \'^  mode:\'' in updater
    assert "expansion_mode" not in updater




def test_shadow_state_paths_are_anchored_and_confined(tmp_path):
    from runtime.learning_store.shadow_hooks import _resolve_shadow_path

    config_path = tmp_path / "plugin" / "config.yaml"
    config_path.parent.mkdir()
    config_path.touch()

    assert _resolve_shadow_path(str(config_path), "learning/profiles.json") == str(
        config_path.parent / "learning" / "profiles.json"
    )
    assert _resolve_shadow_path(str(config_path), "../outside.json") == ""


def test_flat_plugin_loader_registers_recovery_without_tools_module_collision(tmp_path):
    import subprocess
    import sys

    hermes_root = _configured_hermes_root()

    script = r'''
import importlib.util
import sys
import types
from pathlib import Path

plugin_dir = Path(sys.argv[1]).resolve()
hermes_root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(plugin_dir))
sys.path.insert(1, str(hermes_root))
namespace = types.ModuleType("hermes_plugins")
namespace.__path__ = []
sys.modules["hermes_plugins"] = namespace
name = "hermes_plugins.hermes_token_router"
spec = importlib.util.spec_from_file_location(
    name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
)
module = importlib.util.module_from_spec(spec)
module.__package__ = name
module.__path__ = [str(plugin_dir)]
sys.modules[name] = module
spec.loader.exec_module(module)

class Context:
    def __init__(self):
        self.hooks = []
        self.middlewares = []

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_middleware(self, name, callback):
        self.middlewares.append(name)

ctx = Context()
module.register(ctx)
assert module._recovery_tool_registered, "recovery tool was not registered"
assert "tool_request" in ctx.middlewares
print("recovery-registration-ok")
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "runtime"), str(hermes_root)],
        cwd="/tmp",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "recovery-registration-ok"


def test_agent_reference_collision_fails_closed():
    import types

    from runtime import state

    state._agent_ref = None
    state._agent_refs.clear()
    first = types.SimpleNamespace(session_id="same-session")
    second = types.SimpleNamespace(session_id="same-session")

    state._store_agent_ref(first, first.session_id)
    state._store_agent_ref(second, second.session_id)

    assert state._get_agent_ref(first.session_id) is None

    state._drop_agent_ref(first.session_id)
    state._agent_ref = None


def test_rollback_verification_only_requires_v2_marker_absence_for_v1_source():
    updater = (ROOT / "runtime" / "update.sh").read_text(encoding="utf-8")
    assert '[ "$LAYOUT" = "v1" ] && grep -q' in updater
    assert 'layout_version: 2$' in updater


def test_pre_llm_fallback_keeps_session_sticky_surface(monkeypatch):
    import types

    agent = types.SimpleNamespace(
        session_id="sticky-session",
        tools=[{"function": {"name": "terminal"}}],
        valid_tool_names={"terminal"},
        enabled_toolsets=["terminal"],
    )
    calls = []

    monkeypatch.setattr(runtime, "_registration_checked", False)
    monkeypatch.setattr(runtime, "_load_config", lambda: {"global": {"enabled": True}})
    monkeypatch.setattr(runtime, "_is_router_active", lambda cfg: True)
    monkeypatch.setattr(
        runtime,
        "_get_profile_config",
        lambda cfg: {
            "enabled": True,
            "mode": "active",
            "floor_toolsets": [],
            "deterministic_rules_enabled": True,
            "_profile_name": "default",
        },
    )
    monkeypatch.setattr(runtime, "_get_available_toolsets", lambda: {"web"})
    monkeypatch.setattr(
        runtime,
        "_predict_toolsets_by_rules",
        lambda message, available: ({"web"}, "test"),
    )
    monkeypatch.setattr(runtime, "_apply_predicted_tools", lambda *args: calls.append(args))
    monkeypatch.setattr(runtime, "get_shadow", lambda: None)

    runtime._route_tool_surface(
        "pre_llm_call", agent=agent, session_id=agent.session_id,
        turn_id="turn-1", user_message="research this",
    )
    runtime._route_tool_surface(
        "pre_llm_call", agent=agent, session_id=agent.session_id,
        turn_id="turn-2", user_message="research this again",
    )

    assert len(calls) == 1


def test_shadow_post_tool_call_records_usage_when_router_is_observe_only(monkeypatch):
    import sys
    import types

    agent = types.SimpleNamespace(session_id="shadow-usage-session")
    state = runtime._get_router_state(agent)
    state.reset()
    used = []
    shadow = types.SimpleNamespace(on_tool_used=lambda session, toolset: used.append((session, toolset)))
    registry_module = types.ModuleType("tools.registry")
    registry_module.registry = object()

    monkeypatch.setattr(runtime, "_get_agent_ref", lambda session_id: agent)
    monkeypatch.setattr(runtime, "get_shadow", lambda: shadow)
    monkeypatch.setattr(runtime, "_infer_toolset_from_tool", lambda name, registry: "web")
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)

    runtime.post_tool_call(
        session_id=agent.session_id,
        tool_name="web_search",
    )

    assert used == [(agent.session_id, "web")]


def test_doctor_rejects_traversal_profile_before_path_use():
    import os
    import subprocess

    hermes_home = ROOT / "tests" / ".doctor-hermes-home"
    hermes_home.mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(ROOT / "runtime" / "doctor.sh"),
         "--home", str(hermes_home), "--profile", "../escape"],
        cwd=str(ROOT),
        env={**os.environ, "HOME": str(hermes_home.parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 4
    assert "invalid profile name" in result.stderr


def test_wheel_declares_runtime_config_package_data():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.setuptools.package-data]" in pyproject
    assert 'toolshed = [' in pyproject
    assert '"config.yaml"' in pyproject
    assert '"plugin.yaml"' in pyproject
    assert '"config.template.yaml"' in pyproject


def test_updater_checks_grant_in_the_selected_profile_home():
    updater = (ROOT / "runtime" / "update.sh").read_text(encoding="utf-8")
    assert 'GRANT_CFG="$TH/config.yaml"' in updater
    assert 'GRANT_CFG="$TH/profiles/$P/config.yaml"' in updater


def test_installer_verifies_enabled_state_after_mutation():
    installer = (ROOT / "runtime" / "install.sh").read_text(encoding="utf-8")
    assert "EN_AFTER" in installer
    assert "verify-enabled" in installer


def test_doctor_treats_explicit_dot_hermes_home_as_direct_home():
    doctor = (ROOT / "runtime" / "doctor.sh").read_text(encoding="utf-8")
    assert 'basename "$HERMES_HOME"' in doctor
