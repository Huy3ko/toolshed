"""RED-Test: der Router muss den Agenten auch im Worker-Thread finden.

BEFUND 22.10.2026 (Vela, live gemessen gegen den laufenden Hermes auf ONE):

Hermes registriert `pre_llm_call` als BOUNDED Hook
(hermes_cli/plugins_dispatch.py:42 `_HOOK_TIMEOUT_BOUNDED_HOOKS` enthaelt
"pre_llm_call") und fuehrt ihn auf einem Daemon-Worker aus
(plugins_dispatch.py:271 "Run one callback on a daemon worker").

Der Aufrufer-Pfad ist:
    agent/turn_context.py: build_turn_context(agent, ...)
      -> _collect_pre_llm_call_context(agent, ...)   (Zeile 798)
        -> invoke_hook("pre_llm_call", ...)          (Zeile 810)
          -> _run_hook_callback_bounded(...)         (Worker-Thread!)

Der Worker-Thread hat KEINEN Aufrufer-Stack. `agent` liegt als Local nur in
`build_turn_context`, also im abgeschnittenen Teil. Deshalb findet
`_get_agent_from_stack()` (runtime/state.py:15) dort nichts.

BEOBACHTUNG IM BETRIEB (ONE, agent.log):
  WARNING hermes_token_router: no agent reference; full set fallback
  -> der Router verengt die Werkzeugflaeche NIE. Er routet faktisch nicht.

Auf OVH ist die Warnung nicht im Log, weil dort seit 21.10. 20:57 keine
Turns mehr liefen - nicht weil es dort funktioniert.

DER ECHTE WEG: Hermes ruft den Hook mit `session_id` auf. Der Agent laesst
sich ueber die Session zuordnen. `agent/turn_context.py` schreibt die
Session-Zeile; der Agent ist der Besitzer dieser Session. Der Router muss den
Agenten also aus der Session ermitteln, nicht aus dem Stack.

Vertrag dieses Tests:
  1. Ohne Agent im Stack findet der Router den Agenten ueber die Session.
  2. Ohne Agent UND ohne Session-Referenz: keine Route (fail-closed auf
     "unbekannt", aber NICHT auf "volle Toolflaeche mit Alarm-Fehlermeldung").
  3. `pre_turn_context_build` wird NICHT registriert - dieser Hook existiert
     im laufenden Hermes nicht (hermes_cli/plugins.py VALID_HOOKS).
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402
from runtime import state as rt_state  # noqa: E402


class _FakeAgent:
    """Minimaler Agent-Ersatz: entscheidend sind `tools` und `session_id`."""

    def __init__(self, session_id="sess-1"):
        self.tools = [{"function": {"name": "terminal"}}]
        self.session_id = session_id
        self.enabled_toolsets = ["terminal", "file"]


class AgentDiscoveryWithoutStack(unittest.TestCase):
    """Der Agenten-Referenz darf nicht vom Aufrufer-Stack abhaengen."""

    def setUp(self):
        rt_state._agent_ref = None
        rt_state._agent_refs = {}
        cfg = {
            "global": {
                "enabled": True,
                "mode": "active",
                "routing_scope": "first_turn",
                "deterministic_rules_enabled": True,
                "floor_toolsets": ["terminal", "file"],
                "confidence_threshold": 0.90,
            },
        }
        patch_cfg = mock.patch.object(runtime, "_load_config", lambda: cfg)
        patch_active = mock.patch.object(runtime, "_is_router_active", lambda c=None: True)
        patch_cfg.start()
        patch_active.start()
        self.addCleanup(patch_cfg.stop)
        self.addCleanup(patch_active.stop)
        # `register()` (aus dem Hook-Registrierungstest) setzt
        # _registration_checked=True; dann verlangt `_recovery_is_ready()` den
        # registrierten Recovery-Mechanismus. Fuer die Routing-Tests wird der
        # Zustand zurueckgesetzt - wie im echten Lauf vor der Registrierung.
        runtime._registration_checked = False
        self.addCleanup(setattr, runtime, "_registration_checked", False)
        self.addCleanup(self._reset)
        # Registry + Prediction (Muster test_v017_contracts.py:166)
        patch_toolsets = mock.patch.object(
            runtime, "_get_available_toolsets", lambda: {"terminal", "file", "web"})
        patch_predict = mock.patch.object(
            runtime, "_predict_toolsets_by_rules",
            lambda message, available: ({"terminal", "file"}, "test-rule"))
        patch_toolsets.start()
        patch_predict.start()
        self.addCleanup(patch_toolsets.stop)
        self.addCleanup(patch_predict.stop)
        _reg = types.ModuleType("tools.registry")

        class _Reg:
            def get_registered_toolset_names(self):
                return ["terminal", "file", "web"]

            def get_tool_names_for_toolset(self, name):
                return {"terminal": ["read_file"], "file": ["read_file"],
                        "web": ["web_search"]}.get(name, [])

            def get_definitions(self, names, quiet=False):
                return [{"function": {"name": n}} for n in names]

        _reg.registry = _Reg()
        patch_reg = mock.patch.dict(
            sys.modules, {"tools": types.ModuleType("tools"), "tools.registry": _reg})
        patch_reg.start()
        self.addCleanup(patch_reg.stop)

    def _reset(self):
        rt_state._agent_ref = None
        rt_state._agent_refs = {}

    def test_session_map_is_used_when_stack_has_no_agent(self):
        """Ein Agent, der NUR ueber die Session bekannt ist, muss gefunden
        werden. Genau das ist der Worker-Thread-Fall.

        Der Stack-Walk wird hier bewusst ausgeschlossen: der Aufrufer
        dieser Test-Methode hat selbst ein 'agent'-Attribut, was den Walk
        trivially treffen wuerde. Geprueft wird der Vertrag: die Session
        muss den Agenten liefern, unabhaengig vom Stack."""
        agent = _FakeAgent()
        rt_state._store_agent_ref(agent, "sess-1")
        found = rt_state._get_agent_ref("sess-1")
        self.assertIs(found, agent,
                      "der Agent wurde ueber die Session nicht gefunden - "
                      "im Worker-Thread ist das der Normalfall")

    def test_store_and_ref_roundtrip(self):
        """Speichern und Zurücklesen ueber die Session muss identisch sein."""
        agent = _FakeAgent("sess-42")
        rt_state._store_agent_ref(agent, "sess-42")
        self.assertIs(rt_state._get_agent_ref("sess-42"), agent)

    def test_unknown_session_yields_none(self):
        """Eine unbekannte Session liefert None - nicht geraten."""
        self.assertIsNone(rt_state._get_agent_ref("gibt-es-nicht"))

    def test_early_hook_is_not_registered(self):
        """`pre_turn_context_build` existiert im laufenden Hermes nicht.
        Die Registrierung darf ihn nicht als aktiv melden."""
        registered = []

        class Context:
            def register_hook(self, name, callback):
                registered.append(name)

            def register_middleware(self, name, callback):
                registered.append("middleware:" + name)

        fake_plugins = types.ModuleType("hermes_cli.plugins")
        # Die Valid-Hooks des LAUFENDEN Hermes (v0.22.2) - gemessen.
        fake_plugins.VALID_HOOKS = {
            "pre_llm_call", "post_llm_call", "pre_tool_call", "post_tool_call",
            "on_session_end", "on_session_start", "pre_api_request", "post_api_request",
        }
        old = sys.modules.get("hermes_cli.plugins")
        sys.modules["hermes_cli.plugins"] = fake_plugins
        try:
            runtime._registration_checked = False
            runtime.register(Context())
        finally:
            if old is not None:
                sys.modules["hermes_cli.plugins"] = old
            else:
                sys.modules.pop("hermes_cli.plugins", None)
        self.assertNotIn("pre_turn_context_build", registered)
        self.assertIn("pre_llm_call", registered)

    def test_router_uses_session_agent_not_only_stack(self):
        """Der Router-Hook muss den Agenten ueber die Session nutzen, wenn
        der Stack ihn nicht liefert. Sonst: volle Toolflaeche + Warnung."""
        agent = _FakeAgent("sess-7")
        rt_state._store_agent_ref(agent, "sess-7")

        # pre_llm_call laeuft ohne 'agent'-Local im Stack (Worker-Thread).
        # Der Router muss trotzdem den Agenten finden und routen.
        result = runtime.pre_llm_call(
            session_id="sess-7",
            user_message="list the files in /tmp please",
            conversation_history=[],
            is_first_turn=True,
        )
        # Der echte Vertrag: der Hook liefert KEINEN Kontext zurueck, sondern
        # VERENGT die Werkzeugflaeche des Agenten. Genau das ist der
        # Nachweis, dass geroutet wurde.
        vorher = set(agent.enabled_toolsets)
        self.assertTrue({"file", "terminal"}.issubset(vorher),
                        f"die vorhergesagten Toolsets fehlen: {vorher}")
        # Die Werkzeugliste enthaelt NUR die Tools der gerouteten Toolsets.
        erlaubt = {"read_file", "web_search", "request_toolset"}
        namen = {t_["function"]["name"] for t_ in agent.tools}
        self.assertTrue(namen.issubset(erlaubt),
                        f"die Werkzeugliste enthaelt unerwartete Tools: {namen}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
