"""Deterministic context signatures.

Design decision (design review): a signature is an INTERPRETATION of an
observation. It must be:
  - deterministic and reproducible (same context, same signature — even with
    different wording)
  - task-bearing, not channel-bearing: the same task on another channel must
    produce the SAME signature (channel is secondary, used as a lookup weight,
    not as a signature part)
  - valid without continuity (no continuity signal -> still a valid signature)
  - free of timestamps and random ids (stable over time)

Signature format:  intent|continuity-slug|recent-slug

  infrastructure|matrix-caddy|terminal-file

Not pretty, but stable. No LLM, no embeddings (decision: deterministic
rules first).
"""

from __future__ import annotations

from .observations import Observation, _norm_tokens


def canonical_signature(obs: Observation) -> str:
    """Build the canonical signature from a (normalized) observation.

    The observation should be normalized first (obs.normalized()); the
    function normalizes defensively anyway.
    """
    o = obs.normalized()
    intent = o.intent if o.intent else "unknown"
    continuity = _slug(o.continuity_refs)
    recent = _slug(o.recent_toolsets)
    parts = [intent, continuity, recent]
    # strip empty slots but keep position markers so similar contexts stay
    # visibly similar
    return "|".join(p for p in parts if p)


def continuity_slug(tokens: list[str]) -> str:
    """Normalized, sorted, deduped continuity tokens joined by '-'.

    Empty input -> empty string (signature stays valid without continuity).
    """
    return _slug(_norm_tokens(tokens))


def recent_slug(toolsets: list[str]) -> str:
    return _slug(_norm_tokens(toolsets))


def _slug(tokens: list[str]) -> str:
    return "-".join(_norm_tokens(tokens))


# --- compatibility helpers ------------------------------------------------

def signature_from_fields(
    *,
    intent: str,
    continuity_refs: list[str] | None = None,
    recent_toolsets: list[str] | None = None,
) -> str:
    """Convenience builder for callers without a full Observation."""
    return canonical_signature(
        Observation(
            channel="",
            intent=intent,
            continuity_refs=continuity_refs or [],
            recent_toolsets=recent_toolsets or [],
        )
    )
