"""Direct tests for the no-person-review membership policy."""

from types import SimpleNamespace

from dedupe.face_detection import face_detection_signature
from dedupe.human_detection import human_detection_signature
from dedupe.human_policy import (
    HUMAN_DETECTION_CACHE_VERSION,
    has_current_human_signature,
    has_retained_human_face,
    is_current_no_person_decision,
    may_enter_no_person_review,
)


def test_has_current_human_signature() -> None:
    current = human_detection_signature("opencv")
    assert has_current_human_signature(current) is True
    assert has_current_human_signature(human_detection_signature("photon")) is True
    assert has_current_human_signature(human_detection_signature("ensemble")) is True
    # Prefix-only, truncated, or mismatched parameter signatures are rejected.
    assert has_current_human_signature(f"{HUMAN_DETECTION_CACHE_VERSION}|opencv") is False
    assert (
        has_current_human_signature(f"{HUMAN_DETECTION_CACHE_VERSION}|opencv|confidence=0.25")
        is False
    )
    assert has_current_human_signature(current.replace("frame-decode=v3", "frame-decode=v2")) is False
    assert has_current_human_signature(current + "|extra=1") is False
    assert has_current_human_signature("human-presence-v1|opencv") is False
    assert has_current_human_signature(None) is False
    assert has_current_human_signature("") is False


def test_is_current_no_person_decision() -> None:
    signature = human_detection_signature("opencv")
    assert is_current_no_person_decision("no_person_detected", signature) is True
    assert is_current_no_person_decision("person_detected", signature) is False
    assert is_current_no_person_decision("no_person_detected", f"{HUMAN_DETECTION_CACHE_VERSION}|opencv") is False
    assert is_current_no_person_decision("no_person_detected", "old-version") is False
    assert is_current_no_person_decision(None, signature) is False


def test_has_retained_human_face() -> None:
    face_sig = face_detection_signature()
    assert (
        has_retained_human_face(
            SimpleNamespace(face_count=0, female_face_count=0, face_detection_signature=face_sig)
        )
        is False
    )
    assert (
        has_retained_human_face(
            SimpleNamespace(face_count=None, female_face_count=None, face_detection_signature=None)
        )
        is False
    )
    # Unsigned or cleared face counts do not count as verified face verdicts.
    assert has_retained_human_face(SimpleNamespace(face_count=2, female_face_count=0)) is False
    assert (
        has_retained_human_face(
            SimpleNamespace(face_count=2, female_face_count=1, face_detection_signature="")
        )
        is False
    )
    assert (
        has_retained_human_face(
            SimpleNamespace(face_count=2, female_face_count=0, face_detection_signature=face_sig)
        )
        is True
    )
    # A single female face vetoes Non-Human membership even at zero total faces.
    assert (
        has_retained_human_face(
            SimpleNamespace(face_count=0, female_face_count=1, face_detection_signature=face_sig)
        )
        is True
    )


def test_may_enter_no_person_review() -> None:
    signature = human_detection_signature("opencv")
    face_sig = face_detection_signature()
    candidate = SimpleNamespace(
        human_detection_status="no_person_detected",
        human_detection_signature=signature,
        face_count=0,
        female_face_count=0,
        face_detection_signature=face_sig,
    )
    assert may_enter_no_person_review(candidate) is True

    with_face = SimpleNamespace(
        human_detection_status="no_person_detected",
        human_detection_signature=signature,
        face_count=1,
        female_face_count=0,
        face_detection_signature=face_sig,
    )
    assert may_enter_no_person_review(with_face) is False

    stale = SimpleNamespace(
        human_detection_status="no_person_detected",
        human_detection_signature="human-presence-v1|opencv",
        face_count=0,
        female_face_count=0,
        face_detection_signature=face_sig,
    )
    assert may_enter_no_person_review(stale) is False
