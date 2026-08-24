"""Profile store — deliberately dumb persistence layer for capability profiles.

Design decision (design review): the store does NOT decide anything. It
only persists, per context signature, which toolsets were:
  - initially_present (floor + warm set at block start)
  - actually_used
  - discovered (loaded on demand)
  - successful

Keeping these four counters separate is essential: without it you cannot tell
whether a toolset is frequent only because the floor forced it into every
working set (floor bias). Scoring/decay/prediction live in predictor.py and
scoring.py — never here.

Format (JSON file):

    {
      "infrastructure|matrix-caddy|terminal-file": {
        "seen": 8,
        "successful_runs": 8,
        "toolsets": {
          "terminal": {"initially_present": 8, "actually_used": 7,
                       "discovered": 0, "successful": 7, "last_used": 1787300000},
          "web":      {"initially_present": 2, "actually_used": 4,
                       "discovered": 2, "successful": 4, "last_used": 1787299000}
        }
      }
    }

Note: a toolset can be discovered (not present) yet still used+successful —
that is exactly the signal that the warm set is missing it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .observations import Observation
from .signature import canonical_signature


class ProfileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    # --- persistence ------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(self.path)

    # --- recording ---------------------------------------------------------

    def record(
        self,
        obs: Observation,
        *,
        initially_present: list[str] | None = None,
        actually_used: list[str] | None = None,
        discovered: list[str] | None = None,
        successful: list[str] | None = None,
        signature: str | None = None,
    ) -> str:
        """Record one work block under its signature. Returns the signature.

        Defaults fall back to the observation's own fields, so a plain
        `record(obs)` works when the caller has no separate outcome data.
        Pass `signature` explicitly when the caller already computed it once
        (shadow hooks) — this guarantees all bookings of one block land in
        the SAME profile.
        """
        sig = signature or canonical_signature(obs)
        block = self._data.setdefault(sig, {"seen": 0, "successful_runs": 0, "toolsets": {}})
        block["seen"] += 1
        if successful or obs.successful_toolsets:
            block["successful_runs"] += 1

        toolsets = block["toolsets"]
        now = int(time.time())
        for ts in initially_present or obs.recent_toolsets:
            self._bump(toolsets, ts, "initially_present", now)
        for ts in actually_used or obs.successful_toolsets:
            self._bump(toolsets, ts, "actually_used", now)
        for ts in discovered or obs.discovered_toolsets:
            self._bump(toolsets, ts, "discovered", now)
        for ts in successful or obs.successful_toolsets:
            self._bump(toolsets, ts, "successful", now)
        return sig

    @staticmethod
    def _bump(toolsets: dict[str, Any], toolset: str, counter: str, now: int) -> None:
        if not toolset:
            return
        entry = toolsets.setdefault(toolset, {})
        entry[counter] = entry.get(counter, 0) + 1
        entry["last_used"] = now

    # --- read ---------------------------------------------------------------

    def get(self, signature: str) -> dict[str, Any] | None:
        return self._data.get(signature)

    def all_signatures(self) -> list[str]:
        return sorted(self._data.keys())

    def stats(self) -> dict[str, Any]:
        """Aggregate overview — for telemetry, not for routing decisions."""
        total_seen = sum(p["seen"] for p in self._data.values())
        return {
            "profiles": len(self._data),
            "blocks_recorded": total_seen,
            "signatures": self.all_signatures(),
        }
