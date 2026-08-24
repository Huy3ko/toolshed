"""Capability observations — raw, pre-signature data about a work block.

Design decision (design review): observations are STRUCTURED RAW DATA,
not interpretations. The context signature is an interpretation and may be
re-derived later without regenerating history. Keep observations as close to
the log lines as possible.

An observation describes one work block (one or more turns with a common
purpose) from the tooling point of view. It never contains conversation
content — only capability-level facts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Observation:
    """Raw capability-level facts about a work block.

    Fields:
        channel: where the work happened (telegram, matrix, web, cron, cli...)
        intent: coarse intent class (infrastructure, research, document,
            communication, coding, ...) — see INTENT_CLASSES.
        continuity_refs: normalized topic tokens from continuity/session
            context (e.g. ["matrix", "caddy"]). May be empty.
        recent_toolsets: toolsets that were initially present at block start
            (floor + warm set).
        discovered_toolsets: toolsets loaded on demand during the block
            (request_toolset / discovery events).
        successful_toolsets: toolsets actually used successfully.
        registry_fingerprint: stable hash of the capability registry at block
            start, so profile drift over time is visible.
        ts: unix timestamp of block start.
    """

    channel: str
    intent: str
    continuity_refs: list[str] = field(default_factory=list)
    recent_toolsets: list[str] = field(default_factory=list)
    discovered_toolsets: list[str] = field(default_factory=list)
    successful_toolsets: list[str] = field(default_factory=list)
    registry_fingerprint: str = ""
    ts: float = 0.0

    # --- normalization helpers -------------------------------------------

    def normalized(self) -> "Observation":
        """Return a canonical copy: sorted, deduped, lowercased tokens."""
        return Observation(
            channel=_norm_token(self.channel),
            intent=_norm_intent(self.intent),
            continuity_refs=_norm_tokens(self.continuity_refs),
            recent_toolsets=_norm_tokens(self.recent_toolsets),
            discovered_toolsets=_norm_tokens(self.discovered_toolsets),
            successful_toolsets=_norm_tokens(self.successful_toolsets),
            registry_fingerprint=self.registry_fingerprint,
            ts=self.ts,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "intent": self.intent,
            "continuity_refs": self.continuity_refs,
            "recent_toolsets": self.recent_toolsets,
            "discovered_toolsets": self.discovered_toolsets,
            "successful_toolsets": self.successful_toolsets,
            "registry_fingerprint": self.registry_fingerprint,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Observation":
        return cls(
            channel=d.get("channel", ""),
            intent=d.get("intent", ""),
            continuity_refs=list(d.get("continuity_refs", [])),
            recent_toolsets=list(d.get("recent_toolsets", [])),
            discovered_toolsets=list(d.get("discovered_toolsets", [])),
            successful_toolsets=list(d.get("successful_toolsets", [])),
            registry_fingerprint=d.get("registry_fingerprint", ""),
            ts=float(d.get("ts", 0.0)),
        )


# Coarse intent classes. Keep deliberately small — this is a bucket, not a taxonomy.
INTENT_CLASSES = (
    "infrastructure",
    "research",
    "document",
    "communication",
    "coding",
    "data",
    "media",
    "unknown",
)


def _norm_intent(intent: str) -> str:
    i = _norm_token(intent)
    return i if i in INTENT_CLASSES else "unknown"


def _norm_token(s: str) -> str:
    """Lowercase, keep [a-z0-9_-], collapse whitespace to single."""
    return " ".join(s.lower().split()).replace(" ", "-")


def _norm_tokens(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        # split compound tokens (e.g. "matrix/cinny" -> ["matrix", "cinny"])
        for part in item.lower().replace("/", "-").split("-"):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return sorted(out)


def registry_fingerprint(registry: dict[str, Any]) -> str:
    """Stable hash of a capability registry (toolset name -> tool count).

    Used to detect when the registry changed between observations.
    """
    if not registry:
        return ""
    payload = "|".join(
        f"{k}:{len(v) if hasattr(v, '__len__') else v}" for k, v in sorted(registry.items())
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
