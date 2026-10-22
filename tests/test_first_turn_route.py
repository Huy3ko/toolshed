"""Vertragstest der First-Turn-Route (llm_request-Middleware).

Deckt den Vertrag ab, den die Middleware einhalten muss:

  1. Ein schmaler Request wird tatsaechlich schmal - die `tools`-Liste, die
     das Modell sieht, ist die gefilterte.
  2. Fail-open: ohne erfasste Host-Autorisierung bleibt die volle Flaeche.
  3. Fail-open: bei jedem Fehler in der Vorhersage bleibt die volle Flaeche.
  4. Session-sticky: nach einer Route wird ein zweiter Request nicht erneut
     geroutet.
  5. `agent.tools` bleibt unveraendert - die Host-Freigabe bleibt intakt.

Die Tests laufen gegen die isolierte Runtime-Kopie; sie beruehren keine
laufende Hermes-Installation.
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
    def __init__(self, session_id="sess-1"):
        self.session_id = session_id
        self.tools = [
            {"function": {"name": "terminal"}},
            {"function": {"name": "read_file"}},
            {"function": {"name": "web_search"}},
            {"function": {"name": "memory_search"}},
        ]
        self.valid_tool_names = {"terminal", "read_file", "web_search", "memory_search"}
        self.enabled_toolsets = ["terminal", "file", "web", "memory"]


_DEFAULT_TOOLS = (
    {"function": {"name": "terminal"}},
    {"function": {"name": "read_file"}},
    {"function": {"name": "web_search"}},
    {"function": {"name": "memory_search"}},
)


def _request(tools=None, message="Lies die Datei /tmp/notizen.txt"):
    return {
        "model": "test",
        "messages": [{"role": "user", "content": message}],
        "tools": list(tools if tools is not None else [
            {"function": {"name": "terminal"}},
            {"function": {"name": "read_file"}},
            {"function": {"name": "web_search"}},
            {"function": {"name": "memory_search"}},
        ]),
    }


def _install_fake_router(monkey, predicted=None, available=None):
    """Die Router-Internals so ersetzen, dass nur die Route selbst getestet wird."""
    import policy
    import router_tools

    predicted = predicted if predicted is not None else {"file"}
    available = available or {"file", "terminal", "web", "memory"}
    monkey.setattr(policy, "_get_available_toolsets", lambda: set(available))
    monkey.setattr(router_tools, "_resolve_toolset_to_tool_names",
                    lambda toolsets: {f"tool_{ts}" for ts in toolsets})
    monkey.setattr(llr, "_predict", lambda msg, cfg, avail: set(predicted))
    return available



def _call_middleware_with_agent(agent, **kwargs):
    """Ruft die Middleware so auf, dass der Agent im Aufrufer-Stack liegt.

    Das bildet den Host ab: `build_api_request(agent=...)` ruft die Middleware
    aus einem Rahmen, in dem `agent` ein Local ist. Ohne diesen Rahmen
    findet `_find_agent_in_stack()` nichts - was korrekt fail-open waere,
    aber den Test sinnlos machen wuerde.
    """
    return llr.llm_request_middleware(**kwargs)


class _AgentScope:
    """Minimaler Scope, in dem der Agent ein lokaler Name ist.

    `original_user_message` im Stack bildet den Core nach: Hermes reicht
    die ECHTE User-Nachricht als Keyword-Parameter an `build_api_request`
    weiter, und genau diese Funktion liegt im Stack, wenn die Middleware
    laeuft. Der Request selbst traegt den Hermes-Kontext-Wrapper.
    """

    def __init__(self, agent, original_user_message=None):
        self.agent = agent
        self.original_user_message = original_user_message

    def invoke(self, **kwargs):
        agent = self.agent  # noqa: F841 - genau so liegt es im Host-Stack
        original_user_message = self.original_user_message  # noqa: F841
        return llr.llm_request_middleware(**kwargs)


class FirstTurnRouteContract(unittest.TestCase):
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
        # Zwei Zielformen:
        #   _patch("modul.attr", wert)   -> der Wert steckt in `attr`
        #   _patch(modul, "attr", wert)  -> der Wert steckt in `new`
        if isinstance(target, str):
            value = new if new is not None else attr
            p = mock.patch(target, value)
        else:
            module = target
            p = mock.patch.object(module, attr, new)
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

    def test_narrow_request_really_carries_the_narrow_tool_list(self):
        """Bedingung 1: der Request wird schmal."""
        self._patch_config()

        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {f"tool_{t}" for t in ts})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        request = _request()
        # Die Werkzeugnamen, die der Router zurueckgibt, muessen zu den
        # vorhandenen Werkzeugnamen passen, sonst filtert nichts.
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file", "terminal"})

        result = _AgentScope(self.agent).invoke(
            request=request, session_id="sess-1", turn_id="t1",
        )
        self.assertIsNotNone(result, "die Middleware hat nicht geroutet")
        names = {t["function"]["name"] for t in result["request"]["tools"]}
        self.assertEqual(names, {"read_file", "terminal"},
                         f"die Tool-Liste ist nicht die erwartete: {names}")
        self.assertLess(len(names), 4, "es wurde nicht verengt")

    def test_without_authorization_the_full_surface_stays(self):
        """Bedingung 2: ohne Host-Freigabe bleibt alles (fail-open)."""
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        # Der Host hat die Autorisierung ABGESCHLOSSEN und nichts
        # freigegeben. Das ist der Zustand, den `capture_authorization`
        # erzeugt, wenn der Agent kein `enabled_toolsets` anbietet
        # (state.py:121-124): captured=True, authorized=None.
        #
        # WICHTIG: `captured=False` waere der FRISCHE State - da hat der
        # Host noch gar nichts entschieden, und die Middleware darf die
        # Freigabe dann selbst erfassen. Der Testfall hier ist der
        # Verweigerungsfall, und der muss fail-open bleiben.
        self.state.authorized_toolsets = None
        self.state.authorization_captured = True

        request = _request()
        result = _AgentScope(self.agent).invoke(request=request, session_id="sess-1")
        self.assertIsNone(result,
                          "ohne Autorisierung wurde die Flaeche verengt - "
                          "das waere ein Rechteverlust")

    def test_host_without_enabled_toolsets_keeps_the_full_surface(self):
        """Bedingung 2b: der Host bietet gar keine Freigabeflaeche.

        `capture_authorization` setzt dann captured=True, authorized=None.
        Auch hier darf nichts verengt werden.
        """
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        # Der Agent bietet kein `enabled_toolsets` - die Host-Freigabe
        # fehlt damit vollstaendig.
        del self.agent.enabled_toolsets
        self.state.authorized_toolsets = None
        self.state.authorization_captured = False

        request = _request()
        result = _AgentScope(self.agent).invoke(request=request, session_id="sess-1")
        self.assertIsNone(result,
                          "ohne Host-Freigabeflaeche wurde die Flaeche verengt - "
                          "das waere ein Rechteverlust")

    def test_fresh_state_may_capture_the_host_grant(self):
        """Die Middleware darf die Host-Freigabe selbst erfassen.

        Der Hook `pre_llm_call` laeuft im Worker-Thread und findet dort
        keinen Agenten: er legt den State an, erfasst die Autorisierung
        aber nicht. Fuer die Middleware ist das "noch nicht entschieden" -
        sie darf `agent.enabled_toolsets` lesen.

        Gegenprobe zum Rechteverlust: der erfasste Satz wird mit
        `available` UND mit der Host-Freigabe geschnitten - nichts, was
        der Host nicht freigegeben hat, kann in die Route gelangen.
        """
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        # Frischer State: der Hook war zu frueh, nichts erfasst.
        self.state.authorized_toolsets = None
        self.state.authorization_captured = False

        request = _request()
        result = _AgentScope(self.agent).invoke(request=request, session_id="sess-1")
        self.assertIsNotNone(result,
                             "ein frischer State muss die Host-Freigabe erfassen "
                             "koennen - sonst routet der erste Request nie")
        # Die Route darf NUR enthalten, was der Host freigegeben hat.
        routed = {t["function"]["name"] for t in result["request"]["tools"]}
        self.assertTrue(
            routed.issubset({"terminal", "read_file", "web_search", "memory_search"}),
            "Route enthaelt Werkzeuge ausserhalb der Host-Freigabe: %s" % routed,
        )
        # Und die erfasste Autorisierung muss exakt der Host-Freigabe entsprechen.
        self.assertEqual(
            self.state.authorized_toolsets,
            {"terminal", "file", "web", "memory"},
            "erfasste Autorisierung weicht von der Host-Freigabe ab",
        )

    def test_route_stays_inside_the_host_authorization(self):
        """Die Route darf NICHTS enthalten, was der Host nicht freigegeben hat.

        Das ist das eigentliche Rechte-Gate: `allowed` muss mit der
        Host-Freigabe geschnitten werden. Ohne diesen Schnitt koennte der
        Router Werkzeuge anbieten, die der Operator nie freigegeben hat.
        """
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        # 'file' loest zu read_file auf, 'terminal' zu terminal.
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"} if "file" in ts else {"terminal"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        # Der Host gibt NUR terminal frei - file ist nicht freigegeben.
        self.state.authorized_toolsets = {"terminal"}
        self.state.authorization_captured = True

        request = _request()
        result = _AgentScope(self.agent).invoke(request=request, session_id="sess-1")
        self.assertIsNotNone(result, "die Route wurde vollstaendig verworfen")
        routed = {t["function"]["name"] for t in result["request"]["tools"]}
        self.assertNotIn(
            "read_file",
            routed,
            "read_file ist nicht freigegeben, wurde aber geroutet: %s" % routed,
        )
        self.assertTrue(
            routed.issubset({"terminal", "read_file", "web_search", "memory_search"}),
            "Route enthaelt Werkzeuge ausserhalb der Host-Freigabe: %s" % routed,
        )

    def test_stack_message_beats_the_wrapped_request_message(self):
        """Die echte User-Nachricht zaehlt, nicht der Kontext-Wrapper.

        In Hermes traegt die Request-Nachricht den kompletten Kontext
        (tausende Zeichen). Die Regel `long_message_decline_chars` wuerde
        daraus IMMER ein "message_too_long" machen - der Router koennte
        nie routen. `original_user_message` aus dem Stack ist die echte
        Anfrage.
        """
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        self.state.authorized_toolsets = {"file", "terminal", "web", "memory"}
        self.state.authorization_captured = True

        # Der Request traegt einen langen Wrapper, der Stack die kurze
        # echte Nachricht.
        wrapped = "SYSTEMKONTEXT " * 900 + "Lies die Datei /tmp/x."
        request = _request(message=wrapped)
        result = _AgentScope(self.agent, original_user_message="Lies die Datei /tmp/x.").invoke(
            request=request, session_id="sess-1")
        self.assertIsNotNone(
            result,
            "die echte User-Nachricht muss die Laengenregel umgehen - "
            "sonst routet der Router in Hermes nie",
        )

    def test_prediction_failure_keeps_the_full_surface(self):
        """Bedingung 3: ein Fehler in der Vorhersage darf nichts wegnehmen."""
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"})

        def _boom(m, c, a):
            raise RuntimeError("classifier down")

        self._patch(llr, "_predict", new=_boom)

        request = _request()
        # Die Middleware darf nicht werfen - der Host darf nicht brechen.
        result = _AgentScope(self.agent).invoke(request=request, session_id="sess-1")
        self.assertIsNone(result, "bei einem Fehler wurde geroutet")

    def test_second_request_is_not_routed_again(self):
        """Bedingung 4: session-sticky - der zweite Request bleibt unberuehrt."""
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file", "terminal"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        first = _AgentScope(self.agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(first, "die erste Route fehlte")
        self.assertTrue(self.state.initial_route_applied,
                        "der Zustand merkt sich die Route nicht")

        second = _AgentScope(self.agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNone(second, "der zweite Request wurde erneut geroutet")

    def test_agent_tool_surface_is_not_mutated(self):
        """Bedingung 5: `agent.tools` bleibt voll - die Host-Freigabe intakt."""
        self._patch_config()
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file"})
        self._patch(llr, "_predict", new=lambda m, c, a: {"file"})

        before = list(self.agent.tools)
        before_names = set(self.agent.valid_tool_names)

        result = _AgentScope(self.agent).invoke(request=_request(), session_id="sess-1")
        self.assertIsNotNone(result, "es wurde nicht geroutet")

        self.assertEqual(list(self.agent.tools), before,
                         "agent.tools wurde veraendert - die Host-Freigabe ist damit nicht mehr intakt")
        self.assertEqual(set(self.agent.valid_tool_names), before_names,
                         "valid_tool_names wurde veraendert - ein gefiltertes "
                         "Werkzeug waere nicht mehr aufloesbar")


if __name__ == "__main__":
    unittest.main()
