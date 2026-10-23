"""Vertragstest: das flache Root-Payload muss eigenstaendig funktionieren.

GEMESSENER BEFUND (23.10.2026): Das Repo-Root ist die flache Loader-Variante
des Plugins (siehe `__about__.py`: "Version for the flat Hermes plugin
loader (root directory, no package)"). Der Hermes-Install-Payload weist
genau diese Form auf: `~/.hermes/plugins/hermes-token-router/` enthaelt
`__init__.py`, `config.py`, `policy.py`, `state.py` usw. FLACH - ohne
Package-Parent, ohne `src/` und ohne `runtime/` im sys.path.

DIE ZWEI BEFUNDE, DIE DIESER TEST FESTNAGELT:

1. FEHLENDE DATEI. `__init__.py` importiert `llm_request_routing` (relativ
   und flach). Die Datei lag nur in `src/toolshed/` und `runtime/`, nicht im
   Root. Im flachen Layout scheitert der Import mit
   `ModuleNotFoundError: No module named 'llm_request_routing'` - das
   Root-Payload ist damit gar nicht importierbar.

2. STILLER REGELVERLUST. `policy.py` holt den YAML-Loader per
   `from .config import _yaml_module`. Relativ heisst: es braucht einen
   Package-Parent. Im flachen Layout gibt es keinen, der Import schlaegt
   fehl, der Exception-Handler schluckt es, und `_build_dynamic_mcp_rules()`
   liefert 0 statt 4 Regeln - OHNE Fehlermeldung. Die MCP-Server sind damit
   nicht mehr routbar.

Die Blindstelle, die diesen Fehler versteckt hat: `tests/conftest.py` legt
`src/` in den sys.path. Dadurch war `from .config import ...` in der
Testsuite IMMER aufloesbar - der Test sah eine Umgebung, die der flache
Loader nie hat. Dieser Test prueft deshalb in einem FRISCHEN Interpreter
(`-c`), in dem ausschliesslich das Repo-Root als sys.path-Eintrag liegt.

WICHTIG - WARUM DIESER TEST IN EINEM SUBPROCESS LAEUFT:
pytest leitet aus dem Repo-Root (das ein `__init__.py` traegt) das Paket
`toolshed` ab und setzt den PARENT `/srv/companion/hermes_hugo` in den
sys.path. Dadurch laeuft `import toolshed` im Testprozess auf das
REPO-ROOT, nicht auf `src/toolshed` - und der Root-`__init__.py` wird
beim Collection mitimportiert. Faellt die Datei `llm_request_routing.py`
dort weg, bricht die GANZE Suite mit einem Collection-Error ab, statt
einen Testfehler zu melden. Der Subprocess umgeht das: er startet frisch
und laedt das Root-Payload ueber einen expliziten Dateipfad, ohne den
Package-Parent des pytest-Prozesses zu erben.

Reihenfolge der Schranken (der Test sagt, WELCHE schiefgeht):
  1. Root-Payload importierbar (flach)   -> sonst: fehlende Datei
  2. YAML-Loader im flachen Layout        -> sonst: stiller Regelverlust
  3. MCP-Regelbau im flachen Layout       -> sonst: 0 statt N Regeln
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Eigene Hermes-Config fuer die Messung: vier MCP-Server, alle aktiviert.
# Damit ist die Referenzzahl fest und unabhaengig von der Host-Config
# (die sich mit jedem Hermes-Stand aendert).
#
# Warum eine eigene Config statt der Host-Config: `_build_dynamic_mcp_rules()`
# liest die MCP-Server aus `$HERMES_HOME/config.yaml`. Ohne diese Fixture
# hinge die Erwartung an der Host-Config - und `test_e2e_real_dispatcher.py`
# setzt `HERMES_HOME` zusaetzlich auf `.e2e-home` (dort ohne config.yaml).
# Der Test waere dann von der Reihenfolge der Suite abhaengig.
HERMES_HOME_FIXTURE = Path(
    os.environ.get("TOOLSHED_FLAT_TEST_HOME", "/tmp/toolshed_flat_test_home")
)
MCP_SERVER_COUNT = 4


def _ensure_fixture_config() -> None:
    """Legt die Test-Config mit definierter MCP-Server-Zahl an."""
    HERMES_HOME_FIXTURE.mkdir(parents=True, exist_ok=True)
    (HERMES_HOME_FIXTURE / "config.yaml").write_text(
        "mcp_servers:\n"
        + "".join(
            f"  server{i}:\n    enabled: true\n" for i in range(MCP_SERVER_COUNT)
        ),
        encoding="utf-8",
    )

# Der Interpreter des Repos - der Test laeuft NICHT im Host-Interpreter,
# sondern im Repo-venv: die Assertion gilt fuer den Code, nicht fuer die
# Umgebung des Test runners.
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = Path(sys.executable)


def _run_in_flat_root(payload: str) -> subprocess.CompletedProcess:
    """Fuehrt `payload` in einem frischen Interpreter aus.

    Der Interpreter startet mit LEEREM sys.path ausser dem Repo-Root:
    kein `src/`, kein `runtime/`, kein Package-Parent. Genau so sieht
    der flache Loader den Baum.
    """
    # Das Geruest wird ohne Einzug geschrieben und das Payload mit
    # `textwrap.dedent` eingefuegt. Dedent allein laesst die fuehrenden
    # Leerzeichen des Geraests stehen - der Interpreter stuertzt dann
    # mit "IndentationError".
    #
    # WICHTIG: `sys.path[:] = [ROOT]` waere zu aggressiv - es entfernt
    # auch die Standardpfade, und dann findet selbst das eingebaute
    # `__future__` sein Modul nicht mehr. Stattdessen werden die
    # Plugin-Baum-Pfade gezielt ENTFERNT: `src/`, `runtime/` und das
    # Repo-Root als Package-Wurzel. Der flache Loader hat genau diese
    # Pfade nicht.
    kopf = (
        "import importlib.util, os, sys, types\n"
        "\n"
        f"ROOT = {str(REPO_ROOT)!r}\n"
        "\n"
        "# Die Plugin-Baum-Pfade entfernen: `src/`, `runtime/` und das\n"
        "# Repo-Root. Der flache Loader hat sie NICHT - ein flacher Import\n"
        "# darf hier kein gecachtes Modul finden, sonst waere der Test blind.\n"
        "import pathlib\n"
        "_verboten = {\n"
        "    str(pathlib.Path(ROOT).resolve()),\n"
        "    str(pathlib.Path(ROOT, 'src').resolve()),\n"
        "    str(pathlib.Path(ROOT, 'runtime').resolve()),\n"
        "}\n"
        "sys.path[:] = [p for p in sys.path\n"
        "                if str(pathlib.Path(p).resolve() if p else p) not in _verboten]\n"
        "\n"
        "# NUR das Repo-Root kommt hinzu - der flache Loader legt genau\n"
        "# das Verzeichnis des Plugins in den sys.path.\n"
        "sys.path.insert(0, ROOT)\n"
        "\n"
        "# hermes_cli.config nachbauen: get_config() liefert leer, damit\n"
        "# der Fallback-Zweig in policy.py ueberhaupt erreicht wird.\n"
        'm = types.ModuleType("hermes_cli.config")\n'
        "m.get_config = lambda: {}\n"
        'sys.modules["hermes_cli"] = types.ModuleType("hermes_cli")\n'
        'sys.modules["hermes_cli.config"] = m\n'
        "\n"
    )
    script = kopf + textwrap.dedent(payload).strip("\n") + "\n"
    # Die Umgebung wird BEREINIGT - aus zwei gemessenen Gruenden:
    #
    # 1. `PYTHONPATH` zeigt im Testprozess auf das Hermes-Environment und
    #    damit auf den PARENT des Repo-Root. Uebernommen wuerde der Pfad
    #    vom Repo-Root nach `src/` aufloesen - der Subprocess saehe dann
    #    das Package-Layout statt des flachen.
    # 2. `HERMES_HOME` wird von `test_e2e_real_dispatcher.py` auf
    #    `.e2e-home` gesetzt (dort ohne `config.yaml`). Uebernommen
    #    wuerde der Subprocess eine Config OHNE mcp_servers lesen und der
    #    Test waere von der Reihenfolge der Suite abhaengig.
    #
    # Der Test misst deshalb gegen eine selbst gesetzte Config mit
    # definiertem Inhalt - nicht gegen die Host-Config, die sich mit
    # jedem Hermes-Stand aendert.
    _ensure_fixture_config()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "PYTHONHOME", "HERMES_HOME")
    }
    env["HERMES_HOME"] = str(HERMES_HOME_FIXTURE)
    return subprocess.run(
        [str(VENV_PYTHON), "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
        check=False,  # der Rueckgabewert wird im Test ausgewertet
    )


def test_root_payload_is_importable_flat():
    """Das Root-Payload muss im flachen Layout importierbar sein.

    Ohne die Datei `llm_request_routing.py` im Root scheitert schon der
    Import: `ModuleNotFoundError: No module named 'llm_request_routing'`.
    """
    result = _run_in_flat_root(
        """
        spec = importlib.util.spec_from_file_location(
            "flat_root_probe", os.path.join(ROOT, "__init__.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "llm_request_middleware"), (
            "die Middleware wurde nicht importiert"
        )
        print("OK")
        """
    )
    assert result.returncode == 0, (
        "Das Root-Payload ist im flachen Layout NICHT importierbar - "
        "flaecher Loader kann das Plugin nicht laden:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_mcp_rules_survive_the_flat_layout():
    """Der MCP-Regelbau darf im flachen Layout nicht still verlieren.

    `policy.py` holt den YAML-Loader per relativem Import. Ohne
    Package-Parent schlaegt der fehl und der Exception-Handler schluckt
    ihn - die Folge sind 0 statt 4 Regeln, ohne jede Fehlermeldung.
    """
    result = _run_in_flat_root(
        """
        spec = importlib.util.spec_from_file_location(
            "flat_policy_probe", os.path.join(ROOT, "policy.py")
        )
        pol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pol)
        regeln = len(pol._build_dynamic_mcp_rules())
        print("RULES=%d" % regeln)
        """
    )
    assert result.returncode == 0, (
        "policy.py ist im flachen Layout nicht ladbar:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    erwartet = f"RULES={MCP_SERVER_COUNT}"
    assert erwartet in result.stdout, (
        "der MCP-Regelbau verliert im flachen Layout Regeln - "
        "die MCP-Server sind damit nicht mehr routbar. "
        f"Erwartet {MCP_SERVER_COUNT}, erhalten: {result.stdout.strip()}"
    )


def test_yaml_loader_is_reachable_flat():
    """Der YAML-Loader muss im flachen Layout erreichbar sein.

    Das Repo-Root traegt die Datei `config.py` mit `_yaml_module()`. Sie
    muss ohne Package-Parent auffindbar sein - sonst faellt die
    Konfiguration still auf `{}` zurueck und der Router verengt nie.
    """
    result = _run_in_flat_root(
        """
        import config
        modul = config._yaml_module()
        assert hasattr(modul, "safe_load"), (
            "gefundene Bibliothek hat kein safe_load"
        )
        print("OK")
        """
    )
    assert result.returncode == 0, (
        "config.py ist im flachen Layout nicht ladbar oder der YAML-Loader "
        f"nicht erreichbar:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout