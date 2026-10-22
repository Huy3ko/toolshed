"""Vertragstest: die Middleware erfasst die Host-Autorisierung selbst.

GEMESSENER BEFUND (23.10.2026, laufender Hermes v0.22.2): Der Hook
`pre_llm_call` laeuft auf einem Daemon-Worker-Thread (plugins_dispatch.py:271/333);
dort ist der Agent-Stack abgeschnitten, der Hook findet keinen Agenten und
legt deshalb am Agent KEINEN Router-State an.

Die Middleware laeuft INLINE und hat den Agenten im Stack. Sie muss den
State deshalb selbst holen und die Host-Freigabe selbst erfassen - sonst
bleibt `authorized_toolsets = None` und die Middleware bleibt immer im
fail-open: die Verengung fand auch nach dem Import-Fix nicht statt.

Der Test simuliert genau das: ein Agent OHNE Router-State am Agent
(der Hook lief nie) und prueft, dass die Middleware sich den State selbst
besorgt und die Autorisierung erfasst.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "src" / "toolshed"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def host_package(monkeypatch):
    """Laedt das Plugin exakt so, wie der Host es laedt (Paketkontext)."""
    for entry in list(sys.path):
        if not entry:
            continue
        try:
            resolved = str(Path(entry).resolve())
        except OSError:
            continue
        if resolved in (str(PLUGIN_ROOT.resolve()), str(REPO_ROOT.resolve()),
                        str((REPO_ROOT / "src").resolve()),
                        str((REPO_ROOT / "runtime").resolve())):
            sys.path.remove(entry)
    for name in ("config", "policy", "state", "router_tools"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    ns_parent = "hermes_plugins"
    if ns_parent not in sys.modules:
        ns = types.ModuleType(ns_parent)
        ns.__path__ = []
        ns.__package__ = ns_parent
        sys.modules[ns_parent] = ns

    full = f"{ns_parent}.ts_authz_probe"
    spec = importlib.util.spec_from_file_location(
        full, str(PLUGIN_ROOT / "__init__.py"),
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = full
    mod.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[full] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(full, None)
        raise
    yield full, mod
    sys.modules.pop(full, None)
    for name in list(sys.modules):
        if name.startswith(f"{full}."):
            sys.modules.pop(name, None)


class _AgentWithoutRouterState:
    """Agent, wie ihn der Host baut - ohne Router-State (der Hook lief nie)."""

    def __init__(self):
        self.tools = ["terminal", "web_search"]
        self.valid_tool_names = ["terminal", "web_search"]
        self.enabled_toolsets = ["terminal", "file", "web"]
        self.session_id = "authz-probe"


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": name}}


def test_middleware_creates_router_state_when_hook_never_ran(host_package):
    """Kernvertrag: die Middleware besorgt sich den State selbst.

    Vor der Korrektur stand hier `state = getattr(agent, ROUTER_STATE_ATTR, None)`
    -> None, sobald der Hook nicht gelaufen war. Die Autorisierung konnte
    dann nie erfasst werden und die Middleware blieb im fail-open.
    """
    full, _mod = host_package
    sub = importlib.import_module(f"{full}.llm_request_routing")
    state_mod = importlib.import_module(f"{full}.state")

    agent = _AgentWithoutRouterState()
    assert getattr(agent, state_mod.ROUTER_STATE_ATTR, None) is None, (
        "Voraussetzung: der Agent hat noch keinen Router-State"
    )

    request = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Lies die Datei /tmp/x."}],
        "tools": [_tool("terminal"), _tool("web_search")],
    }

    # Agent im Stack: so findet _find_agent_in_stack() ihn.
    # Der Rueckgabewert wird hier nicht geprueft - der Aufruf selbst ist der
    # Gegenstand: er muss den State am Agenten anlegen.
    _result = sub.llm_request_middleware(
        request=request,
        original_request=dict(request),
        session_id="authz-probe",
    )

    state = getattr(agent, state_mod.ROUTER_STATE_ATTR, None)
    assert isinstance(state, state_mod.RouterState), (
        "Die Middleware hat keinen Router-State am Agent angelegt - "
        "der Hook-Pfad laeuft im Worker-Thread und kann das nicht."
    )
    assert state.authorization_captured is True, (
        "Die Middleware hat die Host-Autorisierung nicht erfasst - "
        "ohne sie bleibt sie dauerhaft im fail-open."
    )
    assert state.authorized_toolsets == {"terminal", "file", "web"}


def test_middleware_never_widens_beyond_host_authorization(host_package):
    """Die gewaehlte Flaeche darf die Host-Freigabe nie ueberschreiten.

    Der Request bringt nur `terminal` und `web_search` mit; die Autorisierung
    des Hosts nennt `terminal`, `file` und `web`. Die Middleware darf nur
    behalten, was BEIDES hergibt.
    """
    full, _mod = host_package
    sub = importlib.import_module(f"{full}.llm_request_routing")
    state_mod = importlib.import_module(f"{full}.state")

    agent = _AgentWithoutRouterState()
    agent.enabled_toolsets = ["terminal"]  # Host gibt NUR terminal frei

    request = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Lies die Datei /tmp/x."}],
        "tools": [_tool("terminal"), _tool("web_search")],
    }

    result = sub.llm_request_middleware(
        request=request,
        original_request=dict(request),
        session_id="authz-probe",
    )

    state = getattr(agent, state_mod.ROUTER_STATE_ATTR, None)
    assert state is not None
    assert state.authorized_toolsets == {"terminal"}

    if isinstance(result, dict) and isinstance(result.get("request"), dict):
        kept = {t["function"]["name"]
                for t in result["request"].get("tools", [])
                if isinstance(t, dict)}
        assert kept <= {"terminal"}, (
            f"Die Middleware behaeltt Werkzeuge ausserhalb der Host-Freigabe: {kept}"
        )
