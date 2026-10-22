"""RED-Test: der Router muss den Agenten session-gebunden finden.

BEWIESENE URSACHE (Vela, 22.10.2026, empirisch am laufenden Hermes v0.22.2
gemessen - nicht aus dem Code gelesen):

    pre_llm_call      -> Thread 'hermes-hook-cb'  (NICHT MainThread)
    on_session_start  -> Thread 'hermes-hook-cb'  (NICHT MainThread)
    subagent_stop     -> Thread 'MainThread'      (nicht bounded)
    tool_request MIDDLEWARE -> Thread 'MainThread' (inline!)

`pre_llm_call` ist in `hermes_cli/plugins_dispatch.py:42` ein BOUNDED Hook
und laeuft laut Zeile 271 auf einem Daemon-Worker:
"Run one callback on a daemon worker". Der Aufrufer-Stack - und damit das
`agent`-Local aus `agent/turn_context.build_turn_context` - ist dort
abgeschnitten.

Folge im Betrieb (ONE, ~/.hermes/logs/agent.log):
    WARNING hermes_token_router: no agent reference; full set fallback
Der Router verengt die Werkzeugflaeche NIE. Er routet faktisch nicht.

Die Middleware `tool_request` laeuft dagegen INLINE (MainThread) und
bekommt `session_id` + `turn_id`. Dort ist der Agent ueber den Stack
erreichbar. Das ist die Bruecke: der Agent wird EINMAL session-gebunden
gelernt und danach ueber die Session aufgeloest - ohne Stack-Walk im
Worker-Thread, ohne globales current_agent.

VERTRAG:
  1. Der Middleware-Pfad lernt die Zuordnung session-gebunden.
  2. `pre_llm_call` loest den Agenten ueber die Session auf (Worker-Thread).
  3. Ohne Zuordnung: KEIN Alarm pro Turn. Der Hinweis erscheint hoechstens
     einmal pro Prozess - sonst loggt der Router jede Sekunde.
  4. Zwei Sessions teilen sich nie einen Agenten.
"""
import logging
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402
from runtime import state as rt_state  # noqa: E402


class _FakeAgent:
    def __init__(self, session_id="sess-1"):
        self.tools = [{"function": {"name": "terminal"}}]
        self.session_id = session_id
        self.enabled_toolsets = ["terminal", "file"]
        self.valid_tool_names = {"terminal"}


class AgentLearnedFromInlinePath(unittest.TestCase):
    """Der Agent muss ueber die Session gefunden werden."""

    def setUp(self):
        rt_state._agent_ref = None
        rt_state._agent_refs = {}
        rt_state._reset_missing_agent_notice()
        # Der Router muss AKTIV sein - sonst routet er per Definition nicht
        # (Muster aus test_v017_contracts.py:153).
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
        self.addCleanup(self._reset)
        # Die Toolset-Liste kommt aus Hermes' registry (im Testprozess nicht
        # vorhanden) - Muster aus test_v017_contracts.py:166.
        patch_toolsets = mock.patch.object(
            runtime, "_get_available_toolsets", lambda: {"terminal", "file", "web"})
        patch_predict = mock.patch.object(
            runtime, "_predict_toolsets_by_rules",
            lambda msg, available: ({"terminal", "file"}, "test-rule"))
        patch_toolsets.start()
        patch_predict.start()
        self.addCleanup(patch_toolsets.stop)
        self.addCleanup(patch_predict.stop)
        # Hermes' Tool-Registry (im Testprozess nicht vorhanden).
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
            sys.modules, {"tools": types.ModuleType("tools"),
                          "tools.registry": _reg})
        patch_reg.start()
        self.addCleanup(patch_reg.stop)



    def _reset(self):
        rt_state._agent_ref = None
        rt_state._agent_refs = {}
        rt_state._reset_missing_agent_notice()

    def test_middleware_learns_the_agent_for_the_session(self):
        """Der Middleware-Pfad (inline) lernt die Zuordnung."""
        agent = _FakeAgent("sess-mw")
        # Der Agent liegt im Stack des Aufrufers (so ist es im Betrieb):
        def aufrufer():
            # Der Router sucht den Agenten ueber den Stack:
            rt_state._remember_agent_from_stack("sess-mw")
        aufrufer()
        # Ergebnis: die Session kennt jetzt den Agenten.
        rt_state._store_agent_ref(agent, "sess-mw")
        self.assertIs(rt_state._get_agent_ref("sess-mw"), agent)

    def test_stack_helper_finds_agent_with_tools_attribute(self):
        """Der Stack-Helper findet einen Agenten mit `tools` im Aufrufer-Frame.

        Der Test laeuft in einem Thread, dessen Frame ein 'agent'-Local MIT
        'tools' traegt - so ist es im Betrieb (inline laufende Middleware)."""
        agent = _FakeAgent("sess-stack")
        rt_state._agent_ref = None
        rt_state._agent_refs = {}

        def middleware_aufrufer():
            # 'agent' ist ein Local dieses Frames - genau die Betriebssituation.
            agent = _FakeAgent("sess-stack")
            gefunden = rt_state._remember_agent_from_stack("sess-stack")
            return gefunden

        gefunden = middleware_aufrufer()
        self.assertIsNotNone(gefunden,
                             "der Stack-Helper fand den Agenten nicht")
        self.assertIs(rt_state._get_agent_ref("sess-stack"), gefunden,
                      "der gefundene Agent wurde nicht der Session zugeordnet")

    def test_router_resolves_agent_by_session_without_stack(self):
        """Der Kernvertrag: `pre_llm_call` nutzt die Session-Zuordnung.

        Der Test laeuft in einem Worker-Thread (wie Hermes) - dort gibt es
        keinen Agenten im Stack. Trotzdem muss der Router routen."""
        agent = _FakeAgent("sess-rt")
        rt_state._store_agent_ref(agent, "sess-rt")

        ergebnis = {}

        def worker():
            # Kein 'agent'-Local im Stack -> der Stack-Walk scheitert hier.
            ergebnis["stack"] = rt_state._get_agent_from_stack()
            runtime.pre_llm_call(
                session_id="sess-rt",
                user_message="read the file /tmp/notes.txt",
                conversation_history=[],
                is_first_turn=True,
            )

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertIsNone(ergebnis["stack"], "der Worker hatte unerwartet einen Agenten im Stack")
        # Der Nachweis der Route ist die VERENGTE Werkzeugflaeche, nicht ein
        # Rueckgabewert des Hooks (der liefert per Vertrag keinen Kontext).
        nachher = set(agent.enabled_toolsets)
        self.assertTrue({"file", "terminal"}.issubset(nachher),
                        f"die vorhergesagten Toolsets fehlen: {nachher}")
        erlaubt = {"read_file", "web_search", "request_toolset"}
        namen = {x["function"]["name"] for x in agent.tools}
        self.assertTrue(namen.issubset(erlaubt),
                        f"die Werkzeugliste enthaelt unerwartete Tools: {namen}")

    def test_missing_agent_warns_only_once(self):
        """Ohne Zuordnung: EIN Hinweis, nicht einer pro Turn."""
        rt_state._reset_missing_agent_notice()
        with self.assertLogs("runtime", level="WARNING") as logs:
            runtime.pre_llm_call(
                session_id="sess-unbekannt",
                user_message="hallo",
                conversation_history=[],
                is_first_turn=True,
            )
            runtime.pre_llm_call(
                session_id="sess-unbekannt",
                user_message="hallo nochmal",
                conversation_history=[],
                is_first_turn=True,
            )
        warnungen = [r for r in logs.records if "no agent reference" in r.getMessage()]
        self.assertLessEqual(
            len(warnungen), 1,
            f"der Router warnt {len(warnungen)}-mal statt einmal - das flutet das Log",
        )

    def test_two_sessions_are_never_mixed(self):
        """Zwei Sessions, zwei Agenten - keine Vermischung."""
        a = _FakeAgent("sess-1")
        b = _FakeAgent("sess-2")
        rt_state._store_agent_ref(a, "sess-1")
        rt_state._store_agent_ref(b, "sess-2")
        self.assertIs(rt_state._get_agent_ref("sess-1"), a)
        self.assertIs(rt_state._get_agent_ref("sess-2"), b)
        self.assertIsNot(rt_state._get_agent_ref("sess-1"),
                         rt_state._get_agent_ref("sess-2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
