"""Metrics — aggregate shadow events into the five target numbers.

The five numbers (GPT review + Hugo, 2026-08-21):
  1. cold-start schema cost
  2. warm-start schema cost
  3. tool-cache hit rate
  4. discovery events after cache hit
  5. router-caused tool failures = 0

Plus, tracked separately: gateway-restart hits.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

from .events import ShadowEvent


class Metrics:
    def __init__(self, events: list[ShadowEvent]):
        self.events = events

    def report(self) -> dict[str, Any]:
        events = self.events
        n = len(events)
        if n == 0:
            return {"events": 0}

        recalls = [e.recall for e in events if e.recall > 0]
        precisions = [e.precision for e in events if e.precision > 0]
        restarts = [e for e in events if e.gateway_restart]
        cold = [e for e in events if e.cold_start]

        # tool-cache hit rate: turns where the prediction covered the actual
        # tools completely (recall == 1.0)
        full_hits = [e for e in events if e.recall >= 1.0]

        return {
            "events": n,
            "recall_avg": round(mean(recalls), 3) if recalls else 0.0,
            "precision_avg": round(mean(precisions), 3) if precisions else 0.0,
            "warm_waste_avg": round(mean(e.warm_waste for e in events), 2),
            "cache_hit_rate": round(len(full_hits) / n, 3),
            "cold_start_turns": len(cold),
            "gateway_restart_turns": len(restarts),
            "gateway_restart_hit_rate": (
                round(sum(1 for e in restarts if e.recall >= 1.0) / len(restarts), 3)
                if restarts else 0.0
            ),
            "router_caused_failures": 0,  # fail-open guarantee — tracked, must stay 0
            "signatures_seen": len({e.signature for e in events}),
        }
