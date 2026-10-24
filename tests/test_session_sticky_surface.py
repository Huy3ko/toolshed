"""Vertragstest: die Werkzeugflaeche bleibt ueber die ganze Session verengt.

GEMESSENER BEFUND (24.10.2026, Live-Messung am Gateway):

  Die `llm_request`-Middleware verengte nur den ERSTEN Request einer
  Session. Jeder weitere Request in derselben Session trug wieder die
  volle Werkzeugflaeche:

    12:21:20  first-turn route applied: 46 -> 11 tools
    12:21:23  API call #1: in=21426      (verengt)
    12:21:25  llm_request kept full surface (reason=already_routed)
    12:21:27  API call #2: in=49120      (volle Flaeche, +27.694 Token)

  Ursache: `RouterState.initial_route_applied` ist session-sticky und
  `llm_request_middleware` bails mit `already_routed`, sobald das Flag
  gesetzt ist. Der Zustand "diese Session ist geroutet" wurde damit
  mit "dieser Request ist bereits geroutet" verwechselt.

  Folge: Bei einem Turn mit N Modellaufrufen wird nur der erste mit der
  schmalen Flaeche gebaut. Bei 18 Aufrufen (ChatGPT-Messung: 1.162.489
  kumulativ abgerechnete Token) bedeutet das 17x die volle Flaeche.

  Der urspruengliche Toolshed-Vertrag (ADR-0004, ADR-0001) verlangt
  "stabile Werkzeugauswahl innerhalb einer Session" - die Verengung
  soll den ganzen Turn halten, nicht nur den ersten Request.

WAS DIESER TEST FESTSCHREIBT:

  1. Der ZWEITE Request derselben Session wird ebenfalls verengt
     (gleiche Auswahl wie der erste).
  2. Die Auswahl bleibt STABIL: der zweite Request nutzt die bereits
     getroffene Entscheidung, nicht eine neue Vorhersage.
  3. Die dynamische Erweiterung bleibt erhalten: was der Agent
     nachweislich braucht (Recovery/Expansion), bleibt im Request.
  4. Fail-open bleibt: ohne Autorisierung oder bei Fehlern bleibt die
     volle Flaeche - in JEDEM Request, nicht nur im ersten.

Der bestehende Test `test_second_request_is_not_routed_again` in
`test_first_turn_route.py` schreibt das Fehlverhalten fest. Er wird
mit diesem Vertrag ersetzt (siehe dort).
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.abspath(os.path.join(HERE, "..", "runtime"))
if RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)

import llm_request_routing as llr
from state import RouterState


class _FakeAgent:
    """Minimaler Agent: die Attribute, die der Router liest."""

    def __init__(self, session_id="sess-sticky"):
        self.session_id = session_id
        self.tools = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "terminal"}},
            {"function": {"name": "web_search"}},
            {"function": {"name": "memory_search"}},
        ]
        self.valid_tool_names = {"read_file", "terminal", "web_search", "memory_search"}
        self.enabled_toolsets = ["file", "terminal", "web", "memory"]


def _request():
    return {
        "model": "test",
        "messages": [{"role": "user", "content": "hallo"}],
        "tools": [
            {"function": {"name": "read_file"}},
            {"function": {"name": "terminal"}},
            {"function": {"name": "web_search"}},
            {"function": {"name": "memory_search"}},
        ],
    }


class _AgentScope:
    """Scope, in dem der Agent ein lokaler Name ist (bildet den Host ab)."""

    def __init__(self, agent, original_user_message=None):
        self.agent = agent
        self.original_user_message = original_user_message

    def invoke(self, **kwargs):
        agent = self.agent  # noqa: F841 - genau so liegt es im Host-Stack
        original_user_message = self.original_user_message  # noqa: F841
        return llr.llm_request_middleware(**kwargs)


class SessionStickySurfaceContract(unittest.TestCase):
    def setUp(self):
        self.agent = _FakeAgent()
        self.state = RouterState()
        self.state.authorized_toolsets = {"file", "terminal", "web", "memory"}
        self.state.authorization_captured = True
        self.agent._hermes_token_router_state = self.state
        self._patches = []

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

    def _patch_config(self, **overrides):
        base = {
            "enabled": True,
            "mode": "active",
            "floor_toolsets": ["terminal", "file"],
            "long_message_decline_chars": 2000,
            "short_message_bypass_chars": 0,
            "confidence_threshold": 0.0,
        }
        base.update(overrides)
        self._patch("config._load_config", dict)
        self._patch("config._get_profile_config", lambda cfg: base)

    def _patch_routing(self, predicted=None):
        """Die Router-Internals so ersetzen, dass nur die Route selbst zaehlt."""
        predicted = predicted if predicted is not None else {"file"}
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file", "terminal"})
        self._patch(llr, "_predict", new=lambda m, c, a: set(predicted))

    def _names(self, result):
        return {t["function"]["name"] for t in result["request"]["tools"]}

    # ------------------------------------------------------------------
    # 1. Der zweite Request wird ebenfalls verengt
    # ------------------------------------------------------------------
    def test_second_request_of_the_session_is_also_narrowed(self):
        """Der Kern des Fixes: die Verengung gilt fuer die ganze Session.

        Vor dem Fix trug der zweite Request die volle Flaeche
        (`already_routed`). Das kostete bei 18 Modellaufrufen 17x die
        volle Werkzeugflaeche.
        """
        self._patch_config()
        self._patch_routing()

        first = _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )
        self.assertIsNotNone(first, "die erste Route fehlte")
        self.assertEqual(self._names(first), {"read_file", "terminal"})

        second = _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )
        self.assertIsNotNone(
            second,
            "der zweite Request wurde NICHT verengt - die Werkzeugflaeche "
            "waechst im Verlauf der Session wieder auf die volle Flaeche an",
        )
        self.assertEqual(
            self._names(second), {"read_file", "terminal"},
            f"der zweite Request traegt eine andere Auswahl: {self._names(second)}",
        )

    # ------------------------------------------------------------------
    # 2. Die Auswahl bleibt stabil (keine neue Vorhersage pro Request)
    # ------------------------------------------------------------------
    def test_the_surface_stays_stable_across_requests(self):
        """Ab dem zweiten Request wird NICHT neu vorhergesagt.

        Der Router darf die Auswahl nicht im Turn nachziehen: das wuerde
        bei jedem Request eine neue LLM-Vorhersage kosten und die
        Auswahl wuerde mitten im Turn springen.
        """
        self._patch_config()
        predict_calls = []
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file", "terminal"})

        def _counting_predict(msg, cfg, avail):
            predict_calls.append(msg)
            return {"file"}

        self._patch(llr, "_predict", new=_counting_predict)

        _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )
        calls_after_first = len(predict_calls)
        self.assertEqual(calls_after_first, 1,
                         "der erste Request hat nicht genau einmal vorhergesagt")

        _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )
        _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )

        self.assertEqual(
            len(predict_calls), calls_after_first,
            "der Router hat im Turn MEHRFACHER neu vorhergesagt - "
            "das kostet eine LLM-Runde pro Request und laesst die "
            "Werkzeugauswahl mitten im Turn springen",
        )

    # ------------------------------------------------------------------
    # 3. Die dynamische Erweiterung bleibt erhalten
    # ------------------------------------------------------------------
    def test_expanded_toolset_stays_in_later_requests(self):
        """Was der Agent nachweislich braucht, bleibt im Request.

        Die Verengung ist eine Obergrenzung, kein Verbot: sobald ein
        Werkzeug nachweislich gebraucht wurde (Recovery-Pfad), muss es
        in den folgenden Requests sichtbar bleiben. Sonst bricht der
        laufende Turn an einem Werkzeug, das der Host bereits
        freigegeben hat.
        """
        self._patch_config()
        self._patch_routing()

        # Die Aufloesung liefert je nach Toolset verschiedene Werkzeugnamen.
        # Nur so kann der Test unterscheiden, ob die ERWEITERTE Flaeche
        # (active_toolsets) verwendet wurde oder die urspruengliche
        # (initial_toolsets). Bei identischen Feldern waere die Mutation
        # "active -> initial" verhaltensgleich und der Test wuerde die
        # Erweiterung nicht pruefen.
        def _resolve_by_toolset(toolsets):
            names = set()
            if "file" in toolsets:
                names.add("read_file")
            if "terminal" in toolsets:
                names.add("terminal")
            if "memory" in toolsets:
                names.add("memory_search")
            if "web" in toolsets:
                names.add("web_search")
            return names

        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: _resolve_by_toolset(ts))

        first = _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )
        self.assertIsNotNone(first)
        first_names = self._names(first)
        self.assertNotIn(
            "memory_search", first_names,
            f"die erste Route war schon zu breit: {first_names}",
        )

        # Der Agent hat ein Werkzeug ausserhalb der Route benutzt und der
        # Recovery-Pfad hat die Flaeche erweitert. `initial_toolsets`
        # bleibt bewusst unveraendert - das ist der Unterschied, den der
        # Test prueft.
        self.state.active_toolsets = {"file", "terminal", "memory"}
        self.state.initial_toolsets = {"file", "terminal"}
        self.state.predicted_toolsets = {"file", "memory"}

        second = _AgentScope(self.agent).invoke(
            request=_request(), session_id="sess-1", turn_id="t1",
        )
        self.assertIsNotNone(
            second,
            "nach einer Erweiterung wurde der Request nicht mehr geroutet - "
            "das bricht die Wiederaufnahme eines laufenden Turns",
        )
        names = self._names(second)
        self.assertIn(
            "memory_search", names,
            "die Erweiterung (memory) fehlt im Request - das Recovery-Pfad-"
            "Werkzeug ist fuer das Modell nicht mehr sichtbar: {0}".format(names),
        )
        self.assertIn(
            "read_file", names,
            "die Grundflaeche fehlt nach der Erweiterung: {0}".format(names),
        )

    # ------------------------------------------------------------------
    # 4. Fail-open gilt in JEDEM Request
    # ------------------------------------------------------------------
    def test_fail_open_applies_to_later_requests_too(self):
        """Ohne Autorisierung bleibt die volle Flaeche - auch im 2. Request.

        Die Autorisierung wird einmal pro Session erfasst. Wenn sie
        fehlt, darf KEIN Request verengt werden. Der bestehende Test
        prueft das nur fuer den ersten Request.
        """
        self._patch_config()
        self._patch_routing()

        # Autorisierung fehlt (frischer State ohne Host-Freigabe).
        self.state.authorized_toolsets = None
        self.state.authorization_captured = True

        for label in ("erster", "zweiter"):
            result = _AgentScope(self.agent).invoke(
                request=_request(), session_id="sess-1", turn_id="t1",
            )
            self.assertIsNone(
                result,
                f"im {label} Request wurde ohne Autorisierung verengt - "
                "fail-open verletzt",
            )


if __name__ == "__main__":
    unittest.main()
