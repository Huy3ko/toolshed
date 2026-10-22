"""Vertragstest: First-Turn-Routing ueber die llm_request-Middleware.

Der Befund, den dieser Test festnagelt (gemessen am laufenden Hermes v0.22.2):

  `pre_llm_call` ist ein BOUNDED Hook (hermes_cli/plugins_dispatch.py:42). Er
  laeuft auf einem Daemon-Worker-Thread (Zeile 271/333). Der Aufrufer-Stack
  ist dort abgeschnitten - `build_turn_context` mit seinem `agent`-Local ist
  NICHT erreichbar. Ein Stack-Walk findet dort nie einen Agenten.

  Middleware dagegen laeuft INLINE (kein Timeout-Mechanismus, kein Worker).
  `apply_llm_request_middleware` wird aus `agent/turn_api_request.py` aufgerufen,
  wo `agent` ein Parameter von `build_api_request()` ist und damit im Stack
  liegt. Und die Middleware darf den kompletten Provider-Request ersetzen -
  also auch dessen `tools`-Liste.

Damit ist First-Turn-Routing ueber die vorhandene Plugin-API moeglich.

Dieser Test prueft die drei Bedingungen getrennt, damit ein Fehlschlag sagt,
WELCHE Bedingung verletzt ist:

  1. Die Middleware laeuft inline (MainThread), nicht in einem Worker.
  2. Sie findet den Agenten (Stack-Walk) in dieser Lage.
  3. Sie kann die `tools`-Liste des Requests ersetzen.

Und zusaetzlich den End-to-End-Vertrag: eine schmale Tool-Liste, die im
Request ankommt, wird auch als schmal gemeldet - ohne dass der Host-Agent
selbst verengt werden muss.
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import unittest
from unittest import mock

CORE = os.environ.get(
    "HERMES_CORE", os.path.expanduser("~/.hermes/hermes-agent")
)
if CORE not in sys.path:
    sys.path.insert(0, CORE)

RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime")
if os.path.isdir(RUNTIME):
    sys.path.insert(0, os.path.abspath(RUNTIME))



class _FakeAgent:
    """Minimaler Agent: so sehen die Attribute aus, die der Router liest."""

    def __init__(self, session_id="sess-mw"):
        self.session_id = session_id
        self.tools = [
            {"function": {"name": "terminal"}},
            {"function": {"name": "web_search"}},
            {"function": {"name": "read_file"}},
        ]
        self.valid_tool_names = {"terminal", "web_search", "read_file"}
        self.enabled_toolsets = ["terminal", "file", "web"]


def _agent_in_stack():
    """Derselbe Stack-Walk, den der Router benutzt."""
    for frame in inspect.stack():
        for var in frame.frame.f_locals.values():
            if var is None or isinstance(var, (str, int, float, bool, list, dict, tuple, set)):
                continue
            if hasattr(var, "valid_tool_names") and hasattr(var, "tools"):
                return var
    return None


class LlmRequestMiddlewareContract(unittest.TestCase):
    """Die drei Bedingungen fuer First-Turn-Routing, getrennt geprueft."""

    def test_middleware_runs_inline_not_in_a_worker_thread(self):
        """Bedingung 1: Middleware laeuft im Aufrufer-Thread.

        Das ist der Unterschied zum `pre_llm_call`-Hook, der auf einem
        Daemon-Worker laeuft. Nur im Aufrufer-Thread ist der Stack des
        Turn-Aufrufers - und damit der Agent - erreichbar.
        """
        seen = {}

        def probe(**kwargs):
            seen["thread"] = threading.current_thread()
            seen["is_main"] = threading.current_thread() is threading.main_thread()

        from hermes_cli.plugins_dispatch import PluginDispatchMixin

        class _M(PluginDispatchMixin):  # type: ignore[misc]
            pass

        m = _M()
        m._middleware = {"llm_request": [probe]}
        m._hooks = {}
        m._hook_failures_reported = set()
        m._plugins = []
        m._manager = m

        with mock.patch(
            "hermes_cli.plugins._delivery_manager", return_value=m
        ):
            m.invoke_middleware("llm_request", _payload_key="request",
                                request={}, original_request={})

        self.assertIn("thread", seen, "die Middleware lief nicht")
        self.assertTrue(
            seen["is_main"],
            "die llm_request-Middleware lief NICHT inline "
            f"(Thread: {getattr(seen.get('thread'), 'name', '?')}) - "
            "dann ist der Agent im Stack nicht erreichbar",
        )

    def test_agent_is_reachable_from_the_middleware_stack(self):
        """Bedingung 2: der Agent ist vom Middleware-Aufruf aus im Stack.

        Simuliert die Aufrufkette des Hosts: `build_api_request(agent, ...)`
        ruft `apply_llm_request_middleware()`.
        """
        from hermes_cli.middleware import apply_llm_request_middleware
        from hermes_cli.plugins_dispatch import PluginDispatchMixin

        found = {}

        def probe(**kwargs):
            found["agent"] = _agent_in_stack()

        class _M(PluginDispatchMixin):  # type: ignore[misc]
            pass

        m = _M()
        m._middleware = {"llm_request": [probe]}
        m._hooks = {}
        m._hook_failures_reported = set()
        m._plugins = []
        m._manager = m

        def build_api_request(agent, **kwargs):
            api_kwargs = {
                "model": "test",
                "messages": [{"role": "user", "content": "hallo"}],
                "tools": list(agent.tools),
            }
            return apply_llm_request_middleware(
                api_kwargs,
                session_id=agent.session_id,
                task_id="t1",
                turn_id="turn1",
                api_request_id="req1",
                platform="telegram",
                model="test",
                provider="custom",
                base_url="http://x",
                api_mode="chat_completions",
                api_call_count=1,
            )

        agent = _FakeAgent()
        with mock.patch(
            "hermes_cli.plugins._delivery_manager", return_value=m
        ):
            build_api_request(agent)

        self.assertIsNotNone(
            found.get("agent"),
            "aus der Middleware heraus war kein Agent im Stack erreichbar",
        )
        self.assertEqual(found["agent"].session_id, "sess-mw")

    def test_middleware_can_replace_the_tools_list_in_the_request(self):
        """Bedingung 3: die `tools`-Liste des Requests ist ersetzbar.

        Das ist der eigentliche Eingriff: eine verengte Liste, die im Request
        steht, ist das, was das Modell sieht. Eine Mutation an `agent.tools`
        allein wuerde den bereits gebauten Request nicht erreichen.
        """
        from hermes_cli.middleware import apply_llm_request_middleware
        from hermes_cli.plugins_dispatch import PluginDispatchMixin

        def probe(**kwargs):
            request = kwargs.get("request")
            if not isinstance(request, dict):
                return None
            request = dict(request)
            request["tools"] = [t for t in request.get("tools", [])
                                if t.get("function", {}).get("name") == "terminal"]
            return {"request": request, "source": "test"}

        class _M(PluginDispatchMixin):  # type: ignore[misc]
            pass

        m = _M()
        m._middleware = {"llm_request": [probe]}
        m._hooks = {}
        m._hook_failures_reported = set()
        m._plugins = []
        m._manager = m

        agent = _FakeAgent()
        api_kwargs = {
            "model": "test",
            "messages": [{"role": "user", "content": "hallo"}],
            "tools": list(agent.tools),
        }
        with mock.patch(
            "hermes_cli.plugins._delivery_manager", return_value=m
        ):
            result = apply_llm_request_middleware(
                api_kwargs,
                session_id=agent.session_id,
                task_id="t1",
                turn_id="turn1",
                api_request_id="req1",
                platform="telegram",
                model="test",
                provider="custom",
                base_url="http://x",
                api_mode="chat_completions",
                api_call_count=1,
            )

        names = [t["function"]["name"] for t in result.payload.get("tools", [])]
        self.assertEqual(names, ["terminal"],
                         f"die Tool-Liste im Request wurde nicht ersetzt: {names}")
        self.assertTrue(result.trace, "der Middleware-Trace ist leer - "
                                       "die Ersetzung wurde nicht registriert")

    def test_pre_llm_call_hook_runs_in_a_worker_thread(self):
        """Der Gegenfall: der Hook laeuft NICHT inline.

        Dieser Test haelt die Begruendung fest, warum der Router den Agenten
        im `pre_llm_call`-Pfad nicht findet. Faellt er aus, ist die
        Diagnose "Worker-Thread" nicht mehr belegt - dann muss die
        Loesungsrichtung neu gedacht werden.
        """
        from hermes_cli.plugins_dispatch import PluginDispatchMixin

        seen = {}

        def probe(**kwargs):
            seen["thread"] = threading.current_thread()
            seen["is_main"] = threading.current_thread() is threading.main_thread()

        class _M(PluginDispatchMixin):  # type: ignore[misc]
            pass

        m = _M()
        m._hooks = {"pre_llm_call": [probe]}
        m._middleware = {}
        m._hook_failures_reported = set()
        m._hook_running_callbacks = {}
        m._hook_timeout_suppressed_until = {}
        m._hook_abandoned = {}
        m._hook_max_abandoned_workers = 3
        m._hook_timeout_suppressed_seconds = 60.0
        m._hook_timeout_lock = threading.Lock()
        m._plugins = []
        m._manager = m

        m.invoke_hook("pre_llm_call", session_id="s", user_message="hallo")

        self.assertIn("thread", seen, "der Hook lief nicht")
        self.assertFalse(
            seen["is_main"],
            "der pre_llm_call-Hook laeuft NICHT mehr in einem Worker - "
            "die Worker-Diagnose ist veraltet und der Fix muss neu gedacht werden",
        )


if __name__ == "__main__":
    unittest.main()
