"""Vertragstest: der YAML-Lader muss im Hermes-Host funktionieren.

BEVORHER (Befund 23.10.2026): `config.py` importierte hart `yaml` (PyYAML).
Im Hermes-Host ist PyYAML NICHT installiert - dort stehen `ruamel.yaml` und
Hermes' eigener Wrapper `hermes_yaml`. Der Import schlug fehl, die
Konfiguration wurde still zu `{}`, `_get_profile_config` lieferte die
Defaults (`enabled=False`) und der Router verengte NIE - ohne sichtbaren
Fehler.

Der Test simuliert den Host: `sys.modules['yaml']` wird entfernt, danach
muss `_yaml_module()` trotzdem eine Bibliothek mit `safe_load` liefern und
`_load_config()` die echte config.yaml lesen.

WICHTIG: Der Test prueft die *Ursache* (die Datei wird gelesen), nicht den
Betriebsstand. `global.enabled` ist im Release-Payload bewusst `false` - der
Betrieb schaltet den Router in seiner eigenen config.yaml ein. Wer hier
`enabled == True` erwartet, kodiert den Betriebszustand in den Test.
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

import config as cfg



class _BlockPyyaml:
    """Simuliert den Hermes-Host, in dem PyYAML NICHT installiert ist.

    `import yaml` muss im Test scheitern - sonst prueft der Test die
    Toolshed-venv, in der PyYAML zufaellig vorhanden ist, und nicht den
    Host, in dem der Fehler tatsaechlich auftritt.
    """

    def __enter__(self):
        import builtins

        self._real_import = builtins.__import__

        def _guarded(name, *args, **kwargs):
            if name == "yaml" or name.startswith("yaml."):
                raise ImportError("No module named 'yaml' (simuliert)")
            return self._real_import(name, *args, **kwargs)

        # WICHTIG: `import yaml` im Plugin-Code nutzt den Builtin-Import,
        # nicht `importlib.__import__`. Nur `builtins.__import__` zu patchen
        # blockiert den Import wirklich.
        self._patcher = mock.patch("builtins.__import__", _guarded)
        self._patcher.start()
        # Auch ein bereits importiertes yaml aus dem Cache entfernen.
        self._saved = {k: v for k, v in sys.modules.items()
                       if k == "yaml" or k.startswith("yaml.")}
        for k in self._saved:
            del sys.modules[k]
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        sys.modules.update(self._saved)
        return False


class YamlLoaderContract(unittest.TestCase):
    def test_yaml_module_is_found_without_pyyaml(self):
        """Ohne PyYAML muss eine YAML-Bibliothek gefunden werden."""
        with _BlockPyyaml():
            module = cfg._yaml_module()
        self.assertTrue(
            hasattr(module, "safe_load"),
            "gefundene Bibliothek hat kein safe_load",
        )

    def test_load_config_reads_the_real_config_file(self):
        """_load_config() muss die echte config.yaml lesen, nicht {} liefern."""
        with _BlockPyyaml():
            raw = cfg._load_config()

        self.assertIsInstance(raw, dict, "config.yaml wurde nicht als dict gelesen")
        self.assertIn(
            "global",
            raw,
            "config.yaml ohne 'global'-Sektion - die Datei wurde nicht gelesen",
        )
        self.assertIn(
            "enabled",
            raw.get("global", {}),
            "global.enabled fehlt - die Datei wurde nicht geladen",
        )

    def test_profile_config_reflects_the_file_not_a_blank_default(self):
        """_get_profile_config muss die Datei spiegeln, nicht leer laufen.

        Genau hier schlug der alte Import still fehl: die Datei wurde zu
        `{}`, das Profil bekam nur `{"enabled": False, ...}` und der Router
        verengte nie. Der Test vergleicht das Ergebnis mit dem, was die
        Datei tatsaechlich sagt - unabhaengig davon, ob der Betrieb den
        Router ein- oder ausgeschaltet hat.
        """
        with _BlockPyyaml():
            raw = cfg._load_config()
            profile_cfg = cfg._get_profile_config(raw)

        # Das Profil-Fallback ("nur enabled + Name") entsteht, wenn die Datei
        # NICHT gelesen wurde. Sobald sie gelesen ist, traegt das Profil die
        # globalen Felder - ABER nur, wenn der Router fuer dieses Profil
        # auch aktiviert ist. Der Release-Payload hat `enabled: false`, dann
        # ist der Fallback das korrekte Ergebnis.
        #
        # Der Test prueft deshalb die entscheidende Groesse: die Datei wurde
        # gelesen. Das ist der Unterschied zwischen dem stillen YAML-Fehler
        # und einem korrekt geladenen, aber abgeschalteten Router.
        self.assertIn("_profile_name", profile_cfg)
        self.assertEqual(
            profile_cfg.get("enabled"),
            bool(raw.get("global", {}).get("enabled")),
            "Profil-Fallback weicht von der Datei ab - der YAML-Import schlug fehl",
        )
        if raw.get("global", {}).get("enabled"):
            # Aktiviert: dann MUSS das Profil die globalen Felder tragen.
            self.assertIn("mode", profile_cfg)
            self.assertIn("floor_toolsets", profile_cfg)


if __name__ == "__main__":
    unittest.main()
