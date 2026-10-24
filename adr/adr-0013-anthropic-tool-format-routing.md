{
  "title": "First-Turn-Routing im Anthropic-Tool-Format: Router liest nur OpenAI-Format",
  "status": "proposed",
  "date": "2026-10-24",
  "context": "GEMESSENER BEFUND (24.10.2026, Instanz `christiane`): Der Token-Router verengte auf dieser Instanz nie. Im Journal stand seit dem Gateway-Neustart am 23.10. 22:40:56 genau eine Router-Zeile: `llm_request kept full surface (reason=no_tools_in_request)`. Kein `first-turn route applied`.\n\nDie Instanz laeuft mit `api_mode: anthropic_messages` (Wire-Dump: `tools: 47 Eintraege, erste Schluessel: ['name', 'description', 'input_schema']`, `any function-wrapper?: False`). Vela laeuft mit `api_mode: chat_completions` (OpenAI-Format) und verengt seit Wochen nachweislich (`first-turn route applied` 30x platform=a2a, 21x platform=telegram).\n\nURSACHE: `_request_tool_names()` in `llm_request_routing.py` las die Werkzeugnamen AUSSCHLIESSLICH im OpenAI-Format (`entry.get('function', {}).get('name')`). Das Anthropic-Format fuehrt den Namen DIREKT im Eintrag. Die Anthropic-Konvertierung (`agent/transports/anthropic.py` -> `convert_tools_to_anthropic`) laeuft VOR `apply_llm_request_middleware()` in `agent/turn_api_request.py:143` - die Middleware sieht also je nach Provider ein anderes Format. Auf der Anthropic-Instanz fand der Router keine Namen und bailed mit `no_tools_in_request`.\n\nWARUM DAS SO TEUER WAR: Der Router protokolliert fail-open auf INFO, nicht auf WARNING. Ein Verzicht sah deshalb wie 'nichts passiert' aus. Die Ursache lag nicht in Installation, Konfiguration, Version oder Neustart - alle diese waren geprueft und in Ordnung. Der Unterschied war das Wire-Format des Providers.\n\nZWEITER BEFUND: `_bail_reasons_seen` (llm_request_routing.py:271) ist ein modulglobales Set OHNE Reset. Der Codekommentar darueber sagt 'hoechstens EINMAL je Turn/Session' - das ist falsch, es ist einmal je PROZESS. Eine Zaehlung ueber ein Logfenster verwechselt daher Prozessgenerationen mit Fehlschlaegen.",
  "problem": "Der Router ist auf jedem Provider-Einsatz wirkungslos, dessen Tool-Format nicht dem OpenAI-Schema entspricht. Anthropic ist kein Sonderfall, sondern die zweite verbreitete Form.",
  "options_considered": [
    {
      "name": "A: Router auf das OpenAI-Format festnageln und Anthropic-Instanzen ausdruecklich als nicht unterstuetzt dokumentieren",
      "rejected_because": "Verengung waere auf einer ganzen Provider-Familie still aus. Der Router sieht nicht, ob er arbeiten kann - er bailt und sieht dabei aus wie ein normaler Lauf."
    },
    {
      "name": "B: Format-Erkennung ueber den `api_mode`-Kontextparameter steuern",
      "pros": "Explizit, gut lesbar.",
      "cons": "Koppelt den Router an eine Liste von Provider-Namen. Jeder neue Provider braucht Code. Der Formatunterschied liegt aber nicht im Provider, sondern in der ZWEI Formen, die Hermes erzeugt."
    },
    {
      "name": "C (CHOSEN): Format-tolerant lesen - beide Formen werden erkannt, ohne Provider-Wissen",
      "pros": "Deckt beide real vorkommenden Formen ab und faellt auf keinem neuen Provider still aus. Kein Konfigurationsschalter, kein Neustart fuer die Aktivierung. Der Rueckgabewert bleibt der Name des Eintrags - der Unterschied liegt nur in der Tiefe, nicht in der Bedeutung.",
      "cons": "Der Router traegt implizit Wissen ueber zwei Formate. Das ist im Code dokumentiert und durch einen Test festgenagelt."
    }
  ],
  "decision": "Option C. `_request_tool_names()` liest ueber einen neuen Helfer `_tool_entry_name()`, der den OpenAI-Weg (`entry['function']['name']`) und den Anthropic-Weg (`entry['name']`) kennt. Der Filter, der die Werkzeuge zurueckschreibt, benutzt denselben Helfer - UND gibt die Eintraege unveraendert zurueck, damit kein Format in einen Request eines anderen Providers geschrieben wird.",
  "frozen_invariants": [
    "Die zurueckgegebenen Tool-Eintraege werden UNVERAENDERT zurueckgeschrieben - der Router schreibt kein Provider-Format in einen Request eines anderen Providers.",
    "Ein Request im OpenAI-Format verengt sich genau wie vor dem Fix (Regressionstest).",
    "Ohne Host-Autorisierung bleibt die volle Flaeche (fail-open, unveraendert).",
    "Die drei Payload-Ebenen (Root, src/toolshed/, runtime/) bleiben bytegleich - gewahrt durch test_layer_mirror.py.",
    "Der Hermes-Core bleibt unveraendert; der Fix liegt ausschliesslich im Plugin."
  ],
  "verification": {
    "baseline_red": "Die drei neuen Anthropic-Tests schlagen an der Baseline 6070619 fehl (3 failed, 1 passed) und sind mit dem Fix gruen (4 passed).",
    "mutation_M1": "Anthropic-Fallback in `_tool_entry_name` entfernt -> genau die 3 Anthropic-Tests werden rot.",
    "mutation_M2": "Filter schreibt OpenAI-Struktur zurueck -> genau test_anthropic_format_survives_the_filter wird rot.",
    "suite": "117 passed, 6 skipped (vor dem Fix: 114 passed, 6 skipped).",
    "lint": "ruff: All checks passed."
  },
  "deployment": "NICHT ausgerollt. Der Fix liegt im Repo-Branch `fix/anthropic-tool-format`. Die betroffene Instanz `christiane` nutzt eine eigene Unix-Identitaet mit eigenem Home (0700) - ein Zugriff darauf ist ein fremder Bereich und braucht eine eigene Freigabe. Nach dem Merge muss ihr Plugin-Baum auf den neuen Stand gebracht und ihr Gateway neu gestartet werden.",
  "rollout_falle_origin_main": "KRITISCH, unabhaengig vom Anthropic-Fix: `origin/main` des Repos `Huy3ko/toolshed` steht auf Commit 1bbd658 (02.09.2026). Dieser Stand enthaelt die Datei `llm_request_routing.py` NICHT - die First-Turn-Routing-Middleware kam erst am 23.10.2026 mit 4fbe677 in den Baum, gefolgt von cfae559/0616b2a (session-gebundene Agenten-Aufloesung), 21d4509 (flaches Root-Payload) und 35a92c9 (Anthropic-Format). 12 Commits liegen zwischen origin/main und HEAD; v0.2.0 ist laut eigener Release-Notiz 'unreleased'. `install.sh` installiert per `hermes plugins install <repo>` OHne Ref und zieht damit den Stand von origin/main. Ein Rollout ueber den Default-Branch wuerde die Instanz auf einen Tree ZURUECKZIEHEN, der die Middleware gar nicht enthaelt - der Router verengt dann gar nicht mehr, statt nur falsch. Der Rollout MUSS daher explizit per `--ref` auf einen Commit gesetzt werden, der die Middleware enthaelt (z.B. 35a92c9 oder ein spaeterer Merge-Commit auf main). Zusaetzlich waere ein Rollout auf origin/main durch die install.sh-Verifikation NICHT erkennbar: sie prueft nur `enabled: true` und `tools.override: granted`, beides waere auch bei einem middleware-losen Tree erfuellt.",
  "entdeckungsluecke": "WARUM DAS DREI WOCHEN UNENTDECKT BLIEB: `install.sh` verifiziert als 'Verifikation' nur (a) `enabled: true` in der Plugin-config.yaml und (b) `tools.override: granted`. Beides war auf der betroffenen Instanz ERFUELLT - der Installer meldete 'installed & active'. Der Kopfkommentar des Installers ('verifies routing is REALLY active - never reports success on a half-install') beschreibt damit etwas, was der Installer NICHT tut: geprueft wird die Verdrahtung, nicht die Wirkung. Ein echter Routing-Nachweis (eine 'first-turn route applied'-Zeile im Log nach einem echten Turn) fehlt in der Verifikationslogik. Zusaetzlich protokolliert der Router fail-open auf INFO, sodass der Verzicht selbst nicht als Fehler auffiel. Beides zusammen laesst einen wirkungslosen Router wie einen gesunden aussehen. Folge: der Installer sollte nach der Installation einen echten Turn anstossen und im Log eine 'first-turn route applied'-Zeile verlangen; fehlt sie, ist der Install zu melden als unvollstaendig. Dies ist eine eigene, noch nicht umgesetzte Verbesserung - sie ist NICHT Teil dieses Fixes.",
  "warum_die_asymmetrie_zwischen_den_instanzen": "Beide Instanzen nutzen Minimax, aber ueber verschiedene Provider-Pfade - daher unterschiedliche Wire-Formate. Vela: `provider: custom:minimax` mit explizitem `api_mode: chat_completions` in der model-Sektion der config.yaml (OpenAI-Format, Router verengt nachweislich: 30x platform=a2a, 21x platform=telegram). Christiane: der eingebaute Provider `minimax` (plugins/model-providers/minimax/__init__.py:41, `api_mode=\"anthropic_messages\"`) - Anthropic-Format, Router bailed. Die Asymmetrie liegt also NICHT in der Modellwahl und NICHT in der Hermes-Version, sondern in der api_mode-Angabe. Von Christiane unabhaengig gegengeprueft (sie las die Provider-Definition und den Plugin-Baum selbst vor).",
  "rollback": "git revert des Fix-Commits auf diesem Branch, oder checkout des Referenz-Commits 6070619 fuer die drei Payload-Ebenen. Der Fix ist additiv (eine neue Helferfunktion, eine Aenderung in zwei Aufrufstellen) und beruehrt keine bestehende Signatur.",
  "upstream_reference": "Hermes v0.22.2: agent/turn_api_request.py:143 (apply_llm_request_middleware), agent/transports/anthropic.py:50 (convert_tools), agent/anthropic_message_convert.py (convert_tools_to_anthropic).",
  "related": [
    "ADR-0011 (runtime payload distribution) - begruendet, warum drei Ebenen existieren und gemeinsam gepflegt werden muessen."
  ]
}
