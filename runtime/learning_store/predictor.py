"""Shadow predictor — predicts a working set WITHOUT changing the route.

Design (design review): the productive router keeps deciding. The shadow
predictor computes what it WOULD have chosen, and the evaluation later
compares predicted ∩ actually_used.

Prediction = floor ∪ hot toolsets (from the profile store for this signature).

The predictor NEVER touches the tool surface. It is read-only against the
profile store. Wiring it into the plugin (shadow mode) must be a separate,
explicit step (learning/shadow_hooks.py).
"""

from __future__ import annotations

from typing import Any

from .profile_store import ProfileStore
from .scoring import CANDIDATE_THRESHOLD, HOT_THRESHOLD, toolset_score


class ShadowPredictor:
    def __init__(
        self,
        store: ProfileStore,
        *,
        floor_toolsets: list[str] | None = None,
        hot_threshold: float = HOT_THRESHOLD,
        candidate_threshold: float = CANDIDATE_THRESHOLD,
        max_warm: int = 8,
    ):
        self.store = store
        self.floor = sorted(set(floor_toolsets or []))
        self.hot_threshold = hot_threshold
        self.candidate_threshold = candidate_threshold
        self.max_warm = max_warm

    # --- prediction ---------------------------------------------------------

    def predict(self, signature: str) -> dict[str, Any]:
        """Predict the warm set for a signature. Read-only.

        Returns:
          {
            "signature": ...,
            "floor": [...],
            "hot": [...],            # toolsets above hot threshold
            "candidates": [...],     # above candidate threshold
            "predicted": [...],      # floor + hot (bounded by max_warm)
            "scores": {toolset: score},
            "profile_hits": n,       # how often this signature was seen
          }
        """
        profile = self.store.get(signature)
        scores: dict[str, float] = {}
        if profile:
            for toolset, entry in profile.get("toolsets", {}).items():
                scores[toolset] = toolset_score(entry)

        hot = sorted(t for t, s in scores.items() if s >= self.hot_threshold)
        candidates = sorted(
            t for t, s in scores.items()
            if self.candidate_threshold <= s < self.hot_threshold
        )

        # warm set: floor always + hot toolsets, bounded so the surface stays
        # small (the whole point — recall at minimal warm surface)
        warm = list(self.floor)
        for t in hot:
            if t not in warm:
                warm.append(t)
            if len(warm) >= self.max_warm:
                break

        return {
            "signature": signature,
            "floor": list(self.floor),
            "hot": hot,
            "candidates": candidates,
            "predicted": sorted(warm),
            "scores": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
            "profile_hits": profile["seen"] if profile else 0,
        }

    # --- evaluation -----------------------------------------------------------

    @staticmethod
    def evaluate(predicted: list[str], actual: list[str]) -> dict[str, float]:
        """Precision / recall / warm_waste for one prediction.

        warm_waste = predicted - actual (capabilities loaded but unused).
        The optimizer goal: maximize recall at minimal warm surface — NOT
        raw hit rate (a router that always loads 25 tools would score 100%
        recall and be useless).
        """
        p = set(predicted)
        a = set(actual)
        if not a:
            return {"precision": 0.0, "recall": 0.0, "warm_waste": float(len(p))}
        tp = len(p & a)
        precision = tp / len(p) if p else 0.0
        recall = tp / len(a)
        warm_waste = float(len(p - a))
        return {
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "warm_waste": warm_waste,
        }
