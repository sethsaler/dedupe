"""Shared safety policy for trusted person-detection decisions."""

from __future__ import annotations

HUMAN_DETECTION_CACHE_VERSION = "human-presence-v2-yunet"
MANUALLY_CONFIRMED_HUMAN_STATUS = "person_confirmed"
CACHEABLE_HUMAN_STATUSES = frozenset(
    {"person_detected", "no_person_detected", MANUALLY_CONFIRMED_HUMAN_STATUS}
)


def has_current_human_signature(signature: str | None) -> bool:
    """Return whether a decision came from the currently trusted pipeline."""
    return (
        bool(signature)
        and signature.split("|", 1)[0] == HUMAN_DETECTION_CACHE_VERSION
    )


def is_current_no_person_decision(
    status: str | None, signature: str | None
) -> bool:
    """Only a current, explicit no-person result may enter Non-Human review."""
    return status == "no_person_detected" and has_current_human_signature(signature)
