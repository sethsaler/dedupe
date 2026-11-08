"""Which decoded images are animations.

Pillow reports a JPEG with a Multi-Picture (MPO) extension as a multi-frame
image. Phones and apps write MPO for gain maps, depth maps, and stereo pairs:
the extra images are not playback frames. Often they are truncated or
stripped by the app that re-saved the photo. Treating them as an animation
made hashing fail on the cut-off second image. A clean copy of the same photo
also could not match, because its frame count differed.
"""

from __future__ import annotations

#: Formats whose extra "frames" are auxiliary images, not playback frames.
STILL_MULTI_IMAGE_FORMATS = frozenset({"MPO"})


def is_animated_image(img) -> bool:
    """Whether ``img`` is a real animation (GIF, APNG, animated WebP, …)."""
    if getattr(img, "format", None) in STILL_MULTI_IMAGE_FORMATS:
        return False
    return bool(getattr(img, "is_animated", False))


def animation_frame_count(img) -> int:
    """Playback frames to sample: 1 for stills, including multi-picture JPEGs."""
    if not is_animated_image(img):
        return 1
    return int(getattr(img, "n_frames", 1))
