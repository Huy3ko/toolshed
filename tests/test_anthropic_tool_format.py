"""Vertragstest: First-Turn-Routing im Anthropic-Tool-Format.

GEMESSENER BEFUND (24.10.2026, Instanz `christiane`):

  Der Router hat drei Wochen lang auf einer Anthropic-Instanz
  (`api_mode: anthropic_messages`) nie verengt - der Grund stand im Log:

      hermes-token-router: llm_request kept full surface (reason=no_tools_in_request)

  Ursache: `_request_tool_names()` liest die Werkzeugnamen AUSSCHLIESSLICH
  im OpenAI-Format:

      fn = entry.get("function") or {}
      name = fn.get("name")

  Das Anthropic-Format fuehrt den Namen DIREKT im Eintrag
  (`{"name": ..., "description": ..., "input_schema": ...}`), ohne
  "function"-Wrapper. Die Anthropic-Konvertierung passiert in
  `agent/transports/anthropic.py` -> `convert_tools_to_anthropic()` und
  laeuft, bevor `apply_llm_request_middleware()` in
  `agent/turn_api_request.py:143` aufgerufen wird. Die Middleware sieht
  den konvertierten Request - und findet keine Namen.

  Ein echter Wire-Dump der betroffenen Instanz belegt das Format:

      body keys: ['model', 'messages', 'max_tokens', 'system', 'tools', ...]
      tools: 47 Eintraege, erste Schluessel: ['name', 'description', 'input_schema']
      any function-wrapper?: False

  Die OpenAI-Instanz (`api_mode: chat_completions`) war davon nicht
  betroffen - dort findet `_request_tool_names()` die Namen.

  Der Router muss BEIDE Formate lesen koennen. Er darf sich nicht auf ein
  Provider-Format verlassen, sonst verengt er still auf einem Teil der
  installierten Basis.

Dieser Test nagelt den Vertrag fest:

  1. Anthropic-Format: die Namen werden gelesen.
  2. Anthropic-Format: der Request wird tatsaechlich verengt.
  3. Das Anthropic-Format bleibt beim Filtern ERHALTEN - der Router
     schreibt keine OpenAI-Struktur in einen Anthropic-Request.
  4. OpenAI-Format funktioniert weiter (kein Regressionsschutz).

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
    def __init__(self, session_id="sess-anthropic"):
        self.session_id = session_id
        self.tools = [
            {"name": "terminal", "description": "d", "input_schema": {}},
            {"name": "read_file", "description": "d", "input_schema": {}},
            {"name": "web_search", "description": "d", "input_schema": {}},
            {"name": "memory_search", "description": "d", "input_schema": {}},
        ]
        self.valid_tool_names = {"terminal", "read_file", "web_search", "memory_search"}
        self.enabled_toolsets = ["terminal", "file", "web", "memory"]


def _anthropic_request(tools=None, message="Lies die Datei /tmp/notizen.txt"):
    """Ein Request im Anthropic-Format: Name direkt im Eintrag."""
    return {
        "model": "test",
        "messages": [{"role": "user", "content": message}],
        "max_tokens": 1024,
        "tools": list(tools if tools is not None else [
            {"name": "terminal", "description": "d", "input_schema": {}},
            {"name": "read_file", "description": "d", "input_schema": {}},
            {"name": "web_search", "description": "d", "input_schema": {}},
            {"name": "memory_search", "description": "d", "input_schema": {}},
        ]),
    }


class _AgentScope:
    """Minimaler Scope, in dem der Agent ein lokaler Name ist.

    `original_user_message` im Stack bildet den Core nach: Hermes reicht
    die ECHTE User-Nachricht als Keyword-Parameter an `build_api_request`
    weiter, und genau diese Funktion liegt im Stack, wenn die Middleware
    laeuft.
    """

    def __init__(self, agent, original_user_message=None):
        self.agent = agent
        self.original_user_message = original_user_message

    def invoke(self, **kwargs):
        agent = self.agent  # noqa: F841 - genau so liegt es im Host-Stack
        original_user_message = self.original_user_message  # noqa: F841
        return llr.llm_request_middleware(**kwargs)


class AnthropicToolFormatTest(unittest.TestCase):
    def setUp(self):
        self.agent = _FakeAgent()
        self.state = RouterState()
        self.state.authorized_toolsets = {"file", "terminal", "web", "memory"}
        self.state.authorization_captured = True
        self.agent._hermes_token_router_state = self.state
        self._patches = []
        self._patch("state._get_router_state", lambda agent: self.state)
        self._patch("config._load_config", dict)
        self._patch("config._get_profile_config", lambda cfg: {
            "enabled": True,
            "mode": "active",
            "floor_toolsets": ["terminal", "file"],
            "long_message_decline_chars": 2000,
            "short_message_bypass_chars": 0,
            "confidence_threshold": 0.0,
        })
        self._patch("policy._get_available_toolsets",
                    lambda: {"file", "terminal", "web", "memory"})
        self._patch("router_tools._resolve_toolset_to_tool_names",
                    lambda ts: {"read_file", "terminal"})
        self._patch_obj(llr, "_predict", lambda m, c, a: {"file"})

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _patch(self, target, value):
        """`target` ist "modul.attr" (String) - der Wert steckt in `value`."""
        p = mock.patch(target, value)
        p.start()
        self._patches.append(p)
        return p

    def _patch_obj(self, module, attr, value):
        """Patch auf einem Modulobjekt mit explizitem Attributnamen."""
        p = mock.patch.object(module, attr, value)
        p.start()
        self._patches.append(p)
        return p

    def test_anthropic_tool_names_are_read(self):
        """Bedingung 1: die Namen stehen im Anthropic-Eintrag, nicht in 'function'."""
        names = llr._request_tool_names(_anthropic_request())
        self.assertEqual(
            names,
            {"terminal", "read_file", "web_search", "memory_search"},
            f"die Anthropic-Werkzeugnamen wurden nicht gelesen: {names}",
        )

    def test_anthropic_request_really_gets_narrowed(self):
        """Bedingung 2: der Anthropic-Request wird verengt.

        Vor dem Fix lieferte `_request_tool_names` eine leere Menge, der
        Router bailed mit `no_tools_in_request` und liess die volle
        Flaeche stehen.
        """
        result = _AgentScope(self.agent).invoke(
            request=_anthropic_request(), session_id="sess-anthropic", turn_id="t1",
        )
        self.assertIsNotNone(
            result,
            "der Anthropic-Request wurde nicht verengt - "
            "das ist der Fehler, der auf der betroffenen Instanz auftrat",
        )
        names = {t.get("name") or (t.get("function") or {}).get("name")
                 for t in result["request"]["tools"]}
        self.assertEqual(
            names, {"read_file", "terminal"},
            f"die Tool-Liste ist nicht die erwartete: {names}",
        )

    def test_anthropic_format_survives_the_filter(self):
        """Bedingung 3: der Router schreibt kein OpenAI-Format zurueck.

        Ein Anthropic-Request, der nach der Verengung `function`-Wrapper
        traegt, wuerde beim Anthropic-Transport als unbekanntes Format
        scheitern.
        """
        result = _AgentScope(self.agent).invoke(
            request=_anthropic_request(), session_id="sess-anthropic", turn_id="t1",
        )
        self.assertIsNotNone(result, "der Anthropic-Request wurde nicht verengt")
        for entry in result["request"]["tools"]:
            self.assertNotIn(
                "function", entry,
                "der Router hat OpenAI-Struktur in einen Anthropic-Request geschrieben",
            )
            self.assertIn("name", entry, "der Werkzeugname fehlt nach der Verengung")

    def test_openai_format_still_works(self):
        """Bedingung 4: das OpenAI-Format bleibt unveraendert unterstuetzt."""
        openai_request = {
            "model": "test",
            "messages": [{"role": "user", "content": "Lies die Datei /tmp/notizen.txt"}],
            "tools": [
                {"type": "function", "function": {"name": "terminal"}},
                {"type": "function", "function": {"name": "read_file"}},
                {"type": "function", "function": {"name": "web_search"}},
                {"type": "function", "function": {"name": "memory_search"}},
            ],
        }
        names = llr._request_tool_names(openai_request)
        self.assertEqual(
            names,
            {"terminal", "read_file", "web_search", "memory_search"},
            f"das OpenAI-Format wurde kaputt gemacht: {names}",
        )

        result = _AgentScope(self.agent).invoke(
            request=openai_request, session_id="sess-openai", turn_id="t1",
        )
        self.assertIsNotNone(result, "der OpenAI-Request wurde nicht verengt")
        kept = {t["function"]["name"] for t in result["request"]["tools"]}
        self.assertEqual(kept, {"read_file", "terminal"},
                         f"die Tool-Liste ist nicht die erwartete: {kept}")


if __name__ == "__main__":
    unittest.main()
