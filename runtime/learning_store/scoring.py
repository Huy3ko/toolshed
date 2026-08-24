"""Scoring — reinforcement and decay for capability profiles.

Pure functions, deliberately simple (GPT review: no ML at the start):

  - actually used            score += 2
  - discovered (loaded)      score += 1
  - warm-loaded but unused   score -= 0.25
  - time decay               score *= decay_factor per elapsed period

Thresholds:
  score >= hot_threshold      -> warm set (preloaded)
  between candidate thresholds-> candidate / discovery
  below                       -> not preloaded

The store keeps raw counters (initially_present/actually_used/discovered/
successful). Scoring derives a number from them — the store itself never
decides anything.
"""

from __future__ import annotations

import math
import time
from typing import Any

# --- weights --------------------------------------------------------------

W_ACTUALLY_USED = 2.0
W_DISCOVERED = 1.0
W_WARM_UNUSED = -0.25  # present in working set but never used (floor bias)
HOT_THRESHOLD = 5.0
CANDIDATE_THRESHOLD = 2.0
DECAY_FACTOR = 0.9
DECAY_PERIOD_SECONDS = 7 * 24 * 3600  # one week


def toolset_score(entry: dict[str, Any], *, now: float | None = None) -> float:
    """Score a single toolset entry from the profile store.

    entry: {"initially_present": n, "actually_used": n, "discovered": n,
            "successful": n, "last_used": ts}
    """
    now = now if now is not None else time.time()
    used = float(entry.get("actually_used", 0))
    discovered = float(entry.get("discovered", 0))
    initially = float(entry.get("initially_present", 0))

    score = used * W_ACTUALLY_USED + discovered * W_DISCOVERED
    # warm-loaded but unused: present in the working set without ever being
    # used is a penalty — this is what keeps the warm set small
    warm_unused = max(0.0, initially - used)
    score += warm_unused * W_WARM_UNUSED

    # recency decay: score fades with time since last use
    last_used = float(entry.get("last_used", 0) or 0)
    if last_used > 0 and now > last_used:
        periods = (now - last_used) / DECAY_PERIOD_SECONDS
        score *= math.pow(DECAY_FACTOR, periods)

    return score


def classify_score(score: float, *, hot: float = HOT_THRESHOLD,
                   candidate: float = CANDIDATE_THRESHOLD) -> str:
    """Map a score to a tier: 'hot' | 'candidate' | 'cold'."""
    if score >= hot:
        return "hot"
    if score >= candidate:
        return "candidate"
    return "cold"


def apply_outcome(entry: dict[str, Any], *, used: bool, discovered: bool,
                  warm_unused: bool) -> dict[str, Any]:
    """Return an updated counter entry after one block outcome.

    Pure: does not mutate the input. The store's record() remains the primary
    write path; this is the reference for what a score delta should be.
    """
    out = dict(entry)
    out["score"] = out.get("score", 0.0)
    if used:
        out["score"] += W_ACTUALLY_USED
    if discovered:
        out["score"] += W_DISCOVERED
    if warm_unused:
        out["score"] += W_WARM_UNUSED
    return out
