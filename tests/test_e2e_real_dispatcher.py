"""E2E-Test gegen den ECHTEN Hermes-Dispatcher (kein Mock des Dispatchers).

Zweck
-----
Der Router soll die Werkzeugflaeche eines Agenten im laufenden Hermes
verengen. Ein Unit-Test mit gemocktem Dispatcher beweist das nicht: der
entscheidende Punkt ist, dass `pre_llm_call` in Hermes auf einem
DAEMON-WORKER laeuft und der Aufrufer-Stack dort abgeschnitten ist. Nur
der echte Dispatcher erzeugt genau diese Lage.

Der Test laeuft deshalb gegen die echte Hermes-Installation:

    hermes_cli.plugins (Discovery) -> Plugin-Host -> register()
      -> manager._hooks['pre_llm_call'] / _middleware['tool_request']
      -> PluginDispatchMixin.invoke_hook -> _run_hook_callback_bounded
         (Daemon-Worker fuer BOUNDED Hooks)
      -> runtime.pre_llm_call -> _route_tool_surface

Beweisform: A/B-Vergleich.

    Fall A: Session-Map leer -> KEIN Routing (fail-open, volle Flaeche).
    Fall B: Session-Map gefuellt (Lernpunkt = echte tool_request-Middleware)
            -> Routing, Werkzeugflaeche verengt, floor_toolsets eingehalten.

A <-> B belegt, dass die session-gebundene Aufloesung wirkt. Ohne sie faellt
Fall B auf A zurueck, weil der Stack im Worker abgeschnitten ist.

Voraussetzungen
---------------
Eine Hermes-Installation. Der Pfad wird aus ``HERMES_AGENT_DIR`` gelesen,
sonst aus dem Default-Pfad des Nutzers. Fehlt Hermes, wird der Test
uebersprungen (``skip``) statt fehlgeschlagen - die Suite bleibt auf
Fremdsystemen lauffaehig.

Umgebungsvariablen
------------------
``TOOLSHED_E2E_HERMES_DIR``  Verzeichnis der Hermes-Installation
                             (``hermes_cli``/``agent`` darunter)
``TOOLSHED_E2E_HOME``        eigenes HERMES_HOME fuer den Testlauf
"""
import importlib
import importlib.util
import logging
import os
import sys
import threading
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO / "runtime"

DEFAULT_HERMES_DIRS = [
    Path.home() / ".hermes" / "hermes-agent",
    Path("/srv/companion/hermes_hugo/.hermes/hermes-agent"),
]


def _find_hermes_dir():
    env = os.environ.get("TOOLSHED_E2E_HERMES_DIR")
    if env:
        p = Path(env)
        if (p / "hermes_cli").is_dir():
            return p
        return None
    for cand in DEFAULT_HERMES_DIRS:
        if (cand / "hermes_cli").is_dir():
            return cand
    return None


HERMES_DIR = _find_hermes_dir()


def _make_router_registry(real_registry):
    """Registry-Sicht fuer den Router; Unbekanntes wird an Hermes' echte
    Registry delegiert, damit der echte Ladeweg intakt bleibt."""

    class _RouterRegistry:
        def get_registered_toolset_names(self):
            return ["terminal", "file", "web", "skills", "memory"]

        def get_tool_names_for_toolset(self, name):
            return {
                "terminal": ["terminal"],
                "file": ["read_file"],
                "web": ["web_search"],
                "skills": ["skill_view"],
                "memory": ["yantrikdb_recall"],
            }.get(name, [])

        def get_definitions(self, names, quiet=False):
            return [{"function": {"name": n}} for n in names]

        def __getattr__(self, item):
            if real_registry is None:
                raise AttributeError(item)
            return getattr(real_registry, item)

    return _RouterRegistry()


@unittest.skipUnless(HERMES_DIR, "keine Hermes-Installation gefunden "
                                 "(TOOLSHED_E2E_HERMES_DIR setzen)")
class TestE2ERealDispatcher(unittest.TestCase):
    """Der Router verengt die Werkzeugflaeche im echten Hermes-Pfad."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HERMES_DIR))
        os.environ["HERMES_HOME"] = os.environ.get("TOOLSHED_E2E_HOME") or str(REPO / ".e2e-home")

        # Hermes' echte Registry behalten; dem Router eine ergaenzte Sicht geben.
        real_registry = None
        try:
            real_registry = importlib.import_module("tools.registry").registry
        except Exception:
            pass
        reg_mod = types.ModuleType("tools.registry")
        setattr(reg_mod, "registry", _make_router_registry(real_registry))
        if not hasattr(sys.modules.get("tools"), "registry"):
            sys.modules.setdefault("tools", types.ModuleType("tools"))
        sys.modules["tools.registry"] = reg_mod

        from hermes_cli import plugins as h_plugins  # noqa: E402

        # Der Delivery-Manager laedt das Plugin als PAKET
        # (hermes_plugins.hermes_token_router). Es ist diese Instanz, die im
        # Betrieb arbeitet - und ihre State-Instanz muss der Test benutzen.
        # Ein selbst geladenes Flat-Modul waere eine ZWEITE, davon getrennte
        # State-Instanz: der Lernpunkt schriebe dann in die falsche.
        cls.manager = h_plugins._delivery_manager()
        cls.manager.discover_and_load()

        cls.module = None
        for name, mod in list(sys.modules.items()):
            if name.startswith("hermes_plugins.hermes_token_router") and \
                    hasattr(mod, "pre_llm_call"):
                cls.module = mod
                break
        if cls.module is None:
            raise unittest.SkipTest("Plugin wurde vom Delivery-Manager nicht geladen")

        cls.state = None
        for name, mod in list(sys.modules.items()):
            if name.startswith("hermes_plugins.hermes_token_router") and \
                    name.endswith(".state") and hasattr(mod, "_get_agent_ref"):
                cls.state = mod
                break
        if cls.state is None:
            raise unittest.SkipTest("State-Modul der geladenen Plugin-Instanz nicht gefunden")

    def _fake_agent(self, session_id):
        class FakeAgent:
            def __init__(self):
                self.session_id = session_id
                self.tools = [{"function": {"name": n}} for n in
                              ("read_file", "terminal", "web_search", "browser_exec",
                               "delegate_task", "cronjob_manage")]
                self.enabled_toolsets = ["terminal", "file", "web", "skills", "memory",
                                         "browser", "delegation", "cronjob"]
                self.valid_tool_names = {"read_file", "terminal", "web_search",
                                         "browser_exec", "delegate_task",
                                         "cronjob_manage"}
                self.model = "test-model"
                self.platform = "test"
                self._current_turn_id = "turn-1"

        return FakeAgent()

    def _invoke(self, session_id):
        self.manager.invoke_hook(
            "pre_llm_call",
            session_id=session_id, task_id="task-e2e", turn_id="turn-1",
            user_message="read the file /tmp/probe.txt", conversation_history=[],
            is_first_turn=True, model="test-model", platform="test",
        )

    def setUp(self):
        setattr(self.state, "_agent_ref", None)
        setattr(self.state, "_agent_refs", {})
        self.state._reset_missing_agent_notice()

    def test_without_mapping_no_routing_happens(self):
        """Fall A: ohne Zuordnung bleibt die Werkzeugflaeche voll (fail-open)."""
        agent = self._fake_agent("sess-A")
        vorher = {t["function"]["name"] for t in agent.tools}
        sets_vorher = set(agent.enabled_toolsets)

        self._invoke("sess-A")

        nachher = {t["function"]["name"] for t in agent.tools}
        self.assertEqual(vorher, nachher)
        self.assertEqual(sets_vorher, set(agent.enabled_toolsets))

    def test_middleware_learns_the_session_mapping(self):
        """Der Lernpunkt sitzt in der tool_request-Middleware."""
        from hermes_cli.middleware import apply_tool_request_middleware

        agent = self._fake_agent("sess-L")

        def middleware_aufrufer(agent):
            # 'agent' ist ein Parameter dieses Frames -> Stack-Walk findet ihn.
            # So ist es im Betrieb (tool_executor ruft die Middleware inline auf).
            apply_tool_request_middleware(
                "read_file", {"path": "/tmp/nonexistent-e2e-probe.txt"},
                session_id=agent.session_id, task_id="task-e2e", turn_id="turn-1",
            )

        middleware_aufrufer(agent)
        self.assertIs(self.state._get_agent_ref("sess-L"), agent)

    def test_with_mapping_the_surface_is_narrowed(self):
        """Fall B: mit Zuordnung routet der Router im echten Dispatcher."""
        from hermes_cli.middleware import apply_tool_request_middleware

        agent = self._fake_agent("sess-B")
        vorher = {t["function"]["name"] for t in agent.tools}

        def middleware_aufrufer(agent):
            apply_tool_request_middleware(
                "read_file", {"path": "/tmp/nonexistent-e2e-probe.txt"},
                session_id=agent.session_id, task_id="task-e2e", turn_id="turn-1",
            )

        middleware_aufrufer(agent)
        self._invoke("sess-B")

        nachher = {t["function"]["name"] for t in agent.tools}
        sets = set(agent.enabled_toolsets)
        self.assertLess(len(nachher), len(vorher),
                        f"die Werkzeugflaeche wurde nicht verengt: {sorted(nachher)}")
        # floor_toolsets bleiben erhalten
        self.assertTrue({"terminal", "file", "skills", "memory", "web"}.issubset(sets),
                        f"der Floor wurde verletzt: {sorted(sets)}")
        # Tools ausserhalb des Floors sind entfernt
        self.assertFalse({"browser_exec", "delegate_task", "cronjob_manage"} & nachher),
        self.assertFalse({"browser", "delegation", "cronjob"} & sets),
        self.assertNotIn("browser_exec", nachher)

    def test_hook_runs_on_a_worker_thread(self):
        """Der Hook laeuft NICHT im MainThread - der Grund fuer den Fix."""
        agent = self._fake_agent("sess-T")
        threads = []
        original = self.module.pre_llm_call

        def wrapped(**kwargs):
            threads.append(threading.current_thread().name)
            return original(**kwargs)

        self.manager._hooks["pre_llm_call"] = [wrapped]
        try:
            self._invoke("sess-T")
        finally:
            self.manager._hooks["pre_llm_call"] = [original]

        self.assertTrue(threads, "der Hook wurde nicht aufgerufen")
        self.assertTrue(all(t != "MainThread" for t in threads),
                        f"der Hook lief im MainThread: {threads}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
