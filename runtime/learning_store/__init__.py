"""Toolshed learning layer: observation -> signature -> profile store.

Capability learning pipeline (V2). This layer never touches the tool surface
directly — it records, persists and (later, via predictor.py/scoring.py)
predicts. The V1 router in the repo root stays untouched.
"""

from .observations import INTENT_CLASSES, Observation, registry_fingerprint
from .profile_store import ProfileStore
from .signature import canonical_signature, signature_from_fields

__all__ = [
    "INTENT_CLASSES",
    "Observation",
    "ProfileStore",
    "canonical_signature",
    "registry_fingerprint",
    "signature_from_fields",
]
