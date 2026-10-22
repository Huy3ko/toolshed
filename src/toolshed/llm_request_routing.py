"""First-Turn-Routing ueber die llm_request-Middleware (Hermes v0.22.2).

GEMESSENER BEFUND (22.10.2026, laufender Hermes v0.22.2, Commit d923a5e):

  `pre_llm_call` ist ein BOUNDED Hook (hermes_cli/plugins_dispatch.py:42) und
  laeuft deshalb auf einem Daemon-Worker-Thread (Zeile 271/333). Der
  Aufrufer-Stack ist dort abgeschnitten: `build_turn_context` mit seinem
  `agent`-Local ist NICHT erreichbar. Ein Stack-Walk findet dort nie einen
  Agenten. Im Betrieb sichtbar als:
      "hermes-token-router: no agent reference; full set fallback"

  Middleware dagegen laeuft INLINE (kein Timeout-Mechanismus, kein Worker).
  `apply_llm_request_middleware` wird aus `agent/turn_api_request.py:145`
  aufgerufen, wo `agent` ein Parameter von `build_api_request()` ist und
  damit im Stack liegt. Und die Middleware darf den kompletten
  Provider-Request ersetzen - also auch dessen `tools`-Liste.

Damit ist First-Turn-Routing ueber die vorhandene Plugin-API moeglich, ohne
den Hermes-Core zu beruehren.

WARUM ZUSAETZLICH ZUM HOOK:
  `agent.tools` wird nur beim Agent-Aufbau gesetzt (agent/agent_init.py:1117).
  Der Gateway cacht den Agenten pro Session, aber der ERSTE Turn baut ihn neu
  - mit der vollen Werkzeugflaeche. Eine Mutation an `agent.tools` aus einem
  Hook erreicht den ersten Request also nicht. Die Middleware laeuft dagegen
  NACH dem Request-Aufbau (build_api_request baut api_kwargs mit `tools` aus
  agent.tools) und kann die `tools`-Liste des Requests direkt ersetzen.

VERTRAG:
  - fail-open: ohne Agent, ohne Autorisierung oder bei jedem Fehler bleibt
    die volle Werkzeugflaeche erhalten. Ein Routing-Fehler darf nie eine
    Faehigkeit wegnehmen.
  - Die Route gilt fuer diesen Request; `agent.tools` bleibt unveraendert,
    damit Hermes' eigene Werkzeug-Freigabe (`valid_tool_names`) intakt bleibt
    und ein fehlendes Werkzeug ueber den Recovery-Pfad erreichbar ist.
  - Nur der erste Request eines Turns wird geroutet (session-sticky).

Die Vorhersage selbst wird NICHT dupliziert, sondern aus `policy` importiert -
dieselbe Funktion, dieselben Parameter, dieselbe Confidence-Logik wie im
Hook-Pfad. Sonst wuerden zwei Pfade auseinanderlaufen.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("hermes_plugins.hermes_token_router")

PLUGIN_NAME = "hermes-token-router"


def _request_tool_names(request: Dict[str, Any]) -> Set[str]:
    """Die Werkzeugnamen, die der Request tatsaechlich mitbringt."""
    names: Set[str] = set()
    for entry in request.get("tools") or []:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _find_agent_in_stack() -> Any:
    """Finde den Agenten im Aufrufer-Stack.

    Nur wirksam, wenn die Middleware inline laeuft (der Fall im Host). Der
    `pre_llm_call`-Hook dagegen laeuft in einem Worker-Thread, in dem dieser
    Weg nicht funktioniert - dort greift die session-gebundene Aufloesung.
    """
    import inspect

    for frame in inspect.stack():
        for var in frame.frame.f_locals.values():
            if var is None or isinstance(var, (str, int, float, bool, list, dict, tuple, set)):
                continue
            if hasattr(var, "valid_tool_names") and hasattr(var, "tools"):
                return var
    return None


def _original_user_message_from_stack() -> str:
    """Die echte User-Nachricht aus dem Aufrufer-Stack holen.

    BEVORHER (Befund 23.10.2026): Die Middleware mass die LETZTE
    User-Nachricht aus dem Provider-Request. In Hermes traegt die aber den
    kompletten Kontext-Wrapper (Skills-Policy, Tool-Mapping, Zeitblock) -
    gemessen 8910 Zeichen bei einem 98-Zeichen-User-Text. Die Regel
    `long_message_decline_chars` (2000) bailte damit IMMER und der Router
    konnte in Hermes grundsaetzlich nie routen.

    `build_api_request` im Core bekommt `original_user_message` als
    Keyword-Parameter uebergeben - und genau diese Funktion liegt im
    Stack, wenn die Middleware laeuft (sie wird von dort aus gerufen).
    Deshalb: erst den Stack nach dieser Variablen durchsuchen, und nur
    als Rueckfall auf die Request-Nachricht zurueckfallen.
    """
    import inspect

    for frame_info in inspect.stack():
        f_locals = frame_info.frame.f_locals
        value = f_locals.get("original_user_message")
        if isinstance(value, str) and value.strip():
            return value
    return ""


# Wrapper-Praefixe: Framework-Text, den die Plattform VOR die echte
# User-Nachricht setzt. Er stammt nicht vom User und darf die
# Absichtserkennung nicht verfaelschen.
#
# BEVORHER (Befund 23.10.2026): Der A2A-Praefix enthielt "Reply as you
# would to a colleague's request" - das matchte die Regel
# `(email|mail|...|reply)` und rief damit `mcp-agentmail` auf. Der Router
# route also eine Absicht, die der User nie geaeussert hat.
#
# WICHTIG: Nur fuehrende Praefixe werden entfernt. Ein Marker, der mitten
# in der Nachricht steht (z. B. der "[Current time]"-Block am Ende),
# gehoert zum User-Inhalt oder zum Nachlauf und darf NICHT zum
# Abschneidemarker werden - sonst verliert man die echte Anfrage.
_USER_PREFIX_MARKERS = (
    "[A2A inbound",              # Hermes A2A-Adapter
    "[A2A outbound",             # ausgehende A2A-Nachrichten
    "[Current time]",            # Hermes Zeitblock
    "[System:",                  # Hermes System-Anhaenge
)


def _strip_platform_prefix(text: str) -> str:
    """Fuehrende Plattform-Rahmen abschneiden, der echte User-Text bleibt.

    Hermes haengt Kontextbloecke an die User-Nachricht (A2A-Praefix,
    Zeitblock, System-Anhaenge). Diese Bloecke sind Framework-Text: sie
    nennen Absichten, die der User nicht geaeussert hat, und machen die
    Nachricht lang genug, um die Laengenregel auszuloesen.

    Deshalb: nur fuehrende Praefixe entfernen - mehrfach, weil mehrere
    aufeinander folgen koennen. Ein Marker mitten in der Nachricht wird
    NICHT als Schnittkante benutzt: sonst verliert man den User-Text.
    Unbekannter Rahmen bleibt unveraendert; es wird nichts geraten.
    """
    if not text:
        return text
    current = text.strip()
    changed = True
    while changed:
        changed = False
        for marker in _USER_PREFIX_MARKERS:
            if not current.startswith(marker):
                continue
            rest = current[len(marker):]
            # Der Praefix-Block endet am ersten Leerblock.
            if "\n\n" in rest:
                rest = rest.split("\n\n", 1)[1]
            else:
                rest = rest.lstrip("\n")
            rest = rest.strip()
            if not rest:
                # Nichts Brauchbares uebrig: Original behalten.
                return text
            current = rest
            changed = True
            break
    return current


def _extract_user_message(request: Dict[str, Any]) -> str:
    """Die aktuelle User-Nachricht aus dem Request herausziehen."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        messages = request.get("input")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            if parts:
                return "\n".join(parts)
    return ""


def _predict(user_message: str, profile_cfg: Dict[str, Any], available: Set[str]) -> Optional[Set[str]]:
    """Toolsets vorhersagen - dieselbe Logik wie im Hook-Pfad.

    Gibt `None` zurueck, wenn keine belastbare Route vorliegt (dann fail-open).
    Ein leeres Set bedeutet "leer/periodisch" und ist eine bewusste Route.
    """
    try:
        from .config import _get_router_model, _get_router_provider, _is_classifier_enabled
        from .policy import _predict_toolsets_by_rules, _predict_toolsets_via_llm
    except ImportError:  # pragma: no cover - flacher Loader-Fallback
        from config import _get_router_model, _get_router_provider, _is_classifier_enabled
        from policy import _predict_toolsets_by_rules, _predict_toolsets_via_llm

    deterministic_enabled = bool(profile_cfg.get("deterministic_rules_enabled", True))

    # Leere und Punkt-Probes tragen keine Absicht - genau wie im Hook-Pfad.
    probe_chars = {char for char in user_message if not char.isspace()}
    if not probe_chars or probe_chars <= {".", "…"}:
        return set()

    if deterministic_enabled:
        predicted, _reason = _predict_toolsets_by_rules(user_message, available)
        if predicted is not None:
            return set(predicted)
        if not _is_classifier_enabled(profile_cfg):
            return None
        try:
            predicted = _predict_toolsets_via_llm(
                user_message,
                available,
                _get_router_model(profile_cfg),
                float(profile_cfg.get("confidence_threshold", 0.0)),
                _get_router_provider(profile_cfg),
                max(0.05, float(profile_cfg.get("router_hard_timeout_ms", 1200)) / 1000.0),
                profile_cfg.get("classifier_base_url", "") or "",
                profile_cfg.get("classifier_api_key_env", "") or "",
            )
        except Exception as exc:
            logger.warning("%s: llm prediction failed at llm_request: %s", PLUGIN_NAME, exc)
            return None
        if predicted is None:
            return None
        return set(predicted)

    if not _is_classifier_enabled(profile_cfg):
        return None
    try:
        predicted = _predict_toolsets_via_llm(
            user_message,
            available,
            _get_router_model(profile_cfg),
            float(profile_cfg.get("confidence_threshold", 0.0)),
            _get_router_provider(profile_cfg),
            max(0.05, float(profile_cfg.get("router_hard_timeout_ms", 1200)) / 1000.0),
            profile_cfg.get("classifier_base_url", "") or "",
            profile_cfg.get("classifier_api_key_env", "") or "",
        )
    except Exception as exc:
        logger.warning("%s: llm prediction failed at llm_request: %s", PLUGIN_NAME, exc)
        return None
    if predicted is None:
        return None
    return set(predicted)


# Abbruchgruende des Routings sichtbar machen.
#
# BISHER (Befund 23.10.2026): Der Router war im Betrieb vollstaendig stumm.
# Er loggte Erfolg auf INFO, aber alle fail-open-Abbruchwege auf DEBUG - und
# das Gateway loggt ab WARNING. Ein Verzicht sah deshalb aus wie "nichts
# passiert". Genau das hat die Fehlersuche so teuer gemacht: es gab kein
# Signal, woran sich der Betriebszustand ablesen liess.
#
# _bail protokolliert den konkreten Grund auf INFO, aber hoechstens EINMAL je
# Turn/Session, damit ein Turn mit mehreren Requests das Log nicht flutet.
_bail_reasons_seen: Set[str] = set()


def _bail(reason: str, exc: Optional[BaseException] = None) -> None:
    """Fail-open mit sichtbarem Grund. Gibt None zurueck (Request unveraendert)."""
    if reason not in _bail_reasons_seen:
        _bail_reasons_seen.add(reason)
        if exc is not None:
            logger.info("%s: llm_request kept full surface (reason=%s): %s",
                        PLUGIN_NAME, reason, exc)
        else:
            logger.info("%s: llm_request kept full surface (reason=%s)",
                        PLUGIN_NAME, reason)

def llm_request_middleware(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Ersetze die Werkzeugflaeche im Provider-Request (First-Turn-Routing).

    Rueckgabe-Form wie vom Host erwartet: ``{"request": {...}}`` ersetzt die
    Provider-Kwargs. ``None`` laesst den Request unveraendert.
    """
    reason = "no_request"
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return _bail(reason)

    reason = "no_agent_in_stack"
    agent = _find_agent_in_stack()
    if agent is None:
        return _bail(reason)

    reason = "no_tools_in_request"
    tools_before = _request_tool_names(request)
    if not tools_before:
        return _bail(reason)

    try:
        try:
            from .state import ROUTER_STATE_ATTR, RouterState, _get_router_state
        except ImportError:  # pragma: no cover - flacher Loader-Fallback
            from state import ROUTER_STATE_ATTR, RouterState, _get_router_state
    except Exception as exc:  # pragma: no cover - Import-Fallback
        return _bail("router_state_unavailable", exc)

    # Der State muss hier erzeugt werden koennen, nicht nur gelesen werden.
    #
    # GEMESSENER BEFUND (23.10.2026, laufender Hermes v0.22.2): Der Hook
    # `pre_llm_call` laeuft auf einem Daemon-Worker-Thread; dort ist der
    # Agent-Stack abgeschnitten und der Hook findet keinen Agenten. Der
    # Router-State am Agent wird deshalb vom Hook-Pfad gar nicht erst
    # angelegt. Die Middleware fand `state = None` und blieb bei
    # `authorized_toolsets = None` immer im fail-open - die Verengung fand
    # auch nach dem Import-Fix nicht statt.
    #
    # Die Middleware laeuft INLINE und hat den Agenten im Stack. Sie holt
    # sich den State deshalb selbst (session-gebunden am Agent) und erfasst
    # die Autorisierung des Hosts selbst.
    # War der State bereits am Agent (dann hat der Host-Pfad ihn angelegt),
    # oder erzeugt die Middleware ihn hier neu?
    # Der Wert wird bewusst nicht weiterverwendet - die Frage beantwortet
    # `_get_router_state()` gleich darunter. Die Zuweisung bleibt als
    # Diagnose-Point, daher mit Unterstrich (kein ungenutzter Name).
    try:
        _pre_existing_state = getattr(agent, ROUTER_STATE_ATTR, None)
    except Exception:  # pragma: no cover - defensive
        _pre_existing_state = None

    try:
        state = _get_router_state(agent)
    except Exception as exc:  # pragma: no cover - defensive
        return _bail("router_state_unavailable", exc)

    if not isinstance(state, RouterState):
        return _bail("no_router_state")
    if state.initial_route_applied:
        # Session-sticky: der erste Request dieses Turns hat entschieden.
        return _bail("already_routed")

    # Die Host-Freigabe selbst erfassen. `capture_authorization` liest
    # `agent.enabled_toolsets` - die Werkzeugflaeche, die Hermes dem Agenten
    # gegeben hat. Ohne diese Erfassung wuerde die Middleware eine Flaeche
    # waehlen, die der Host nicht freigegeben hat.
    #
    # WICHTIG: `capture_authorization` darf hier KEINE Freigabe erfinden.
    # Der Host-Pfad setzt `authorization_captured = True` und
    # `authorized_toolsets = None`, wenn er KEINE Freigabeflaeche anbietet -
    # das ist das Signal "nichts freigegeben". Erfasst die Middleware dann
    # selbst, wuerde sie `agent.enabled_toolsets` lesen und eine Freigabe
    # erfinden, die der Host nie erteilt hat. Das waere ein Rechteverlust.
    #
    # Deshalb: die Erfassung ist nur erlaubt, wenn der Host-Pfad den Agenten
    # NIE gesehen hat - dann gibt es keinen Host-Zustand, der eine
    # Freigabe verweigert. Ein bereits am Agent vorhandener State bedeutet:
    # der Host-Pfad war hier und hat die Autorisierung bewusst nicht
    # erfasst (bei `authorization_captured = True` und
    # `authorized_toolsets = None` heisst das: "Host gibt nichts frei").
    #
    # Wann darf die Middleware die Autorisierung selbst erfassen?
    #
    # Der Hook-Pfad `pre_llm_call` laeuft im Worker-Thread und findet dort
    # keinen Agenten. Er legt den State an, kann die Autorisierung aber
    # nicht erfassen (`authorization_captured` bleibt False). Das ist
    # "noch nicht entschieden", NICHT "verweigert" - die Middleware darf
    # hier die Host-Freigabe selbst lesen.
    #
    # Echte Verweigerung: `authorization_captured = True` und
    # `authorized_toolsets = None`. Das setzt `capture_authorization` dann,
    # wenn der Host KEIN `enabled_toolsets` anbietet. Genau das ist der
    # Fall, in dem die Middleware fail-open bleiben MUSS.
    #
    # Ohne diese Unterscheidung verliert der Router den ERSTEN Request
    # jedes Turns: der Hook war zu frueh, die Middleware bails, erst der
    # zweite Request routet.
    if state.authorization_captured and not state.authorized_toolsets:
        # Der Host hat die Autorisierung abgeschlossen und nichts freigegeben.
        return _bail("no_host_authorization")
    if not state.authorization_captured:
        # Noch nicht erfasst: die Middleware erfasst die Host-Freigabe jetzt.
        raw_grant = getattr(agent, "enabled_toolsets", None)
        if not isinstance(raw_grant, (list, tuple, set, frozenset)):
            return _bail("no_host_authorization")
        if not state.capture_authorization(agent):
            return _bail("no_host_authorization")

    try:
        try:
            from .config import _get_profile_config, _load_config
            from .policy import _get_available_toolsets
            from .router_tools import _resolve_toolset_to_tool_names
        except ImportError:  # pragma: no cover - flacher Loader-Fallback
            from config import _get_profile_config, _load_config
            from policy import _get_available_toolsets
            from router_tools import _resolve_toolset_to_tool_names
    except Exception as exc:
        return _bail("router_internals_unavailable", exc)

    profile_cfg = _get_profile_config(_load_config())
    if not profile_cfg.get("enabled", False):
        return _bail("router_disabled")
    mode = profile_cfg.get("mode")
    if mode in ("off", "invalid", "shadow"):
        # Shadow/aus liefert keine echte Verengung; die Host-Flaeche bleibt.
        return _bail(f"mode_{mode}")

    reason = "no_user_message"
    # Die echte User-Nachricht bevorzugen: der Request traegt den
    # Hermes-Kontext-Wrapper, nicht den User-Text.
    user_message = _original_user_message_from_stack() or _extract_user_message(request)
    user_message = _strip_platform_prefix(user_message)
    if not user_message:
        return _bail(reason)

    decline_chars = profile_cfg.get("long_message_decline_chars", 2000)
    if len(user_message) > decline_chars:
        return _bail("message_too_long")
    bypass_chars = profile_cfg.get("short_message_bypass_chars", 0)
    if bypass_chars and len(user_message.strip()) < bypass_chars:
        return _bail("message_too_short")

    try:
        available = _get_available_toolsets()
    except Exception as exc:
        return _bail("toolsets_unavailable", exc)
    if not available:
        return _bail("no_available_toolsets")

    try:
        predicted = _predict(user_message, profile_cfg, available)
    except Exception as exc:
        # fail-open: ein Vorhersagefehler darf die Werkzeugflaeche nie
        # verkleinern. Der Host darf durch den Router nicht brechen.
        return _bail("prediction_raised", exc)
    if predicted is None:
        # Keine belastbare Route -> fail-open.
        return _bail("no_route")

    floor_toolsets = set(profile_cfg.get("floor_toolsets", ["terminal", "file", "web"]))
    authorized = getattr(state, "authorized_toolsets", None)
    if not authorized:
        # Die Autorisierungsflaeche des Hosts ist noch nicht erfasst. Der
        # Hook-Pfad erfasst sie; hier fail-open bleiben, statt eine
        # Werkzeugflaeche zu waehlen, die der Host nicht freigegeben hat.
        return _bail("no_authorization_captured")

    allowed = (set(predicted) | floor_toolsets) & set(available) & set(authorized)
    if not allowed:
        return _bail("empty_allowed_set")

    try:
        allowed_names = _resolve_toolset_to_tool_names(allowed)
    except Exception as exc:
        return _bail("toolset_resolution_failed", exc)
    if not allowed_names:
        return _bail("no_tool_names")

    # Nur die Werkzeuge behalten, die der Host ohnehin mitbringt.
    kept = [t for t in request.get("tools") or []
            if isinstance(t, dict)
            and (t.get("function") or {}).get("name") in allowed_names]
    if not kept:
        return _bail("nothing_kept")

    kept_names = _request_tool_names({"tools": kept})
    if kept_names == tools_before:
        # Nichts gewonnen - der Request traegt bereits genau diese Flaeche.
        return _bail("no_savings")

    new_request = dict(request)
    new_request["tools"] = kept

    # Der Agent-Zustand wird NICHT verengt: `valid_tool_names` bleibt voll,
    # damit ein Aufruf eines gefilterten Werkzeugs ueber den Recovery-Pfad
    # aufloesbar bleibt. Nur die Sicht des Modells wird schmaler.
    try:
        if isinstance(state, RouterState):
            state.active = True
            state.predicted_toolsets = set(predicted)
            state.set_initial_surface(set(allowed))
    except Exception as exc:
        logger.debug("%s: could not record router state: %s", PLUGIN_NAME, exc)

    logger.info(
        "%s: first-turn route applied at llm_request: %d -> %d tools (session=%s) allowed=%s",
        PLUGIN_NAME, len(tools_before), len(kept_names), kwargs.get("session_id") or "?",
        ",".join(sorted(allowed)),
    )
    return {"request": new_request, "source": PLUGIN_NAME}
