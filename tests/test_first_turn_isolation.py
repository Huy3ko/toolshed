"""Isolation und Verhalten der First-Turn-Route (2.3 / 2.4).

Der Router entscheidet pro Sitzung, welche Werkzeugflaeche das Modell sieht.
Wenn diese Entscheidung die Sitzungsgrenze oder die Autorisierungsgrenze
verletzt, ist das ein Rechteverlust - kein Routing-Fehler. Diese Tests
pruefen genau das:

  1. Zwei Sitzungen teilen sich keinen Zustand.
  2. Zwei Agenten in derselben Sitzung storen sich nicht.
  3. Die Route bleibt innerhalb der vom Host freigegebenen Toolsets.
  4. Die Freigabe wird beim Host gelesen, nicht geraten.
  5. Ein fehlendes Werkzeug bleibt ueber den Recovery-Pfad erreichbar.
  6. Ohne Session-Autorisierung: kein Eingriff.
  7. Concurrent Turns zweier Sitzungen ueberleben sich gegenseitig nicht.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from typing import ClassVar
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.abspath(os.path.join(HERE, "..", "runtime"))
if RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

import llm_request_routing as llr
from state import RouterState


class _FakeAgent:
    """Agent-Ersatz.

    `enabled_toolsets` traegt die Host-Freigabe (Toolset-Namen). `tools` und
    `valid_tool_names` tragen Werkzeugnamen (Schema-Namen). Die beiden
    Ebenen werden getrennt gehalten - der Host kennt Werkzeugnamen, die
    Freigabe wird in Toolsets gewaehrt.
    """

    TOOL_NAMES: ClassVar[dict] = {
        "terminal": "terminal",
        "file": "read_file",
        "web": "web_search",
        "memory": "memory_search",
    }

    def __init__(self, session_id="sess-1", toolsets=("terminal", "file", "web", "memory")):
        self.session_id = session_id
        self.enabled_toolsets = list(toolsets)
        names = [self.TOOL_NAMES[t] for t in toolsets if t in self.TOOL_NAMES]
        self.tools = [{"function": {"name": n}} for n in names]
        self.valid_tool_names = set(names)

    def _attr(self, name):
        return getattr(self, name, None)


class _Scope:
    """Ruft die Middleware aus einem Rahmen, in dem `agent` ein Local ist."""

    def __init__(self, agent):
        self.agent = agent

    def invoke(self, **kwargs):
        agent = self.agent  # noqa: F841 - so liegt es im Host-Stack
        return llr.llm_request_middleware(**kwargs)


def _request(message="Lies die Datei /tmp/x.txt", names=("terminal", "read_file", "web_search", "memory_search")):
    return {
        "model": "test",
        "messages": [{"role": "user", "content": message}],
        "tools": [{"function": {"name": n}} for n in names],
    }


class IsolationContract(unittest.TestCase):
    def setUp(self):
        self._patches = []
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file", "terminal"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})
        self._patch("config._load_config", dict)
        self._patch("config._get_profile_config", lambda cfg: {
            "enabled": True,
            "mode": "active",
            "floor_toolsets": ["terminal", "file"],
            "long_message_decline_chars": 2000,
            "short_message_bypass_chars": 0,
            "confidence_threshold": 0.0,
        })

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _patch(self, target, attr=None, new=None):
        if isinstance(target, str):
            value = new if new is not None else attr
            p = mock.patch(target, value)
        else:
            p = mock.patch.object(target, attr, new)
        p.start()
        self._patches.append(p)
        return p

    def _agent_with_state(self, session_id="sess-1", toolsets=("terminal", "file", "web", "memory")):
        agent = _FakeAgent(session_id, toolsets)
        state = RouterState()
        state.capture_authorization(agent)
        agent._hermes_token_router_state = state
        return agent, state

    # --- 1: Session-Isolation -------------------------------------------
    def test_two_sessions_do_not_share_state(self):
        """Bedingung 1: keine geteilte Route zwischen Sitzungen."""
        a1, s1 = self._agent_with_state("sess-1")
        a2, s2 = self._agent_with_state("sess-2")

        r1 = _Scope(a1).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(r1, "Sitzung 1 wurde nicht geroutet")

        # Sitzung 2 ist unberuehrt: ihr Zustand kennt keine Route.
        self.assertFalse(s2.initial_route_applied,
                         "Sitzung 2 hat den Zustand von Sitzung 1 uebernommen")
        self.assertIsNone(s2.predicted_toolsets,
                          "Sitzung 2 hat die Vorhersage von Sitzung 1 uebernommen")

        r2 = _Scope(a2).invoke(request=_request(), session_id="sess-2")
        self.assertIsNotNone(r2, "Sitzung 2 wurde nicht geroutet")

        # Beide Sitzungen haben ihre EIGENE Route - nicht dieselbe.
        self.assertEqual(s2.initial_toolsets, s1.initial_toolsets)
        self.assertIsNot(s2.initial_toolsets, s1.initial_toolsets,
                         "die Sitzungen teilen ein Mengenobjekt - "
                         "eine Aenderung in der einen wirkt auf die andere")

    # --- 2: Zwei Agenten in derselben Sitzung ---------------------------
    def test_two_agents_in_one_session_do_not_interfere(self):
        """Bedingung 2: zwei Agenten derselben Sitzung storen sich nicht."""
        a1, s1 = self._agent_with_state("sess-1")
        a2, s2 = self._agent_with_state("sess-1")

        r1 = _Scope(a1).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(r1, "Agent 1 wurde nicht geroutet")

        # Agent 2 hat seinen eigenen Zustand und wird eigenstaendig geroutet.
        self.assertFalse(s2.initial_route_applied,
                         "Agent 2 hat den Zustand von Agent 1 uebernommen")
        r2 = _Scope(a2).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(r2, "Agent 2 wurde nicht geroutet")

        # Beide Agenten haben getrennte Zustandsobjekte.
        self.assertIsNot(s1.initial_toolsets, s2.initial_toolsets,
                         "die Agenten teilen ein Mengenobjekt - "
                         "eine Aenderung in dem einen wirkt auf den anderen")

    # --- 3: Autorisierungsgrenze ----------------------------------------
    def test_route_stays_within_host_granted_toolsets(self):
        """Bedingung 3: die Route darf die Host-Freigabe nicht verlassen."""
        agent, state = self._agent_with_state("sess-1", toolsets=("terminal", "file"))
        # Der Host gibt NUR terminal und file frei.
        self.assertEqual(state.authorized_toolsets, {"terminal", "file"})

        # Die Vorhersage will mehr, als freigegeben ist.
        self._patch(llr, "_predict", new=lambda m, c, a: {"file", "memory", "web"})

        result = _Scope(agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(result, "es wurde nicht geroutet")
        names = {t["function"]["name"] for t in result["request"]["tools"]}
        # Nur Werkzeuge aus der freigegebenen Flaeche dufsten durch.
        self.assertTrue(names <= {"read_file", "terminal"},
                        f"die Route verlaesst die Host-Freigabe: {names}")

    # --- 4: Autorisierung wird gelesen, nicht geraten -------------------
    def test_authorization_is_read_from_the_host(self):
        """Bedingung 4: ohne Host-`enabled_toolsets` wird nicht geroutet."""
        agent = _FakeAgent("sess-1")
        # `enabled_toolsets` fehlt: der Host hat nichts freigegeben.
        del agent.enabled_toolsets
        state = RouterState()
        state.capture_authorization(agent)
        agent._hermes_token_router_state = state

        self.assertIsNone(state.authorized_toolsets,
                          "die Autorisierung wurde geraten statt gelesen")
        result = _Scope(agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNone(result, "ohne Host-Freigabe wurde geroutet")

    # --- 5: Recovery bleibt erreichbar ----------------------------------
    def test_hidden_tool_stays_resolvable_via_recovery(self):
        """Bedingung 5: ein ausgeblendetes Werkzeug bleibt aufloesbar."""
        agent, _state = self._agent_with_state("sess-1")
        result = _Scope(agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(result)

        names = {t["function"]["name"] for t in result["request"]["tools"]}
        self.assertNotIn("memory_search", names,
                         "die Werkzeugflaeche wurde nicht verengt")

        # Der Host kennt die Werkzeuge weiterhin - fuer das Modell sind sie nur
        # unsichtbar. Deshalb bleibt der Recovery-Pfad aufloesbar: das Modell
        # darf ein verdecktes Werkzeug anfordern, und der Host fuehrt es aus.
        agent_names = {t["function"]["name"] for t in agent.tools}
        self.assertIn("memory_search", agent_names,
                      "agent.tools wurde verengt - die Host-Freigabe ist gebrochen")
        self.assertEqual(agent.valid_tool_names,
                         {"terminal", "read_file", "web_search", "memory_search"},
                         "valid_tool_names wurde veraendert - ein verdecktes "
                         "Werkzeug waere nicht mehr aufloesbar")

    # --- 6: Concurrent Turns zweier Sitzungen ----------------------------
    def test_concurrent_turns_of_two_sessions_survive(self):
        """Bedingung 6: parallele Turns verschiedener Sitzungen storen sich nicht."""
        results = {}
        errors = []

        def run(session_id):
            try:
                agent, _ = self._agent_with_state(session_id)
                r = _Scope(agent).invoke(request=_request(), session_id=session_id)
                results[session_id] = r
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(f"sess-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Fehler unter Parallelbetrieb: {errors}")
        self.assertEqual(len(results), 8, f"nicht alle Sessions geroutet: {len(results)}")
        for session_id, r in results.items():
            self.assertIsNotNone(r, f"{session_id} wurde nicht geroutet")

    # --- 7: Floor-Werkzeuge bleiben -------------------------------------
    def test_floor_toolsets_are_always_present(self):
        """Bedingung 7: die Bodenwerkzeuge verschwinden nie."""
        agent, _state = self._agent_with_state("sess-1")
        # Die Vorhersage liefert nichts - es bleibt nur der Boden.
        self._patch(llr, "_predict", new=lambda m, c, a: set())

        result = _Scope(agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(result, "der leere Boden wurde nicht angewendet")
        names = {t["function"]["name"] for t in result["request"]["tools"]}
        self.assertIn("terminal", names, "das Bodenwerkzeug terminal fehlt")
        self.assertIn("read_file", names, "das Bodenwerkzeug file fehlt")


if __name__ == "__main__":
    unittest.main()
