"""Ordered video fingerprint comparisons."""

from dedupe.similar_video import video_fingerprint_distances


def test_video_fingerprint_preserves_frame_order() -> None:
    first = "0000000000000000"
    second = "ffffffffffffffff"
    ordered = f"v2:{first},{second}"
    reversed_order = f"v2:{second},{first}"

    assert video_fingerprint_distances(ordered, ordered) == [0, 0]
    assert video_fingerprint_distances(ordered, reversed_order) == [64, 64]
