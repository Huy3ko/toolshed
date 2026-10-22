# Fix-Historie: Session-gebundene Agenten-Auflösung (2026-10-22)

## Das Problem

Der Token-Router verengte die Werkzeugfläche **nie**. Im Betrieb-Log stand in
jedem Turn:

```
WARNING hermes_token_router: no agent reference; full set fallback
```

Der Router lief, registrierte seine Hooks, wurde geladen — und tat nichts.
Das ist der schlimmere Fall: ein aktiviertes Feature, das stillschweigend
ausfällt.

## Die Ursache

Der Router fand den Agenten über einen **Stack-Walk**: Er suchte in den
aufrufenden Frames ein Local namens `agent` mit `tools`-Attribut. Das Local
stammt aus `agent/turn_context.py::build_turn_context(agent, ...)`.

Gemessen am laufenden Hermes v0.22.2 (Commit `d923a5eb48`), nicht aus dem
Code gelesen:

| Aufrufstelle | Thread | Stack-Walk möglich? |
|---|---|---|
| `pre_llm_call` | `hermes-hook-cb` (Daemon-Worker) | **nein** |
| `on_session_start` | `hermes-hook-cb` (Daemon-Worker) | **nein** |
| `subagent_stop` | `MainThread` | ja |
| `tool_request` (Middleware) | `MainThread` (inline) | ja |

`pre_llm_call` ist in `hermes_cli/plugins_dispatch.py:42` ein **BOUNDED Hook**
und läuft laut Zeile 271 auf einem Daemon-Worker („Run one callback on a
daemon worker"). Der Aufrufer-Stack ist dort abgeschnitten; `build_turn_context`
mit seinem `agent`-Local ist **nicht erreichbar**.

Der Stack-Walk konnte dort also **prinzipiell nie** etwas finden — auf keinem
Host, in keiner Version. Das war kein Konfigurationsfehler, sondern ein
Fehler im Annahmen des Routers über Hermes' Ausführungsmodell.

## Die Lösung

Zwei Änderungen, beide in `runtime/` (kein Eingriff in den Hermes-Core):

1. **Auflösung über die Session statt über den Stack.**
   `_route_tool_surface` löst den Agenten zuerst über
   `_get_agent_ref(session_id)` auf; der Stack-Walk bleibt Notnagel für
   inline laufende Aufrufstellen.

2. **Lernpunkt an einer inline laufenden Stelle.**
   Die `tool_request`-Middleware läuft inline im MainThread — dort ist der
   Agent im Stack erreichbar. Sie lernt die Zuordnung über
   `_remember_agent_from_stack(session_id)` und ordnet sie der Session zu.
   Danach findet der Worker-Hook den Agenten über die Session.

Kein globales `current_agent`: zwei parallele Sessions teilen sich nie einen
Agenten. Fehlt eine eindeutige Zuordnung, bleibt der Router **fail-open**
(volle Werkzeugfläche) — der Vertrag des Routers verlangt genau das.

### Nebenwirkung mitbehoben

Die Warnung „kein Agent gefunden" erschien **in jedem Turn** und flutete das
Log. Sie wird jetzt höchstens **einmal je Prozess** ausgegeben
(`_note_missing_agent_once()`).

## Nachweis

Vier Nachweise, jeder für sich:

1. **Unit-Tests** — 19 Tests für Session-Auflösung, Worker-Thread,
   Parallelität, Session-Isolation und Registry-Semantik.
2. **Kippprobe (Mutation)** — drei Mutationen, jede bricht die Suite sichtbar:

   | Mutation | Erwartung | Ergebnis |
   |---|---|---|
   | M1: Auflösung wieder Stack-first | rot | 1 Test rot |
   | M2: Lernpunkt aus der Middleware entfernen | rot | 1 Test rot |
   | M3: Einmal-Warnung zurück auf je-Turn | rot | 1 Test rot |

   *Die erste Kippprobe zeigte eine Lücke in den Tests selbst:* M1 und M2
   blieben grün, weil die Tests die Wirkung prüften, nicht den Mechanismus.
   Zwei Vertragstests wurden ergänzt, bis alle drei Mutationen greifen.

3. **E2E gegen den echten Hermes-Dispatcher** (`tests/test_e2e_real_dispatcher.py`)
   — kein Mock des Dispatchers. Beweisform ist der A/B-Vergleich:

   | Fall | Bedingung | Ergebnis |
   |---|---|---|
   | A | Session-Map leer | 6 Tools → 6 Tools, kein Routing (fail-open) |
   | B | Session-Map gefüllt | 6 Tools → 4 Tools, Floor eingehalten |

   Im Fall B fallen `browser_exec`, `delegate_task` und `cronjob_manage`
   weg; `terminal`, `file`, `skills`, `memory`, `web` bleiben (Floor).

4. **Kippprobe des E2E** — Session-Auflösung entfernen bzw. Lernpunkt
   entfernen: beide brechen den E2E-Test sichtbar.

## Ausführen

```bash
# E2E-Umgebung bauen (isoliertes HERMES_HOME mit dem Repo-Stand)
tests/e2e_setup_env.sh /tmp/toolshed-e2e-home

TOOLSHED_E2E_HERMES_DIR=~/.hermes/hermes-agent \
TOOLSHED_E2E_HOME=/tmp/toolshed-e2e-home \
python -m pytest tests/test_e2e_real_dispatcher.py -v
```

Ohne Hermes-Installation wird der E2E-Test **übersprungen**, nicht
fehlgeschlagen — die Suite bleibt auf Fremdsystemen lauffähig.

## Betroffene Dateien

- `runtime/state.py` — `_remember_agent_from_stack()`, `_note_missing_agent_once()`,
  `_reset_missing_agent_notice()`; Doku am Stack-Walk.
- `runtime/__init__.py` — Session-Auflösung in `_route_tool_surface()`,
  Lernpunkt in `tool_request_middleware()`.
- `src/toolshed/` — Spiegelung derselben Änderungen (eingebautes Paket).
- `tests/test_agent_session_resolution.py`, `tests/test_agent_discovery.py`,
  `tests/test_session_registry.py` — neu.
- `tests/test_e2e_real_dispatcher.py`, `tests/e2e_setup_env.sh` — neu.

## Noch nicht geschehen

Der Fix ist **in den Repo-Stand committet**, aber **nicht** in eine laufende
Installation eingespielt. Ein Wechsel der Dateien im installierten
Plugin-Verzeichnis wirkt erst nach einem Neustart des Gateways, weil Hermes
den Plugin-Code einmal beim Start lädt (kein Hot-Reload).
