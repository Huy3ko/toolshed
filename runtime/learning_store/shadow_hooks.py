"""Shadow hooks — bridge between the router plugin and the learning layer.

This is the ONLY module that touches both worlds. It is designed so the
router's V1 behavior is completely untouched:

  - predict(signature) is called from the plugin's pre_llm_call hook,
    but its result is NEVER applied to the tool surface — it is logged.
  - on_tool_used(toolset) is called from post_tool_call with the toolsets
    the model actually exercised.
  - finalize(session_id) is called from on_session_end and writes a
    ShadowEvent with precision/recall/warm_waste.

If shadow mode is disabled (default), all calls are no-ops. Fail-open holds:
any exception inside shadow code is swallowed and logged, never propagated.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any

from .observations import Observation
from .predictor import ShadowPredictor
from .profile_store import ProfileStore
from .signature import canonical_signature
from telemetry_store.events import ShadowEvent, ShadowEventLog

log = logging.getLogger("hermes_plugins.toolshed.shadow")

LOCK = threading.Lock()


class ShadowHooks:
    def __init__(
        self,
        *,
        enabled: bool = False,
        store_path: str = "",
        events_path: str = "",
        floor_toolsets: list[str] | None = None,
        max_warm: int = 8,
        channel: str = "unknown",
    ):
        self.enabled = enabled
        self.store = ProfileStore(store_path) if store_path else ProfileStore("/tmp/unused-profiles.json")
        self.events = ShadowEventLog(events_path) if events_path else ShadowEventLog("/tmp/unused-events.jsonl")
        self.predictor = ShadowPredictor(
            self.store,
            floor_toolsets=floor_toolsets or [],
            max_warm=max_warm,
        )
        self.channel = channel
        self._sessions: dict[str, dict[str, Any]] = {}
        # process-start detection: if the store already has profiles when this
        # process starts, persistence across a process boundary is proven.
        self._store_had_profiles_at_start = len(self.store.all_signatures()) > 0

    def _store_fingerprint(self) -> str:
        """sha256 of the profile file — identical before/after restart = proof."""
        try:
            if self.store.path.exists():
                return hashlib.sha256(
                    self.store.path.read_bytes()
                ).hexdigest()[:12]
        except OSError:  # pragma: no cover
            pass
        return ""

    # --- session lifecycle ---------------------------------------------------

    def on_turn(self, session_id: str, *, intent: str,
                continuity_refs: list[str] | None = None,
                recent_toolsets: list[str] | None = None) -> dict[str, Any]:
        """Called at pre_llm_call. Returns the prediction (for logging only —
        NEVER feed it into the tool surface)."""
        if not self.enabled:
            return {"shadow": False}
        try:
            obs = Observation(
                channel=self.channel,
                intent=intent,
                continuity_refs=continuity_refs or [],
                recent_toolsets=recent_toolsets or [],
            ).normalized()
            sig = canonical_signature(obs)
            with LOCK:
                slot = self._sessions.setdefault(session_id, {"signature": sig, "used": set(), "predicted": []})
                pred = self.predictor.predict(sig)
                slot["predicted"] = pred["predicted"]
                slot["profile_hits"] = pred["profile_hits"]
                # record the initial state (what was present at block start)
                self.store.record(obs, initially_present=pred["predicted"], signature=sig)
                self.store.save()
            return {"shadow": True, "signature": sig, "predicted": pred["predicted"],
                    "profile_hits": pred["profile_hits"]}
        except Exception:  # pragma: no cover - shadow must never break routing
            log.exception("shadow on_turn failed")
            return {"shadow": False}

    def on_tool_used(self, session_id: str, toolset: str) -> None:
        """Called at post_tool_call with the toolset actually used.

        Books the usage into the profile store immediately — this is the
        learning path: usage -> store -> score -> future predictions.
        Uses the signature computed once in on_turn so all bookings of one
        block land in the SAME profile.
        """
        if not self.enabled:
            return
        try:
            with LOCK:
                slot = self._sessions.get(session_id)
                if not slot:
                    return
                slot["used"].add(toolset)
                obs = Observation(
                    channel=self.channel,
                    intent="unknown",
                    continuity_refs=[],
                    recent_toolsets=[],
                ).normalized()
                self.store.record(obs, actually_used=[toolset], signature=slot["signature"])
                self.store.save()
        except Exception:  # pragma: no cover
            log.exception("shadow on_tool_used failed")

    def finalize(self, session_id: str) -> None:
        """Called at on_session_end. Writes the ShadowEvent."""
        if not self.enabled:
            return
        try:
            with LOCK:
                slot = self._sessions.pop(session_id, None)
            if not slot:
                return
            ev = ShadowPredictor.evaluate(
                predicted=slot["predicted"],
                actual=sorted(slot["used"]),
            )
            self.events.append(ShadowEvent(
                session_id=session_id,
                signature=slot["signature"],
                predicted=slot["predicted"],
                actual=sorted(slot["used"]),
                precision=ev["precision"],
                recall=ev["recall"],
                warm_waste=int(ev["warm_waste"]),
                cold_start=slot.get("profile_hits", 0) == 0,
                gateway_restart=self._store_had_profiles_at_start,
                profile_loaded=self._store_had_profiles_at_start,
                store_hash=self._store_fingerprint(),
                prediction_hit=ev["recall"] >= 1.0,
            ))
        except Exception:  # pragma: no cover
            log.exception("shadow finalize failed")


# Singleton — the plugin keeps one instance per process.
_shadow: ShadowHooks | None = None
_configured: bool = False


def _plugin_config_path() -> str:
    """Plugin config.yaml lives next to the learning/ package."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config.yaml"
        if candidate.exists():
            return str(candidate)
    return ""


def get_shadow() -> ShadowHooks:
    """Lazy singleton; self-configures from the plugin config.yaml once.

    shadow: {enabled, store_path, events_path, floor_toolsets, max_warm,
             channel} — absent/disabled -> no-op hooks.
    """
    global _shadow, _configured
    if _shadow is None:
        cfg_path = _plugin_config_path()
        kwargs: dict[str, Any] = {}
        try:
            if cfg_path:
                import yaml  # PyYAML is a plugin dependency
                cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
                s = (cfg.get("shadow") or {})
                kwargs = {
                    "enabled": bool(s.get("enabled", False)),
                    "store_path": s.get("store_path", ""),
                    "events_path": s.get("events_path", ""),
                    "floor_toolsets": s.get("floor_toolsets") or [],
                    "max_warm": int(s.get("max_warm", 8)),
                    "channel": s.get("channel", "unknown"),
                }
        except Exception:  # pragma: no cover
            log.exception("shadow self-config failed")
        _shadow = ShadowHooks(**kwargs)
        _configured = True
    return _shadow


def configure_shadow(**kwargs: Any) -> ShadowHooks:
    global _shadow
    _shadow = ShadowHooks(**kwargs)
    return _shadow
