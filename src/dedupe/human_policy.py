"""Shared safety policy for trusted person-detection decisions."""

from __future__ import annotations

import math

HUMAN_DETECTION_CACHE_VERSION = "human-presence-v6-yolox"
MANUALLY_CONFIRMED_HUMAN_STATUS = "person_confirmed"
CACHEABLE_HUMAN_STATUSES = frozenset(
    {"person_detected", "no_person_detected", MANUALLY_CONFIRMED_HUMAN_STATUS}
)


def has_current_human_signature(signature: str | None) -> bool:
    """Return whether a decision matches a full, current pipeline signature."""
    if not isinstance(signature, str) or not signature:
        return False
    parts = signature.split("|")
    if len(parts) < 5 or parts[0] != HUMAN_DETECTION_CACHE_VERSION:
        return False
    from .human_detection import (
        DEFAULT_CONFIDENCE,
        DEFAULT_PHOTON_MODEL,
        HUMAN_BACKENDS,
        human_detection_signature,
    )

    backend = parts[1]
    if backend not in HUMAN_BACKENDS:
        return False
    kv: dict[str, str] = {}
    for part in parts[2:]:
        if "=" not in part:
            return False
        key, value = part.split("=", 1)
        if not key or not value or key in kv:
            return False
        kv[key] = value
    confidence = DEFAULT_CONFIDENCE
    if "confidence" in kv:
        try:
            confidence = float(kv["confidence"])
        except ValueError:
            return False
        if not math.isfinite(confidence) or confidence < 0.0:
            return False
    photon_model = kv.get("model", DEFAULT_PHOTON_MODEL)
    return signature == human_detection_signature(
        backend,
        confidence=confidence,
        photon_model=photon_model,
    )


def is_current_no_person_decision(
    status: str | None, signature: str | None
) -> bool:
    """Only a current, explicit no-person result may enter Non-Human review."""
    return status == "no_person_detected" and has_current_human_signature(signature)


def has_retained_human_face(record: object) -> bool:
    """Any counted face from a signed face-detection pass — especially a female face — must be kept."""
    signature = getattr(record, "face_detection_signature", None)
    if not isinstance(signature, str) or not signature.strip():
        return False
    female_faces = getattr(record, "female_face_count", None) or 0
    faces = getattr(record, "face_count", None) or 0
    return female_faces >= 1 or faces >= 1


def may_enter_no_person_review(record: object) -> bool:
    """Membership rule: current no-person verdict and no counted human face."""
    return is_current_no_person_decision(
        getattr(record, "human_detection_status", None),
        getattr(record, "human_detection_signature", None),
    ) and not has_retained_human_face(record)
