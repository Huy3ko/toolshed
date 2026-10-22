"""Vertragstest: die Middleware muss im PAKETkontext des Hosts funktionieren.

GEMESSENER BEFUND (23.10.2026): Der Host laedt ein Verzeichnis-Plugin als
Paket `hermes_plugins.<slug>` (hermes_cli/plugins_loader.py:704-725) mit
`submodule_search_locations=[plugin_dir]` und legt den Namespace-Parent an.
Das Plugin-Verzeichnis kommt NICHT in sys.path - und `src/` auch nicht.

Damit sind flache Imports (`from config import ...`) in einem Plugin-Submodul
nicht aufloesbar. Die Middleware nutzte flache Imports und fiel damit in
BETRIEB in jeden `except`-Zweig: sie gab still `None` zurueck und verengte
nie - ohne jede Fehlermeldung.

DIE BLINDSTELLE, DIE DIESEN FEHLER VERSTECKTE:
`tests/conftest.py` legt `src/` in sys.path. Dadurch war `import config`
in der Testsuite IMMER aufloesbar - der Test sah eine Umgebung, die der
Host nie hat. Deshalb sind hier die Pfade, die der Host nicht hat, waehrend
des Tests aktiv aus sys.path zu entfernen.

Der Ausfall ist von aussen nicht unterscheidbar: die Middleware liefert in
beiden Faellen `None` (fail-open). Der Test prueft deshalb den WEG, nicht
das Ergebnis - die Middleware darf den Import-Schritt nicht ueberspringen.

Reihenfolge der Schranken in `llm_request_middleware`:
  1. request ist kein dict            -> None
  2. kein Agent im Stack              -> None
  3. keine tools im Request           -> None
  4. Import RouterState               -> <-- geprueft
  5. Import config/policy/router_tools -> <-- geprueft
Um Schranke 4/5 zu erreichen, braucht der Test einen Agenten im Stack und
einen Request mit tools.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "src" / "toolshed"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def host_package(monkeypatch):
    """Laedt das Plugin exakt so, wie der Host es laedt.

    - Namespace-Parent `hermes_plugins` anlegen
    - Plugin als `hermes_plugins.<slug>` mit submodule_search_locations laden
    - `src/` und das Repo-Root aus sys.path entfernen: der Host hat sie nicht
    - Top-Level-`config`/`policy`/`state`/`router_tools` aus sys.modules
      entfernen: ein frueherer Test kann sie gecacht haben, sonst waere
      dieser Test reihenfolgeabhaengig und blind.
    """
    # ALLE Pfade entfernen, die auf einen Plugin-Baum zeigen: `src/`, das
    # Repo-Root und das Release-Payload `runtime/`. Andere Testdateien legen
    # `runtime/` in sys.path - bliebe das stehen, faende ein flacher Import
    # das gecachte `state` und der Test waere reihenfolgeabhaengig blind.
    for entry in list(sys.path):
        if not entry:
            continue
        resolved = str(Path(entry).resolve()) if Path(entry).exists() else entry
        if resolved in (str(PLUGIN_ROOT.resolve()), str(REPO_ROOT.resolve()),
                        str((REPO_ROOT / "src").resolve()),
                        str((REPO_ROOT / "runtime").resolve())):
            sys.path.remove(entry)

    # Gecachte Top-Level-Module entfernen (monkeypatch stellt sie wieder
    # her). Ohne das findet ein flacher Import in der vollen Suite das
    # gecachte Modul und der Test verliert seine Aussagekraft.
    for name in ("config", "policy", "state", "router_tools"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    ns_parent = "hermes_plugins"
    if ns_parent not in sys.modules:
        ns = types.ModuleType(ns_parent)
        ns.__path__ = []
        ns.__package__ = ns_parent
        sys.modules[ns_parent] = ns

    slug = "ts_pkgctx_probe"
    full = f"{ns_parent}.{slug}"
    spec = importlib.util.spec_from_file_location(
        full, str(PLUGIN_ROOT / "__init__.py"),
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = full
    mod.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[full] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(full, None)
        raise
    yield full, mod
    sys.modules.pop(full, None)
    for name in list(sys.modules):
        if name.startswith(f"{full}."):
            sys.modules.pop(name, None)


class _StackAgent:
    """Minimaler Agent, wie `_find_agent_in_stack` ihn erkennt."""

    def __init__(self):
        self.tools = ["terminal", "web_search"]
        self.valid_tool_names = ["terminal", "web_search"]
        self.enabled_toolsets = ["terminal", "web"]
        self.session_id = "pkgctx"


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": name}}


def test_host_paths_absent_from_sys_path(host_package):
    """Voraussetzung: der Host hat weder das Plugin-Dir noch `src/` im Pfad."""
    _full, _mod = host_package
    assert str(PLUGIN_ROOT.resolve()) not in [
        str(Path(e).resolve()) for e in sys.path if e and Path(e).exists()
    ]
    assert str((REPO_ROOT / "runtime").resolve()) not in [
        str(Path(e).resolve()) for e in sys.path if e and Path(e).exists()
    ]


def test_flat_imports_do_not_resolve_to_the_plugin_modules(host_package):
    """Die Voraussetzung des Befunds: flache Imports treffen NICHT das Plugin.

    `config`/`policy`/`state` liegen im Repo-Root als (veraltete) Root-Kopien.
    In der Testsuite kann pytest den Repo-Root in sys.path legen - dann waere
    ein flacher Import erfolgreich, aber er lieferte die FALSCHE Datei (die
    Root-Kopie, nicht das Plugin-Modul).

    Geprueft wird deshalb nicht "flacher Import scheitert", sondern: ein
    flacher Import loest nicht auf das Plugin-Modul auf. Genau diese
    Verwechslung liess den Betriebsfehler unentdeckt.
    """
    _full, _mod = host_package
    plugin_dir = str(PLUGIN_ROOT.resolve())
    for name in ("config", "policy", "state", "router_tools"):
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue  # noch besser: gar nicht aufloesbar
        origin = getattr(mod, "__file__", "") or ""
        assert not origin.startswith(plugin_dir), (
            f"Der flache Import von {name!r} liefert die Plugin-Datei "
            f"{origin} - im Host waere das nicht der Fall: dort faellt er auf "
            "ModuleNotFoundError, und die Middleware liefert still None."
        )


def test_router_internals_importable_in_package_context(host_package):
    """Die Router-Internen muessen im Paketkontext RELATIV auflösbar sein."""
    full, _mod = host_package
    for name in ("config", "policy", "state", "router_tools"):
        mod = importlib.import_module(f"{full}.{name}")
        assert mod is not None


def test_middleware_reaches_the_router_internals_in_package_context(
    host_package, caplog
):
    """VERHALTENSTEST: die Middleware darf den Import-Schritt nicht überspringen.

    Mit flachen Imports wirft der Import im Paketkontext
    `ModuleNotFoundError`; die Middleware fängt ihn im `except`-Zweig ab
    und liefert still `None` - von aussen nicht zu unterscheiden von einem
    sauberen fail-open.

    Beobachtbar ist deshalb die Log-Meldung des except-Zweigs
    "router internals unavailable at llm_request". Sie darf im Paketkontext
    NICHT auftreten - sonst läuft der Router im Betrieb stumm.
    """
    full, _mod = host_package
    sub = importlib.import_module(f"{full}.llm_request_routing")

    agent = _StackAgent()
    request = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Lies die Datei /tmp/x."}],
        "tools": [_tool("terminal"), _tool("web_search")],
    }

    caplog.set_level(logging.DEBUG, logger="hermes_plugins.hermes_token_router")
    with caplog.at_level(logging.DEBUG, logger="hermes_plugins.hermes_token_router"):
        found = sub._find_agent_in_stack()
        assert found is agent, "Test-Voraussetzung: Agent nicht im Stack erkannt"

        result = sub.llm_request_middleware(
            request=request,
            original_request=dict(request),
            session_id="pkgctx",
        )

    # fail-open bleibt: kein Absturz, keine Verengung (keine Autorisierung).
    assert result is None

    unavailable = [
        rec.getMessage() for rec in caplog.records
        if "router internals unavailable" in rec.getMessage()
    ]
    assert not unavailable, (
        "Die Middleware hat die Router-Internen im Paketkontext nicht "
        f"erreicht - im Betrieb würde sie still ohne Verengung laufen: {unavailable}"
    )

    state_unavailable = [
        rec.getMessage() for rec in caplog.records
        if "router state unavailable" in rec.getMessage()
    ]
    assert not state_unavailable, (
        f"Der RouterState-Import schlug im Paketkontext fehl: {state_unavailable}"
    )
