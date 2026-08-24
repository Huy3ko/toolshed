"""Shadow events — one record per evaluated turn.

A shadow event captures: what the predictor would have loaded (predicted),
what the router/model actually used (actual), and the derived numbers.
It is the unit of evaluation for shadow learning.

gateway_restart is tracked SEPARATELY from regular cache hits — without it you
cannot tell whether persistence really works or only within a running process
(GPT review + Hugo, 2026-08-21).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class ShadowEvent:
    session_id: str
    signature: str
    predicted: list[str] = field(default_factory=list)
    actual: list[str] = field(default_factory=list)
    precision: float = 0.0
    recall: float = 0.0
    warm_waste: int = 0
    cold_start: bool = False       # True if the session started cold (no profile hit)
    gateway_restart: bool = False  # True if this turn followed a process restart
    profile_loaded: bool = False   # True if the store already had profiles at
                                   # process start (persistence across restart)
    store_hash: str = ""           # sha256 of profiles.json at event time —
                                   # identical hash before/after restart = proof
    prediction_hit: bool = False   # recall == 1.0 (explicit, for the restart test)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ShadowEvent":
        return cls(
            session_id=str(d.get("session_id", "")),
            signature=str(d.get("signature", "")),
            predicted=list(d.get("predicted", [])),
            actual=list(d.get("actual", [])),
            precision=float(d.get("precision", 0.0)),
            recall=float(d.get("recall", 0.0)),
            warm_waste=int(d.get("warm_waste", 0)),
            cold_start=bool(d.get("cold_start", False)),
            gateway_restart=bool(d.get("gateway_restart", False)),
            profile_loaded=bool(d.get("profile_loaded", False)),
            store_hash=str(d.get("store_hash", "")),
            prediction_hit=bool(d.get("prediction_hit", False)),
            ts=float(d.get("ts", time.time())),
        )


class ShadowEventLog:
    """Append-only JSONL event log (one event per line — safe for concurrent
    appends from the gateway process)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event: ShadowEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

    def read(self) -> list[ShadowEvent]:
        if not self.path.exists():
            return []
        events: list[ShadowEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(ShadowEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return events
