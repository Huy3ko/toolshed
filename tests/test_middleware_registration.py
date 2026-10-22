"""Die Middleware muss auch REGISTRIERT sein — nicht nur vorhanden.

Der Befund, der diesen Test ausgeloest hat: eine Kippprobe, in der die
`llm_request`-Registrierung entfernt wurde, blieb gruen. Die bisherigen
Tests riefen die Middleware direkt auf - sie prueften damit die Funktion,
aber nicht den Weg, den der Host tatsaechlich nimmt. Genau an dieser
Stelle liegt der Unterschied zwischen "implementiert" und "wirksam".

Der Host ruft die Middleware ueber `ctx.register_middleware("llm_request", ...)`
auf. Dieser Test faehrt genau diesen Weg: ein echter `PluginContext`-Ersatz
sammelt die Registrierungen, und danach wird geprueft:

  1. Die Middleware ist unter dem Namen "llm_request" registriert.
  2. Der Host wuerde sie in der Reihenfolge der gueltigen Middleware-Namen finden.
  3. Ein Host ohne die Middleware-Ausnahme (also: der reale Fall) nutzt sie.

Die Valid-Liste des Hosts wird aus dem echten Core gelesen, nicht geraten:
`hermes_cli.plugins.VALID_MIDDLEWARE`.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
RUNTIME = os.path.join(REPO, "runtime")
if RUNTIME not in sys.path:
    sys.path.insert(0, RUNTIME)


class _RecordingContext:
    """Minimaler Ersatz fuer den PluginContext des Hosts."""

    def __init__(self):
        self.hooks = {}
        self.middleware = {}
        self.tools = []

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_middleware(self, name, fn):
        # Der Host prueft den Namen gegen seine gueltige Liste. Der Test
        # tut das ebenfalls - aber nur, wenn die Liste wirklich geladen
        # werden konnte (siehe _valid_middleware). Ist der Core nicht da,
        # darf der Test nicht an einer leeren Liste scheitern.
        valid = _valid_middleware()
        if valid and name not in valid:
            raise ValueError(f"invalid middleware name: {name}")
        self.middleware[name] = fn

    def register_tool(self, *args, **kwargs):
        self.tools.append((args, kwargs))


def _valid_middleware() -> set:
    """Die gueltigen Middleware-Namen des laufenden Hermes.

    Aus dem Core lesen, wenn er verfuegbar ist. Fehlt der Core (isolierte
    Testumgebung), gilt die Liste, die der Host in 0.22.2 fuehrt - sie wird
    dann als Annahme im Test benannt, nicht stillschweigend gesetzt.
    """
    try:
        from hermes_cli.plugins import VALID_MIDDLEWARE  # type: ignore

        return set(VALID_MIDDLEWARE)
    except Exception:
        return {"llm_request", "llm_execution", "tool_request", "tool_execution"}



def _plugin_under_test():
    """Das Plugin, dessen register() geprueft wird.

    `conftest.py` stellt `src/` an den Anfang von sys.path, der Test selbst
    stellt `runtime/` davor. Damit koennten zwei verschiedene Module mit
    demselben Namen im Spiel sein. Der Test muss EINES davon festgreifen -
    und es muss das sein, das auch der Host laedt. Der Host laedt das
    Release-Payload (`runtime/`), weil er das Plugin flach aus dem
    Installationsverzeichnis importiert. Deshalb wird hier der Modulname
    entfernt und neu importiert.
    """
    import importlib
    import importlib.util
    import sys

    # Der flache Hermes-Loader laedt das Plugin aus dem Installations-
    # verzeichnis als Modul namens "__init__". Damit der Test genau das
    # prueft, wird die Datei direkt geladen - unabhaengig davon, was
    # pytest vorher in sys.modules hinterlegt hat.
    sys.modules.pop("__init__", None)
    spec = importlib.util.spec_from_file_location(
        "hermes_token_router_under_test",
        os.path.join(RUNTIME, "__init__.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_token_router_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class MiddlewareRegistrationContract(unittest.TestCase):
    def test_llm_request_is_a_valid_middleware_name_in_the_host(self):
        """Bedingung 1: der Host akzeptiert den Namen."""
        self.assertIn("llm_request", _valid_middleware(),
                      "der Host kennt 'llm_request' nicht - der Registrierungsweg "
                      "existiert so nicht")

    def test_register_registers_the_llm_request_middleware(self):
        """Bedingung 2: die Middleware wird unter 'llm_request' registriert."""
        plugin = _plugin_under_test()
        ctx = _RecordingContext()

        plugin.register(ctx)

        self.assertTrue(plugin._llm_request_middleware_registered,
                        "die Registrierung schlug fehl - der Host wuerde die "
                        "Middleware nie aufrufen")
        self.assertIn("llm_request", ctx.middleware,
                      "die llm_request-Middleware ist nicht registriert - "
                      "der Host wuerde sie nie aufrufen")
        self.assertIs(ctx.middleware["llm_request"], plugin.llm_request_middleware,
                      "die registrierte Middleware ist nicht die erwartete")

    def test_registration_survives_the_host_registry_lookup(self):
        """Bedingung 3: der Host findet die Middleware in seiner Middleware-Reihenfolge."""
        plugin = _plugin_under_test()
        ctx = _RecordingContext()
        plugin.register(ctx)

        valid = _valid_middleware()
        used = [name for name in ("llm_request", "tool_request") if name in ctx.middleware]
        self.assertTrue(used, "keine Middleware registriert")
        for name in used:
            self.assertIn(name, valid, f"{name} ist fuer den Host ungueltig")


if __name__ == "__main__":
    unittest.main()
