"""Shadow hooks — bridge between the router plugin and the learning layer.

This is the ONLY module that touches both worlds. It is designed so the
router's V1 behavior is completely untouched:

  - predict(signature) is called from the plugin's pre_llm_call hook,
    but its result is NEVER applied to the tool surface — it is logged.
  - on_tool_used(toolset) is called from post_tool_call with the toolsets
    the model actually exercised.
  - finalize(session_id, block_id) is called after a run block and writes a
    ShadowEvent; without a block id it flushes the persistent session.

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
try:
    from ..telemetry_store.events import ShadowEvent, ShadowEventLog
except ImportError:  # flat Hermes loader
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
        paths_confined = bool(store_path and events_path)
        self.enabled = bool(enabled and paths_confined)
        self.store = ProfileStore(store_path) if paths_confined else None
        self.events = ShadowEventLog(events_path) if paths_confined else None
        self.predictor = (
            ShadowPredictor(
                self.store,
                floor_toolsets=floor_toolsets or [],
                max_warm=max_warm,
            )
            if self.store is not None
            else None
        )
        self.channel = channel
        self._sessions: dict[tuple[str, str], dict[str, Any]] = {}
        # process-start detection: if the store already has profiles when this
        # process starts, persistence across a process boundary is proven.
        self._store_had_profiles_at_start = bool(
            self.store is not None and len(self.store.all_signatures()) > 0
        )

    def _store_fingerprint(self) -> str:
        """sha256 of the profile file — identical before/after restart = proof."""
        try:
            if self.store is not None and self.store.path.exists():
                return hashlib.sha256(
                    self.store.path.read_bytes()
                ).hexdigest()[:12]
        except OSError:  # pragma: no cover
            pass
        return ""

    @staticmethod
    def _slot_key(session_id: str, block_id: str | None) -> tuple[str, str]:
        return (session_id, str(block_id or "session"))

    def _find_slot_key(
        self, session_id: str, block_id: str | None = None
    ) -> tuple[str, str] | None:
        if block_id is not None:
            key = self._slot_key(session_id, block_id)
            return key if key in self._sessions else None
        matches = [key for key in self._sessions if key[0] == session_id]
        return matches[-1] if matches else None

    # --- session lifecycle ---------------------------------------------------

    def on_turn(self, session_id: str, *, intent: str,
                continuity_refs: list[str] | None = None,
                recent_toolsets: list[str] | None = None,
                block_id: str | None = None) -> dict[str, Any]:
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
                key = self._slot_key(session_id, block_id)
                slot = self._sessions.setdefault(
                    key, {"signature": sig, "used": set(), "predicted": [],
                          "block_id": key[1]}
                )
                if self.predictor is None:
                    return {"shadow": False}
                pred = self.predictor.predict(sig)
                slot["predicted"] = pred["predicted"]
                slot["profile_hits"] = pred["profile_hits"]
            return {"shadow": True, "signature": sig, "predicted": pred["predicted"],
                    "profile_hits": pred["profile_hits"]}
        except Exception:  # pragma: no cover - shadow must never break routing
            log.exception("shadow on_turn failed")
            return {"shadow": False}

    def on_tool_used(
        self,
        session_id: str,
        toolset: str,
        *,
        block_id: str | None = None,
        status: str | None = None,
    ) -> None:
        """Book a successfully completed toolset for one session block."""
        if not self.enabled:
            return
        if status is not None and str(status).strip().lower() not in {
            "ok", "success", "completed"
        }:
            return
        try:
            with LOCK:
                key = self._find_slot_key(session_id, block_id)
                if key is None:
                    return
                slot = self._sessions[key]
                if toolset in slot["used"]:
                    return
                slot["used"].add(toolset)
        except Exception:  # pragma: no cover
            log.exception("shadow on_tool_used failed")

    def finalize(self, session_id: str, *, block_id: str | None = None) -> None:
        """Write one or all pending ShadowEvents for a session."""
        if not self.enabled or self.store is None or self.events is None:
            return
        try:
            with LOCK:
                if block_id is None:
                    keys = [key for key in self._sessions if key[0] == session_id]
                else:
                    key = self._slot_key(session_id, block_id)
                    keys = [key] if key in self._sessions else []
                slots = [(key, self._sessions.pop(key)) for key in keys]
            for key, slot in slots:
                ev = ShadowPredictor.evaluate(
                    predicted=slot["predicted"],
                    actual=sorted(slot["used"]),
                )
                obs = Observation(
                    channel=self.channel,
                    intent="unknown",
                    continuity_refs=[],
                    recent_toolsets=slot["predicted"],
                ).normalized()
                self.store.record(
                    obs,
                    initially_present=slot["predicted"],
                    actually_used=sorted(slot["used"]),
                    signature=slot["signature"],
                )
                self.store.save()
                self.events.append(ShadowEvent(
                    session_id=session_id,
                    block_id=key[1],
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


# One instance per Hermes home/profile context, not one process-global learner.
_shadow: ShadowHooks | None = None
_shadows: dict[str, ShadowHooks] = {}
_configured: bool = False


def _plugin_config_path() -> str:
    """Return only the config next to this runtime payload."""
    here = Path(__file__).resolve()
    candidate = here.parent.parent / "config.yaml"
    return str(candidate) if candidate.exists() else ""


def _shadow_scope_key() -> str:
    """Return the active Hermes home, including context-local profile overrides."""
    try:
        from hermes_constants import get_hermes_home, hermes_home_key
        return hermes_home_key(get_hermes_home())
    except Exception:
        return str(Path.home() / ".hermes")


def _shadow_state_root() -> Path:
    return Path(_shadow_scope_key())


def _resolve_shadow_path(
    config_path: str,
    configured_path: str,
    scope_root: str | Path | None = None,
) -> str:
    """Resolve state below the active Hermes home or plugin directory only."""
    if not isinstance(config_path, str) or not config_path:
        return ""
    if not isinstance(configured_path, str) or not configured_path.strip():
        return ""
    try:
        plugin_dir = Path(config_path).resolve().parent
        root = Path(scope_root).expanduser().resolve() if scope_root else plugin_dir
        candidate = Path(configured_path.strip()).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        resolved.relative_to(root)
        return str(resolved)
    except (OSError, ValueError):
        return ""


def get_shadow() -> ShadowHooks:
    """Lazy singleton; self-configures from the plugin config.yaml once.

    shadow: {enabled, store_path, events_path, floor_toolsets, max_warm,
             channel} — absent/disabled -> no-op hooks.
    """
    global _shadow, _configured
    scope_key = _shadow_scope_key()
    if scope_key not in _shadows:
        cfg_path = _plugin_config_path()
        kwargs: dict[str, Any] = {}
        try:
            if cfg_path:
                import yaml  # PyYAML is a plugin dependency
                cfg = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
                s = (cfg.get("shadow") or {})
                scope_root = _shadow_state_root()
                kwargs = {
                    "enabled": bool(s.get("enabled", False)),
                    "store_path": _resolve_shadow_path(
                        cfg_path, s.get("store_path", ""), scope_root
                    ),
                    "events_path": _resolve_shadow_path(
                        cfg_path, s.get("events_path", ""), scope_root
                    ),
                    "floor_toolsets": s.get("floor_toolsets") or [],
                    "max_warm": int(s.get("max_warm", 8)),
                    "channel": s.get("channel", "unknown"),
                }
        except Exception:  # pragma: no cover
            log.exception("shadow self-config failed")
        _shadows[scope_key] = ShadowHooks(**kwargs)
    _shadow = _shadows[scope_key]
    _configured = True
    return _shadow


def configure_shadow(**kwargs: Any) -> ShadowHooks:
    global _shadow
    scope_key = _shadow_scope_key()
    _shadow = ShadowHooks(**kwargs)
    _shadows[scope_key] = _shadow
    return _shadow
