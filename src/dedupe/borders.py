"""Black-border detection and in-place cropping for still images.

A scan with border detection enabled looks along each edge of every
croppable image for solid black bars — letterboxing, pillarboxing, scanner
margins, screenshots of videos — and suggests the crop that removes them.
Images that look like screenshots are also checked for interface at the top
and bottom edges: a phone's status bar (clock, battery, notification icons),
a home indicator or navigation bar, a desktop menu bar or taskbar, and the
rows an app stacks between those and the picture — its header, the row naming
a post's author, a reply field, an action row.
Applying a suggestion rewrites the file at its original path; the untouched
original goes to the system Trash first, so every crop can be undone.

Crop boxes are expressed in *display orientation* (after the EXIF orientation
tag is applied), which is how thumbnails and the browser show the image. A
cropped file is saved upright with its orientation tag reset.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import functools
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .models import FileRecord, MediaType
from .parallel import map_parallel

# Bump when the detection rules change so cached verdicts are recomputed.
# A user's dismissal under an earlier signature still stands.
BORDER_SIGNATURE = "black-borders-v4"
LEGACY_BORDER_SIGNATURES = frozenset(
    {"black-borders-v1", "black-borders-v2", "black-borders-v3"}
)
# Cached verdict for a file the user dismissed ("No crop needed").
BORDER_DISMISSED = "dismissed"

# Formats Pillow can both read and write back without changing the file type.
# RAW, GIF (animation), and video are deliberately excluded.
CROPPABLE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
}

# A bar is black: its level (the median of the outermost line) is at or
# below this luma (0–255). JPEG ringing and sensor noise keep "black" bars a
# few levels above zero.
DARK_LUMA = 28
# A bar is one flat level. A line belongs to the bar only when nearly every
# pixel sits within BAR_TOLERANCE of the bar's level. Dark hair, a shadowed
# limb or a dark backdrop has texture and drifts above the level, so it
# stops the bar instead of being swallowed by it.
BAR_TOLERANCE = 12
BAR_LINE_FRACTION = 0.995
# On a screenshot, a speck — the privacy dot a phone shows over a full-screen
# video — does not end a bar: within BAR_SPECK_ZONE of the height from the top
# or bottom edge, where a phone draws its status bar, a line whose pixels off
# the level all sit within BAR_SPECK_FRACTION of its length is flat too, so
# long as the specks there span no more lines than that. Deeper in, a moon in
# a flat night sky ends the bar, as it does on any photo.
BAR_SPECK_FRACTION = 0.02
BAR_SPECK_ZONE = 0.04
# A JPEG stores colour at half size in blocks of 8, so a dot's ringing can
# reach the 16 lines around it while staying faint enough to leave those
# lines at the bar's level.
BAR_SPECK_RINGING = 16
# A dot's own ink stands off the bar at least this share as far as its
# brightest pixel on the line; the ringing a JPEG leaves beside it is fainter.
BAR_SPECK_INK = 0.25
# A bar ends at a hard edge: within BAR_EDGE_LINES past it, some line must
# be clearly picture, with at least this share of it off the bar's level.
# Black headroom that fades into a dark subject, or a night scene darkening
# toward the frame, changes a few pixels per line instead and is left alone:
# a missed crop costs nothing, a crop through someone's hair does.
BAR_EDGE_LINES = 4
BAR_EDGE_FRACTION = 0.2
# Ignore slivers: a bar must be at least this many pixels and this share of
# the image's dimension along that axis.
MIN_BORDER_PIXELS = 2
MIN_BORDER_FRACTION = 0.004
# The remaining picture must keep at least this share of each dimension;
# otherwise the image is mostly black (a night sky, a dark frame) rather
# than a picture framed by bars.
MIN_CONTENT_FRACTION = 0.2
# First-pass decode size. JPEGs decode at a reduced DCT scale for the quick
# check; only images that show bars are decoded again at full resolution.
SCREEN_MAX_SIDE = 1024

JPEG_SAVE_QUALITY = 95
LOSSY_SAVE_QUALITY = 92

# --- screenshot interface ---------------------------------------------------
# Only images with some sign of being a screenshot are checked for interface
# bars: a screenshot-style name, the "Screenshot" tag iOS writes, the macOS
# screen-capture flag, or a screen-sized frame with no camera behind it.
SCREENSHOT_NAME_PATTERN = re.compile(
    r"screen[ _-]?shot|screen[ _-]?capture|screencap|bildschirmfoto"
    r"|captura de pantalla|capture d.?[ée]cran",
    re.IGNORECASE,
)
PHONE_SCREEN_WIDTHS = frozenset(
    {640, 720, 750, 828, 1080, 1125, 1170, 1179, 1206, 1242, 1260, 1284, 1290, 1320, 1440}
)
DESKTOP_SCREEN_WIDTHS = frozenset(
    {1280, 1366, 1440, 1470, 1512, 1536, 1600, 1680, 1710, 1728, 1920, 2048, 2560,
     2880, 2940, 3024, 3072, 3360, 3440, 3456, 3840, 5120}
)
SCREEN_CAPTURE_XATTR = "com.apple.metadata:kMDItemIsScreenCapture"
# Interface is read row by row, sorting each row by the colours it holds.
# Pixels within this per-channel distance of a colour count as that colour.
CHROME_TOLERANCE = 14
# The outermost row is mostly one colour: the bar's background. Glyphs that
# an earlier crop clipped may touch the edge, so the row need not be clean.
CHROME_EDGE_FLAT = 0.6
# A row holds something when at least this share of it is off the background.
CHROME_INK_FRACTION = 0.003
# A row of one colour: background, a hairline, a band of another colour.
CHROME_ONE_COLOUR = 0.97
# Interface rows hold a few flat colours — background, ink, a pill, a tinted
# badge — plus the anti-aliased pixels between them. A row with less of
# itself on three colours than this is picture.
CHROME_PLAIN = 0.8
# Glyph ink stands at least this far from its background.
CHROME_CONTRAST = 40
# Glyphs are narrow: no run of ink in a glyph row is longer than this share
# of the width. A longer run is a pill, a button, a field, or picture.
CHROME_GLYPH_RUN = 0.12
# A pill — an active call's green capsule beside the clock, a button's
# background — runs wider than a glyph but within this share of the width.
CHROME_PILL_RUN = 0.2
# Two inks of like weight on a row — a white clock beside a green pill — pull
# the median of the ink to a colour that is neither; the peel settles on the
# ink within this distance of it.
CHROME_PEEL_REACH = 64
CHROME_BAR_RUNS = 4  # glyphs across a bar's row; two corners or three knobs are fewer
# Bars begin after a blank margin (share of height); shapes cut by the edge
# — rounded corners, drawer knobs — do not, and need a glyph more to count.
CHROME_BAR_MARGIN = 0.008
# The letters of a word stand within this share of the width of each other,
# and none is wider than this share (an avatar is).
CHROME_LETTER_GAP = 0.012
CHROME_LETTER_WIDTH = 0.04
# Page dots — the marks under a carousel — stand within this share of the
# height, their row within this share of the width at the centre, and no dot
# wider than this share.
CHROME_DOTS_ROWS = 0.015
CHROME_DOTS_WIDTH = 0.2
CHROME_DOT_WIDTH = 0.03
# One page dot — the current page — differs from the rest by more than this
# (0–255, in any channel), and the rest agree within half of it.
CHROME_DOT_MARK = 48
# A row of icons or buttons is at least this share of the height tall; a
# shorter row of marks holding no word is a picture's birds or boats.
CHROME_GLYPH_ROWS = 0.008
# A row of several glyphs — icons, buttons — reaches across at least this
# share of the width; a few marks close together are a picture's birds.
CHROME_GLYPH_SPREAD = 0.3
# A line of text content spans the width in many short, closely spaced runs;
# an action row's icons and counts sit far apart.
CHROME_TEXT_RUNS = 24
CHROME_TEXT_EXTENT = 0.7
CHROME_TEXT_COVERAGE = 0.75
# A caption or headline covers at least this share of its rows with ink.
CHROME_DENSE_INK = 0.3
# The picture begins where at least this share of the height is busy rows.
CHROME_BUSY_ROWS = 0.004
# How far past the cut limit the detector reads to tell a picture from more
# interface (share of height).
CHROME_LOOKAHEAD = 0.1
# A post's paragraphs are written in one ink: the fainter stands off the band
# at least this share as far as the other, where an app's greyed-out date or
# time under the post's words does not.
CHROME_ONE_INK = 0.85
# A timeline post sets its picture in the words' column: its side within this
# share of the width of where the name and words begin.
CHROME_COLUMN_ALIGN = 0.02
CHROME_MIN_FRACTION = 0.012
CHROME_SAMPLE_COLUMNS = 480


@dataclass(frozen=True)
class ChromeEdge:
    """How far interface may reach in from one edge (shares of height)."""

    max_fraction: float
    # The first glyphs sit this close to the edge (a clock, a nav pill).
    first_ink_max: float
    # Interface elements are short: a cluster of rows is at most this tall
    # unless it is text, and a longer run of wide two-toned rows is picture.
    cluster_max: float
    # A home indicator — a centred pill — lives at this edge.
    pill: bool
    # A carousel's page dots sit under the picture, so only at this edge.
    dots: bool


# Status bar or menu bar, then an app's header and the row naming a post's author.
CHROME_TOP = ChromeEdge(
    max_fraction=0.22, first_ink_max=0.07, cluster_max=0.045, pill=False, dots=False
)
# Home indicator, navigation buttons, tab bar, taskbar, reply field, action row.
CHROME_BOTTOM = ChromeEdge(
    max_fraction=0.30, first_ink_max=0.06, cluster_max=0.08, pill=True, dots=True
)


@dataclass(frozen=True)
class CropBox:
    """A crop rectangle in display orientation: ``[left, right) × [top, bottom)``."""

    left: int
    top: int
    right: int
    bottom: int
    # Full display-oriented image size the box was measured against.
    width: int
    height: int
    # Edges ("top", "bottom") whose crop removes screenshot interface rather
    # than black bars.
    chrome: tuple[str, ...] = ()

    @property
    def crop_width(self) -> int:
        return self.right - self.left

    @property
    def crop_height(self) -> int:
        return self.bottom - self.top

    @property
    def removed_fraction(self) -> float:
        total = self.width * self.height
        if total <= 0:
            return 0.0
        return 1.0 - (self.crop_width * self.crop_height) / total

    def encode(self) -> str:
        parts = [
            str(value)
            for value in (self.left, self.top, self.right, self.bottom, self.width, self.height)
        ]
        if self.chrome:
            parts.append("+".join(self.chrome))
        return ",".join(parts)

    @classmethod
    def decode(cls, value: str | None) -> CropBox | None:
        if not value or value == BORDER_DISMISSED:
            return None
        parts = value.split(",")
        chrome: tuple[str, ...] = ()
        if len(parts) == 7:
            chrome = tuple(edge for edge in parts.pop().split("+") if edge in ("top", "bottom"))
        try:
            left, top, right, bottom, width, height = (int(part) for part in parts)
        except ValueError:
            return None
        box = cls(left, top, right, bottom, width, height, chrome)
        return box if box.is_valid() else None

    def is_valid(self) -> bool:
        return (
            0 <= self.left < self.right <= self.width
            and 0 <= self.top < self.bottom <= self.height
            and (self.left, self.top, self.right, self.bottom) != (0, 0, self.width, self.height)
        )

    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
            "crop_width": self.crop_width,
            "crop_height": self.crop_height,
            "removed_fraction": round(self.removed_fraction, 4),
            "chrome": list(self.chrome),
        }


def is_croppable(record: FileRecord) -> bool:
    return (
        record.media_type == MediaType.IMAGE
        and Path(record.path).suffix.lower() in CROPPABLE_EXTENSIONS
    )


def suggested_crop(record: FileRecord) -> CropBox | None:
    """The record's current crop suggestion, or ``None`` (none, dismissed, stale)."""
    if record.border_signature != BORDER_SIGNATURE:
        return None
    return CropBox.decode(record.border_crop)


def _register_heif() -> None:
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass


def _luma(image):
    """Grayscale numpy array with transparency composited onto white.

    Transparent margins are not black bars; flattening onto white keeps
    them from reading as dark pixels.
    """
    import numpy as np
    from PIL import Image

    has_alpha = image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        array = np.asarray(image, dtype=np.float32)
        peak = float(array.max()) or 1.0
        return (array * (255.0 / max(peak, 255.0))).astype(np.uint8)
    return np.asarray(image.convert("L"))


def _edge_bar(lines, *, speck_lines: int = 0) -> int:
    """Thickness of the black bar at the start of ``lines`` (ordered from the edge).

    ``lines`` is a 2-D luma array, one row per line running parallel to the
    edge. Returns 0 when the edge is not a flat black band ending in a hard
    edge against the picture. A dot within the first ``speck_lines`` lines
    (a screenshot's status bar) does not end the bar.
    """
    import numpy as np

    level = float(np.median(lines[0]))
    if level > DARK_LUMA:
        return 0
    distance = np.abs(lines.astype(np.int16) - level)
    near = distance <= BAR_TOLERANCE
    flat = near.mean(axis=1) >= BAR_LINE_FRACTION
    speck_lines = min(speck_lines, len(lines))
    if speck_lines and not flat[:speck_lines].all():
        off = ~near[:speck_lines]
        # A dot is as wide as its own ink: faint ringing beside it on its
        # lines does not make it wider.
        strength = distance[:speck_lines]
        ink = off & (strength >= strength.max(axis=1, keepdims=True) * BAR_SPECK_INK)
        first = np.argmax(ink, axis=1)
        last = ink.shape[1] - np.argmax(ink[:, ::-1], axis=1)
        reach = max(2, int(off.shape[1] * BAR_SPECK_FRACTION))
        specked = off.any(axis=1) & (last - first <= reach)
        # A dot is as short as it is wide; a mast or a streak is not, even one
        # broken by lines at the bar's level: specks nearer each other than a
        # dot's height are one object. Lines at the level within a JPEG's
        # ringing of the dot's own lines do not make it taller; a mast
        # reaches past them.
        where = np.flatnonzero(specked)
        if where.size:
            begins = np.concatenate(([True], np.diff(where) > reach))
            group = np.cumsum(begins) - 1
            for index in range(int(group[-1]) + 1):
                lines_of = where[group == index]
                own = lines_of[~flat[lines_of]]
                if own.size == 0:
                    continue
                low = lines_of[lines_of < own[0] - BAR_SPECK_RINGING]
                high = lines_of[lines_of > own[-1] + BAR_SPECK_RINGING]
                top = low[0] if low.size else own[0]
                bottom = high[-1] if high.size else own[-1]
                specked[lines_of] = bottom - top + 1 <= reach
        flat[:speck_lines] |= specked
    run = int(flat.size) if flat.all() else int(np.argmin(flat))
    if run == 0 or run >= len(lines):
        return run
    off = 1.0 - near[run : run + BAR_EDGE_LINES].mean(axis=1)
    return run if off.max() >= BAR_EDGE_FRACTION else 0


def detect_borders_in_array(
    luma, *, speck: bool | tuple[bool, bool] = False
) -> tuple[int, int, int, int] | None:
    """Black-bar thickness ``(top, bottom, left, right)`` for a luma array.

    Returns ``None`` when no edge carries a bar worth cropping or when the
    picture left over would be too small to be the real content. ``speck``
    lets a dot ride along in a top or bottom bar's status-bar rows, as on a
    screenshot; a pair says which of the two edges allow it.
    """
    import numpy as np

    height, width = luma.shape[:2]
    if height < 8 or width < 8:
        return None
    # A bar never reaches past the share the picture must keep, so only that
    # much of each edge is examined.
    reach_rows = height - int(np.ceil(height * MIN_CONTENT_FRACTION)) + BAR_EDGE_LINES
    reach_cols = width - int(np.ceil(width * MIN_CONTENT_FRACTION)) + BAR_EDGE_LINES
    speck_top, speck_bottom = (speck, speck) if isinstance(speck, bool) else speck
    zone = max(2, int(height * BAR_SPECK_ZONE))
    top = _edge_bar(luma[:reach_rows], speck_lines=zone if speck_top else 0)
    if top >= height:
        return None  # the whole frame is black
    bottom = _edge_bar(luma[::-1][:reach_rows], speck_lines=zone if speck_bottom else 0)
    left = _edge_bar(luma.T[:reach_cols])
    right = _edge_bar(luma.T[::-1][:reach_cols])

    min_rows = max(MIN_BORDER_PIXELS, int(np.ceil(height * MIN_BORDER_FRACTION)))
    min_cols = max(MIN_BORDER_PIXELS, int(np.ceil(width * MIN_BORDER_FRACTION)))
    top = top if top >= min_rows else 0
    bottom = bottom if bottom >= min_rows else 0
    left = left if left >= min_cols else 0
    right = right if right >= min_cols else 0
    if not (top or bottom or left or right):
        return None
    if height - top - bottom < height * MIN_CONTENT_FRACTION:
        return None
    if width - left - right < width * MIN_CONTENT_FRACTION:
        return None
    return top, bottom, left, right


def _flattened(image):
    """``image`` with transparency composited onto white, as 8-bit RGB."""
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        return image.convert("L").convert("RGB")
    return image.convert("RGB")


def _has_screen_capture_flag(path: Path) -> bool:
    try:
        return SCREEN_CAPTURE_XATTR in _list_xattrs(path)
    except OSError:
        return False


def screenshot_evidence(path: str | Path, image) -> str | None:
    """Why ``image`` looks like a screenshot (``"name"``, ``"metadata"``,
    ``"shape"``), or ``None`` when nothing suggests one.

    ``image`` is the opened, not yet transposed source; only its header is read.
    """
    path = Path(path)
    if SCREENSHOT_NAME_PATTERN.search(path.name):
        return "name"
    if _has_screen_capture_flag(path):
        return "metadata"
    try:
        exif = image.getexif()
        exif_ifd = exif.get_ifd(0x8769)
    except Exception:
        exif, exif_ifd = {}, {}
    comment = exif_ifd.get(0x9286)  # UserComment: iOS writes "Screenshot"
    if isinstance(comment, bytes):
        comment = comment.decode("latin-1", "ignore")
    if isinstance(comment, str) and "screenshot" in comment.lower():
        return "metadata"
    xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "ignore")
    if isinstance(xmp, bytes) and b"screenshot" in xmp.lower():
        return "metadata"
    if exif.get(0x010F) or exif.get(0x0110):
        return None  # a camera Make or Model: a photo, not a screen
    width, height = image.size
    if exif.get(0x0112, 1) in (5, 6, 7, 8):
        width, height = height, width
    if height >= width * 1.3 and width in PHONE_SCREEN_WIDTHS:
        return "shape"
    if width > height and width in DESKTOP_SCREEN_WIDTHS and 1.25 <= width / height <= 2.5:
        return "shape"
    return None


@dataclass
class _Rows:
    """What each row of a strip holds, as arrays indexed by row."""

    medians: object  # the row's main colour
    ink: object  # share of the row off its main colour
    strong: object  # share of the row far off its main colour
    one_colour: object  # the row is a single colour
    busy: object  # more than a few flat colours: picture
    glyph: object  # short runs of contrasting ink on one colour
    faint: object  # a little weak ink: compression ringing, a gradient's edge
    two_tone: object  # other inked rows: a pill, a field, a wide or weak shape
    runs: object  # ink runs across the row
    longest: object  # the longest ink run, as a share of the width
    span: object  # (first, last + 1) ink column, as shares of the width
    coverage: object  # share of the span within a letter's gap of ink
    centre: object  # share of the middle fifth holding strong ink
    sides: object  # (left, right): ink present in the outer 40% on that side
    far: object  # pixels off the row's main colour, by row and column
    pixels: object  # the strip itself, by row and column


def _masked_medians(pixels, mask):
    """Per-row, per-channel median of the ``pixels`` (rows × columns × 3,
    0..255) under ``mask``, by histogram; rows with nothing under the mask
    give 0."""
    import numpy as np

    count = mask.shape[0]
    bins = count * 3 * 256
    lanes = (
        np.arange(count, dtype=np.int32)[:, None, None] * 3
        + np.arange(3, dtype=np.int32)[None, None, :]
    ) * 256
    flat = lanes + pixels
    flat[~mask] = bins  # a dump bin for the pixels outside the mask
    histogram = np.bincount(flat.ravel(), minlength=bins + 1)[:bins].reshape(count, 3, 256)
    cumulative = histogram.cumsum(axis=2)
    total = mask.sum(axis=1)
    low = np.maximum((total - 1) // 2, 0)[:, None, None]
    high = (total // 2)[:, None, None]
    medians = ((cumulative <= low).sum(axis=2) + (cumulative <= high).sum(axis=2)) / 2.0
    return np.where(total[:, None] > 0, medians, 0.0)


def _near_segment(pixels, doubled, base, colour):
    """Pixels of each row within the tolerance of the segment from the row's
    ``base`` colour to ``colour``: an ink and its blends with the band behind
    it, which anti-aliasing draws anywhere along that segment."""
    import numpy as np

    axis = colour - base
    doubled_axis = (2 * axis).astype(np.int32)
    scale = np.maximum((axis * axis).sum(axis=1), 1.0)[:, None]
    along = np.einsum("rck,rk->rc", doubled, doubled_axis, dtype=np.int64) / 4.0 / scale
    along = np.clip(along, 0.0, 1.0)
    worst = None
    for channel in range(3):  # channel by channel keeps the temporaries small
        foot = base[:, channel][:, None] + along * axis[:, channel][:, None]
        distance = np.abs(pixels[:, :, channel] - foot)
        worst = distance if worst is None else np.maximum(worst, distance)
    return worst <= CHROME_TOLERANCE


def _peel_colours(window, medians, far, one_colour):
    """Share of each row near its colours: the background, then the median of
    what is left (the ink), then the median of what is still left (a pill, a
    second ink). Rows of one colour are wholly near."""
    import numpy as np

    plain = np.ones(len(window))
    rows = np.flatnonzero(~one_colour)
    if rows.size == 0:
        return plain
    pixels = window[rows].astype(np.int32)  # (rows, columns, 3)
    columns = pixels.shape[1]
    base = medians[rows]  # whole or half values
    doubled = 2 * pixels - (2 * base).astype(np.int32)[:, None, :]  # exact
    off = far[rows].copy()
    near = ~off
    for _ in range(2):
        active = off.any(axis=1)
        if not active.any():
            break
        colour = _masked_medians(pixels, off)
        close = _near_segment(pixels, doubled, base, colour) & active[:, None]
        # Two inks of like weight on a row — a white clock beside a green
        # pill — pull the median to a colour that is neither. Where that left
        # most of a sparse row's ink unexplained, settle on the ink within
        # reach of the median — a real share of what is left, not a few
        # stray pixels of compression — and keep that when it explains more.
        left = off.sum(axis=1)
        unsettled = np.flatnonzero(
            active & (left <= 0.5 * columns) & ((close & off).sum(axis=1) < 0.5 * left)
        )
        if unsettled.size:
            some = pixels[unsettled]
            worst = None
            for channel in range(3):
                distance = np.abs(some[:, :, channel] - colour[unsettled, channel][:, None])
                worst = distance if worst is None else np.maximum(worst, distance)
            nearby = off[unsettled] & (worst <= CHROME_PEEL_REACH)
            settled = _masked_medians(some, nearby)
            better = _near_segment(some, doubled[unsettled], base[unsettled], settled)
            better &= (nearby.sum(axis=1) >= 0.25 * left[unsettled])[:, None]
            improved = (better & off[unsettled]).sum(axis=1) > (close[unsettled] & off[unsettled]).sum(axis=1)
            close[unsettled[improved]] = better[improved]
        near |= close
        off &= ~close
    plain[rows] = near.mean(axis=1)
    return plain


def _measure_rows(window) -> _Rows:
    import numpy as np

    count, columns = window.shape[:2]
    medians = np.median(window, axis=1)
    distance = np.abs(window - medians[:, None, :]).max(axis=2)
    far = distance > CHROME_TOLERANCE
    ink = far.mean(axis=1)
    strong = (distance > CHROME_CONTRAST).mean(axis=1)
    one_colour = ink <= 1.0 - CHROME_ONE_COLOUR
    inked = ink >= CHROME_INK_FRACTION
    plain = _peel_colours(window, medians, far, one_colour)
    busy = plain < CHROME_PLAIN
    # Runs of ink along each row.
    before = np.zeros_like(far)
    before[:, 1:] = far[:, :-1]
    runs = (far & ~before).sum(axis=1)
    counted = np.cumsum(far, axis=1)
    reset = np.maximum.accumulate(np.where(far, 0, counted), axis=1)
    longest = (counted - reset).max(axis=1) / columns
    first = np.argmax(far, axis=1)
    last = columns - np.argmax(far[:, ::-1], axis=1)
    span = np.stack([first, last], axis=1) / columns
    # Ink within a letter's gap of other ink: text lines fill their span.
    radius = max(2, int(columns * 0.008))
    padded = np.zeros((count, columns + 1), dtype=counted.dtype)
    padded[:, 1:] = counted
    positions = np.arange(columns)
    ahead = np.minimum(positions + radius + 1, columns)
    behind = np.maximum(positions - radius, 0)
    dilated = padded[:, ahead] - padded[:, behind]
    coverage = (dilated > 0).sum(axis=1) / np.maximum(last - first, 1)
    glyph = (
        inked
        & ~busy
        & (ink <= 0.5)
        & (longest <= CHROME_GLYPH_RUN)
        & (strong >= ink * 0.5)
    )
    faint = inked & ~busy & (ink < 0.1) & (strong < ink * 0.5)
    two_tone = inked & ~busy & ~glyph & ~faint
    fifth = distance[:, int(columns * 0.4) : int(columns * 0.6)] > CHROME_CONTRAST
    side = int(columns * 0.4)
    sides = np.stack([far[:, :side].any(axis=1), far[:, columns - side :].any(axis=1)], axis=1)
    return _Rows(
        medians, ink, strong, one_colour, busy, glyph, faint, two_tone, runs, longest, span,
        coverage, fifth.mean(axis=1), sides, far, window,
    )


def _runs_of(mask):
    """``(starts, ends)`` of the runs of set columns in a row's mask."""
    import numpy as np

    edges = np.flatnonzero(mask[1:] != mask[:-1]) + 1
    if mask[0]:
        edges = np.concatenate(([0], edges))
    if mask[-1]:
        edges = np.concatenate((edges, [mask.size]))
    return edges[0::2], edges[1::2]


def _stretches(held, gap: int) -> Iterator[tuple[int, int]]:
    """``(start, end)`` of each run of held rows; runs fewer than ``gap`` apart merge."""
    count = len(held)
    row = 0
    while row < count:
        if not held[row]:
            row += 1
            continue
        start, end = row, row + 1
        while end < count:
            if held[end]:
                end += 1
                continue
            resume = end
            while resume < count and not held[resume]:
                resume += 1
            if resume >= count or resume - end >= gap:
                break
            end = resume
        yield start, end
        row = end


@dataclass
class _Cluster:
    """A stretch of interface rows: glyphs, a pill, a hairline."""

    start: int
    end: int
    content: bool  # lines of text spanning the width
    words: bool  # holds a word: letter-sized runs of ink in close sequence
    text: bool  # lines of words of any width, such as a meme's caption
    line: bool  # a short line of letter-sized glyphs, words or not, but no page dots
    letters: int  # the height of its lines of glyphs, in rows
    left: float  # where its glyphs begin and end, as shares of the width
    right: float
    hole: float  # the widest blank between its glyphs, as a share of the width
    ink: float  # how far its ink stands off the band: a post's text, or greyed-out


def _looks_like_a_bar(rows, start: int, end: int, height: int, edge: ChromeEdge, *,
                      center_empty: bool, pill_allowed: bool, busy_min: int) -> str | None:
    """How the first cluster in from the edge reads as a bar: ``"glyphs"`` (a
    clock and icons, buttons across the width), ``"pill"`` (a home indicator),
    or ``None`` when it reads as content or picture."""
    import numpy as np

    if start > height * edge.first_ink_max or end - start > height * edge.cluster_max:
        return None
    if rows.busy[start:end].sum() >= busy_min:
        return None
    inked = np.flatnonzero(rows.ink[start:end] >= CHROME_INK_FRACTION) + start
    if inked.size == 0:
        return None
    glyphs = rows.glyph[start:end].any()
    if not glyphs:
        # A pill beside the clock keeps every row it crosses from reading as
        # glyphs by its one wide run; several marks with the widest a pill do.
        pilled = (
            rows.two_tone[start:end]
            & (rows.runs[start:end] >= CHROME_BAR_RUNS)
            & (rows.longest[start:end] <= CHROME_PILL_RUN)
        )
        glyphs = bool(pilled.any())
    if not glyphs and not pill_allowed:
        return None  # a progress bar, a shape: not a bar's clock and icons
    if rows.strong[inked].sum() < 0.5 * rows.ink[inked].sum():
        return None  # faint: a gradient crossing the tolerance, not glyphs
    longest = float(np.median(rows.longest[inked]))
    if rows.strong[inked].mean() >= CHROME_DENSE_INK and longest <= CHROME_GLYPH_RUN:
        return None  # a caption or a headline
    if center_empty and rows.centre[inked].mean() > 0.02:
        return None  # phone status bars keep the middle clear; captions do not
    spread = rows.sides[start:end].any(axis=0).all()
    runs = float(np.median(rows.runs[inked]))
    # Three buttons after a blank margin are a navigation bar; three shapes
    # touching the edge are drawer knobs or rounded corners.
    bar_runs = CHROME_BAR_RUNS - 1 if start >= height * CHROME_BAR_MARGIN else CHROME_BAR_RUNS
    if glyphs and spread and runs >= bar_runs:
        return "glyphs"  # a clock on one side, icons on the other; buttons across
    if not pill_allowed:
        return None
    middle = float(np.median(rows.span[inked].mean(axis=1)))
    if runs <= 2 and 0.15 <= longest <= 0.6 and 0.4 <= middle <= 0.6:
        return "pill"
    return None


def _is_content(rows, start: int, end: int) -> bool:
    """Lines of text spanning the width, like a post's words beside its picture."""
    import numpy as np

    glyph = np.flatnonzero(rows.glyph[start:end]) + start
    if glyph.size == 0:
        return False
    lines = (
        (rows.runs[glyph] >= CHROME_TEXT_RUNS)
        & (rows.span[glyph, 1] - rows.span[glyph, 0] >= CHROME_TEXT_EXTENT)
        & (rows.coverage[glyph] >= CHROME_TEXT_COVERAGE)
    )
    return bool(lines.mean() >= 0.5)


def _is_line(rows, start: int, end: int, cluster_rows: int) -> bool:
    """A short line of glyphs on its own, like the last line of a paragraph:
    letters, not an avatar, reaching no farther than most of the width."""
    import numpy as np

    if end - start > cluster_rows or (rows.two_tone | rows.busy)[start:end].any():
        return False
    glyph = np.flatnonzero(rows.glyph[start:end]) + start
    if glyph.size == 0:
        return False
    extent = float((rows.span[glyph, 1] - rows.span[glyph, 0]).max())
    letters = float(np.median(rows.longest[glyph])) <= CHROME_LETTER_WIDTH
    return letters and float(np.median(rows.runs[glyph])) >= 6 and extent <= 0.6


def _is_text(rows, start: int, end: int, cluster_rows: int) -> bool:
    """Lines of letters of any width, like a meme's caption: filling their
    span as words do, which spread icons and a header's arrow and title do
    not, with no more than a couple of rows at big letters' anti-aliased
    rims reading as two-toned."""
    import numpy as np

    if end - start > cluster_rows:
        return False
    if int((rows.two_tone | rows.busy)[start:end].sum()) > max(2, int(0.05 * (end - start))):
        return False
    glyph = np.flatnonzero(rows.glyph[start:end]) + start
    if glyph.size == 0 or float(np.median(rows.longest[glyph])) > CHROME_LETTER_WIDTH:
        return False
    return bool((rows.coverage[glyph] >= CHROME_TEXT_COVERAGE).mean() >= 0.5)


def _is_dots(rows, start: int, end: int, height: int, scale: float) -> bool:
    """A carousel's page dots: a short row of two or more small round marks,
    each no wider than a dot and no farther from the next than a few dots,
    at one pitch, about the centre and within a fifth of the width, one of
    them — the current page — in another colour. Two birds, two stars or two
    boats on a flat sky stand wider than tall or farther apart; a picture's
    balls or coins, and the letters of a caption, are alike in colour.
    ``scale`` is the image pixels per sampled column."""
    import numpy as np

    if end - start > max(4, int(height * CHROME_DOTS_ROWS)):
        return False
    columns = rows.far.shape[1]
    row = start + int(np.argmax(rows.ink[start:end]))  # the dots' equators
    starts, ends = _runs_of(rows.far[row])
    marks = ends - starts >= 2  # not a stray pixel
    starts, ends = starts[marks], ends[marks]
    if starts.size < 2:
        return False
    widest = int((ends - starts).max())
    if widest > max(2, int(columns * CHROME_DOT_WIDTH)):
        return False
    extent = (ends[-1] - starts[0]) / columns
    middle = (ends[-1] + starts[0]) / 2 / columns
    if extent > CHROME_DOTS_WIDTH or not 0.4 <= middle <= 0.6:
        return False
    # Round: about as tall as the widest dot, allowing for the blur at its rim.
    tall = end - start
    if not 0.6 * widest * scale <= tall <= 1.6 * widest * scale + 6:
        return False
    # A JPEG's ringing beside a saturated dot — a mark no more than half a
    # dot wide and faint against the band — is no dot, and does not bring
    # two marks closer together.
    band = rows.medians[row]
    distance = np.abs(rows.pixels[row] - band).max(axis=1)
    strength = np.array([float(distance[first:last].max()) for first, last in zip(starts, ends)])
    solid = ((ends - starts) * 2 > widest) | (strength >= CHROME_CONTRAST)
    starts, ends = starts[solid], ends[solid]
    if starts.size < 2:
        return False
    if int((starts[1:] - ends[:-1]).max()) > 3 * widest:
        return False
    # At one pitch, though a long carousel shrinks its end dots; a word's
    # letters stand at many. Each mark's centre is the middle of its ink,
    # which a column gained or lost at its rim to compression hardly moves.
    centres = np.array([
        first + float(np.average(np.arange(last - first) + 0.5, weights=distance[first:last]))
        for first, last in zip(starts, ends)
    ])
    pitches = np.diff(centres)
    if float(pitches.max() - pitches.min()) > max(2.0, 0.15 * float(np.median(pitches))):
        return False
    # The current page in another colour: each mark's core is its pixel
    # farthest from the band.
    cores = []
    for first, last in zip(starts, ends):
        segment = rows.pixels[row, first:last]
        cores.append(segment[int(np.argmax(np.abs(segment - band).max(axis=1)))])
    cores = np.array(cores, dtype=np.float64)
    if len(cores) == 2:
        return float(np.abs(cores[0] - cores[1]).max()) > CHROME_DOT_MARK
    odd = np.abs(cores - np.median(cores, axis=0)).max(axis=1)
    marked = odd > CHROME_DOT_MARK
    return int(marked.sum()) == 1 and float(odd[~marked].max()) <= CHROME_DOT_MARK / 2


def _avatar_at(rows, start: int, end: int):
    """The side (0 left, 1 right) of an avatar at a margin of the cluster's
    rows and its inner edge, as a share of the width, or ``None``: on the
    rows that keep the cluster's band (a bold name beside the avatar and a
    button can fill half a row and pull its median off it), the widest run
    of ink setting out from that margin — the picture inside a story's ring
    — is never wider than a pill and is an avatar's width on much of the
    height. A button or the end of a name at the other margin may run
    wider."""
    import numpy as np

    columns = rows.far.shape[1]
    margin = 0.15 * columns
    band = np.median(rows.medians[start:end], axis=0)
    on_band = np.abs(rows.medians[start:end] - band).max(axis=1) <= CHROME_TOLERANCE
    sides = np.zeros((2, end - start, 2))  # side, row, (first, last + 1) column
    for offset in np.flatnonzero(on_band):
        starts, ends = _runs_of(rows.far[start + offset])
        for side, near in enumerate((starts <= margin, ends >= columns - margin)):
            if near.any():
                widest = np.flatnonzero(near)[int(np.argmax((ends - starts)[near]))]
                sides[side, offset] = (starts[widest], ends[widest])
    for side, runs in enumerate(sides / columns):
        widths = runs[:, 1] - runs[:, 0]
        if float(widths.max()) <= CHROME_PILL_RUN and (
            int((widths >= 1.5 * CHROME_LETTER_WIDTH).sum()) >= 0.3 * (end - start)
        ):
            inner = runs[:, 1].max() if side == 0 else runs[widths > 0, 0].min()
            return side, float(inner)
    return None


def _beside_an_avatar(rows, start: int, end: int, cluster_rows: int) -> bool:
    """Whether a cluster taller than the cap is an author row: an avatar — a
    round picture at a margin, no wider than a pill, filling much of the
    row's height — beside the words, within half again the cap."""
    if end - start > 1.5 * cluster_rows:
        return False
    return _avatar_at(rows, start, end) is not None


def _is_interface(rows, start: int, end: int, height: int, cluster_rows: int, *,
                  words: bool, dots: bool, scale: float) -> bool:
    """Whether a cluster past the first is interface, not the picture's own
    detail: a head against a white sky, a star, a pair of legs, a boat.
    ``dots`` allows a carousel's page dots (only under the picture)."""
    import numpy as np

    if dots and _is_dots(rows, start, end, height, scale):
        return True
    if _is_content(rows, start, end) or _is_line(rows, start, end, cluster_rows):
        return True
    if not rows.glyph[start:end].any():
        # One shape on the band: a field, a button, a progress bar, a hairline.
        return end - start <= cluster_rows
    if end - start > cluster_rows and not (words and _beside_an_avatar(rows, start, end, cluster_rows)):
        # Taller than any interface element but an author row. A block of
        # words that tall, or words with a stripe through them, is a poster
        # or a sign in the picture.
        return False
    inked = np.flatnonzero((rows.glyph | rows.two_tone)[start:end]) + start
    if end - start < max(4, int(height * CHROME_GLYPH_ROWS)) and not words:
        # Shorter than an icon or a button and no word: a picture's birds or
        # boats, however many. (A story's progress bar, segments filling the
        # row, reads as one shape on the band above.)
        return False
    # Interface rows hold several glyphs — letters, icons, buttons — spread
    # across the row or forming a word, where a picture's object makes one
    # or two runs of ink and its three birds a few runs close together.
    spread = float(rows.span[inked, 1].max() - rows.span[inked, 0].min())
    if spread < CHROME_GLYPH_SPREAD and not words:
        return False
    glyphed = int((rows.runs[inked] >= CHROME_BAR_RUNS - 1).sum())
    return glyphed >= max(3, int(0.15 * inked.size))


def _has_words(rows, start: int, end: int, scale: float) -> bool:
    """Whether the cluster holds a word: on three or more rows, four or more
    letter-sized runs of ink in close sequence. A letter is no wider than the
    line is tall; four birds in a tight flock are."""
    import numpy as np

    columns = rows.far.shape[1]
    letter_gap = max(2, int(columns * CHROME_LETTER_GAP))
    letter_width = max(2, int(columns * CHROME_LETTER_WIDTH))
    worded = 0
    for row in np.flatnonzero(rows.glyph[start:end]) + start:
        starts, ends = _runs_of(rows.far[row])
        if starts.size < 4:
            continue
        letters = (ends - starts <= letter_width) & ((ends - starts) * scale <= 1.25 * (end - start))
        joined = (starts[1:] - ends[:-1] <= letter_gap) & letters[1:] & letters[:-1]
        # Four letters make three joins in a row.
        if joined.any() and np.bincount(np.cumsum(~joined)[joined]).max() >= 3:
            worded += 1
            if worded >= 3:
                return True
    return False


def _cluster(rows, start: int, end: int, height: int, cluster_rows: int, *,
             words: bool, dots: bool, scale: float) -> _Cluster:
    """A cluster of interface rows, read for whether it is the post's words."""
    import numpy as np

    dotted = dots and _is_dots(rows, start, end, height, scale)
    glyph = np.flatnonzero(rows.glyph[start:end]) + start
    marked = glyph if glyph.size else np.arange(start, end)
    # The height of its lines: the stretch of inked rows each row falls in,
    # at the median row, so an i's dot or an accent does not count as a line.
    lengths = np.array([
        last - first for first, last in _stretches(rows.ink[start:end] >= CHROME_INK_FRACTION, 1)
    ])
    letters = int(np.median(np.repeat(lengths, lengths))) if lengths.size else end - start
    inked = np.flatnonzero(rows.far[start:end].any(axis=0))
    hole = float(np.diff(inked).max() - 1) / rows.far.shape[1] if inked.size >= 2 else 0.0
    distance = np.abs(rows.pixels[start:end] - rows.medians[start:end, None, :]).max(axis=2)
    far = rows.far[start:end]
    ink = float(np.percentile(distance[far], 99)) if far.any() else 0.0
    return _Cluster(
        start, end,
        content=_is_content(rows, start, end),
        words=words,
        text=words and _is_text(rows, start, end, cluster_rows) and not dotted,
        line=_is_line(rows, start, end, cluster_rows) and not dotted,
        letters=letters,
        left=float(rows.span[marked, 0].min()),
        right=float(rows.span[marked, 1].max()),
        hole=hole,
        ink=ink,
    )


def _words_beside_avatar(rows, cluster: _Cluster, picture: int, gap: int, band,
                         height: int) -> bool:
    """Whether an author row holds the post's own words: a line under the
    name, beside the avatar, with the picture set in their column (a
    timeline post on X, Threads, Bluesky) — its side lined up with the words
    and running straight down. Over a picture reaching under the avatar,
    even one whose corner is the page's colour (a white sky, a black
    shadow), or with the name alone beside it, the row holds none."""
    import numpy as np

    if not cluster.words:
        return False
    avatar = _avatar_at(rows, cluster.start, cluster.end)
    if avatar is None or avatar[0] != 0:
        return False
    inner = avatar[1]
    columns = rows.far.shape[1]
    off = np.abs(rows.pixels[picture : picture + 8] - band).max(axis=2) > CHROME_TOLERANCE
    held = off.mean(axis=0) >= 0.5
    if not held.any() or int(np.argmax(held)) / columns < inner:
        return False
    side = int(np.argmax(held))
    first = int(np.ceil(inner * columns)) + 2
    beside = rows.far[cluster.start : cluster.end, first:]
    inked = np.flatnonzero(beside.any(axis=0))
    if inked.size == 0 or abs(first + int(inked[0]) - side) > CHROME_COLUMN_ALIGN * columns:
        return False
    # Below its rounded corner, the side of a picture set in a column runs
    # straight down; the edge of a sky or a shadow wanders.
    below = picture + max(8, int(height * 0.02))
    deep = np.abs(rows.pixels[below : below + max(16, int(height * 0.02))] - band).max(axis=2)
    deep = deep > CHROME_TOLERANCE
    if len(deep) >= 8:
        sides = np.where(deep.any(axis=1), np.argmax(deep, axis=1), columns)
        if float((np.abs(sides - side) <= 1).mean()) < 0.6:
            return False
    return len(list(_stretches(beside.any(axis=1), gap))) >= 2


def _one_paragraph(line: _Cluster, below: _Cluster) -> bool:
    """Whether the short ``line`` reads on into the text ``below`` it (the
    next cluster toward the picture): no more than a line's height apart
    and, between two short lines, of about one height — where a header's
    title stands farther off and an action row's icons stand taller. Two of
    a post's paragraphs, words of one size a blank line apart, read on too
    when they share a margin, or a centre between two short lines (text
    across the width is centred on any title); a header's back arrow and
    title, or a title and its icons, stand a wide blank apart. A post's
    paragraphs are written in one ink: the smaller, greyed-out date or time
    an app sets a blank line under its words is no paragraph of theirs."""
    tall, other = line.letters, below.letters
    gap = below.start - line.end
    alike = max(tall, other) <= 1.6 * min(tall, other)
    if below.content:
        if gap <= tall:
            return True
    elif alike and gap <= max(tall, other):
        return True
    if not (line.words and below.words and alike and gap <= 2 * max(tall, other)):
        return False
    if line.ink < CHROME_ONE_INK * below.ink and tall < other:
        return False
    if max(line.hole, below.hole) > 4 * CHROME_LETTER_GAP:
        return False
    if abs(line.left - below.left) <= 0.02:
        return True
    centred = abs((line.left + line.right) - (below.left + below.right)) / 2 <= 0.02
    return centred and not (line.content or below.content)


def _edge_chrome(strip, height: int, edge: ChromeEdge, *, center_empty: bool,
                 certain: bool, width: int) -> int:
    """Rows of interface at the start of ``strip`` (rows ordered from the edge;
    columns sampled from an image ``width`` pixels wide).

    Interface is a stack of short clusters — glyphs, pills, hairlines — on
    flat bands of colour: the status bar, then whatever the app draws between
    it and the picture (a header, the row naming a post's author, a reply
    field, an action row). Every cluster has to read as interface: the first
    as a bar, the rest as several glyphs, a field, or text. A cluster that
    reads as the picture's own detail — a head against a white sky, a star, a
    boat — ends the stack before it, as does the picture itself: rows that
    stop being flat or two-toned, or a band of another colour holding nothing
    but picture. Text next to the picture (a post's words above its photo) is
    kept. When no picture lies within reach, as on a page of text, the cut
    falls at the first change of background after the first cluster, else
    halfway to the next cluster, else a margin past the bar — the last only
    when the image is a screenshot for ``certain`` (by name or metadata) or
    the bar is a home indicator. Returns 0 when the edge does not look like
    an interface bar.
    """
    import numpy as np

    limit = min(len(strip) - 2, int(height * edge.max_fraction))
    if limit < 4:
        return 0
    lookahead = int(height * CHROME_LOOKAHEAD)
    window = strip[: limit + lookahead + 2].astype(np.int16)
    rows = _measure_rows(window)
    if rows.ink[0] > 1.0 - CHROME_EDGE_FLAT:
        return 0  # the picture runs to the edge; anything there is drawn over it
    gap = max(2, int(height * 0.004))
    band_min = max(3, int(height * 0.003))
    busy_min = max(3, int(height * CHROME_BUSY_ROWS))
    cluster_rows = int(height * edge.cluster_max)
    scale = width / window.shape[1]
    count = len(window)

    def steps(row: int) -> bool:
        """The background jumps at ``row`` rather than drifting past the tolerance."""
        before = rows.medians[max(row - 2, 0)]
        after = rows.medians[min(row + 1, count - 1)]
        return float(np.abs(after - before).max()) > CHROME_TOLERANCE

    band = rows.medians[0]
    position = 0  # the band holds from this row on
    clusters: list[_Cluster] = []
    first_kind = None
    separators: list[int] = []  # rows where the background changes, after the first cluster
    switches: list[int] = []  # band changes the walk carried on through
    settled = 0  # clusters found before the latest band change
    picture = None
    tight = None  # a caption set within the walk's gap of its photo
    flat_top = False  # the picture announced itself with a band of its own colour
    ended = False
    while picture is None and not ended:
        ended = True
        held = (np.abs(window[position:] - band).max(axis=2) > CHROME_TOLERANCE).mean(axis=1)
        held = held >= CHROME_INK_FRACTION
        for start, end in _stretches(held, gap):
            start += position
            end += position
            if start > limit:
                break
            look = slice(start, min(end, start + lookahead))
            other = rows.one_colour[start:end] & (
                np.abs(rows.medians[start:end] - band).max(axis=1) > CHROME_TOLERANCE
            )
            # A gradient drifting past the tolerance is no change of background.
            jump = bool(other.any()) and steps(start + int(np.argmax(other)))
            if clusters and jump:
                separators.append(start + int(np.argmax(other)))
            # A band of another colour may begin behind a row or two of
            # compression ringing or stray pixels.
            offset = 0
            while offset < min(gap, len(other) - 1) and not other[offset] and (
                rows.faint[start + offset] or rows.one_colour[start + offset]
            ):
                offset += 1
            lead = len(other) - offset if other[offset:].all() else int(np.argmin(other[offset:]))
            if lead >= band_min:
                change = start + offset
                if rows.busy[look].sum() >= busy_min:
                    picture = change  # the picture's own flat top
                    flat_top = True
                elif jump and rows.glyph[change + lead : look.stop].any():
                    # More interface in a new colour — an app bar under the
                    # status bar, a page under a dark bar. Carry on in it.
                    band = np.median(window[change : change + lead].reshape(-1, 3), axis=0)
                    position = change + lead
                    switches.append(change)
                    settled = len(clusters)
                    ended = False
                # Else a plain band — a desktop, an empty page — or a
                # gradient ends the stack.
                break
            busy = int(rows.busy[look].sum())
            two_tone = int(rows.two_tone[look].sum())
            if busy >= busy_min or two_tone > cluster_rows:
                # The picture starts at its first solid row. Glyphs sitting
                # on it across a gap belong to the interface, and faint rows
                # of compression ringing to the band; glyphs touching the
                # texture are the picture's own outline (a bottle's neck).
                anchor = rows.busy if busy >= busy_min else rows.two_tone
                solid = start + int(np.argmax(anchor[start:end]))
                while solid > start and held[solid - 1 - position] and not rows.faint[solid - 1]:
                    solid -= 1
                skip = 0
                while skip < min(cluster_rows, solid - start) and (
                    rows.glyph[start + skip]
                    or rows.faint[start + skip]
                    or (rows.one_colour[start + skip] and not other[skip])
                ):
                    skip += 1
                picture = start + skip
                # A caption set within the gap of its photo is still the
                # post's words for the keep below to weigh. Words there that
                # read as no caption — a collage's labels spread over its
                # photos, a credit line across the row — are left out of the
                # weighing, so the post's words beyond them are still kept.
                glyphs = np.flatnonzero(rows.glyph[start:picture])
                if clusters and glyphs.size:
                    last = start + int(glyphs[-1]) + 1
                    if _has_words(rows, start, last, scale):
                        caption = _cluster(
                            rows, start, last, height, cluster_rows,
                            words=True, dots=edge.dots, scale=scale,
                        )
                        if caption.content or caption.text or caption.line:
                            tight = caption
                            clusters.append(tight)
                break
            if not clusters:
                inked = rows.glyph[start:end] | rows.two_tone[start:end]
                if not inked.any():
                    continue  # a hairline before the first cluster
                first_kind = _looks_like_a_bar(
                    rows, start, end, height, edge,
                    center_empty=center_empty, pill_allowed=edge.pill, busy_min=busy_min,
                )
                if first_kind is None:
                    return 0
                later = other.copy()
                later[: int(np.argmax(inked)) + 1] = False
                if later.any() and steps(start + int(np.argmax(later))):
                    separators.append(start + int(np.argmax(later)))
            words = _has_words(rows, start, end, scale)
            if clusters and not _is_interface(
                rows, start, end, height, cluster_rows, words=words, dots=edge.dots, scale=scale
            ):
                # The picture's own detail ends the stack: cut at the change
                # of background before it when there is one, else at it.
                between = [row for row in separators if clusters[-1].end <= row < start]
                picture = between[0] if between else start
                break
            clusters.append(_cluster(
                rows, start, end, height, cluster_rows, words=words, dots=edge.dots, scale=scale,
            ))
    if not clusters:
        return 0
    # The picture rose out of the band the walk switched into. Unless a word
    # of interface stood in that band, the band was the picture's own flat
    # top — a sky with birds, a meme's caption band — and the switch its
    # edge. A caption set tight against the photo is the picture's own.
    if picture is not None and switches and not flat_top and not any(
        cluster.content or cluster.words for cluster in clusters[settled:] if cluster is not tight
    ):
        picture = switches[-1]
        if settled:
            del clusters[settled:]  # the sky's own birds, not words to keep

    first = clusters[0]
    if picture is not None:
        cut = picture
        index = len(clusters) - 1
        kept = None
        authored = False
        # The post's words beside its picture are kept: lines spanning the
        # width and lines of words reading on as one paragraph (a meme's
        # caption, a short tweet, a poem's lines) — though not a header's
        # title or an action row a line apart from them, and not a lone line
        # right after the bar, which goes with it. A short line of marks
        # right next to the picture is its own, words or not: boats on its
        # white fog, birds in its sky. A timeline post's first line beside
        # the avatar keeps the author row with it, as a cut between would
        # cut the avatar.
        while index >= 1:
            cluster = clusters[index]
            if cluster.content or (
                cluster.text and (kept is None or _one_paragraph(cluster, kept))
            ) or (cluster.line and kept is None):
                kept = cluster
                index -= 1
                continue
            if _words_beside_avatar(rows, cluster, picture, gap, band, height):
                kept = cluster
                index -= 1
                authored = True
            break
        if (
            kept is not None and index == 0 and len(clusters) == 2
            and not kept.content and not authored
        ):
            kept = None
        if kept is not None:
            previous = clusters[index]
            between = [row for row in separators if previous.end <= row < kept.start]
            cut = between[0] if between else (previous.end + kept.start) // 2
    elif separators:
        cut = separators[0]
    elif len(clusters) > 1:
        cut = (first.end + clusters[1].start) // 2  # halfway to the first text below
    elif certain or first_kind == "pill":
        cut = min(first.end + first.start, limit)  # a margin like the one above the glyphs
    else:
        return 0  # one row of glyphs alone does not make a photo a screenshot
    if cut < height * CHROME_MIN_FRACTION or cut > limit:
        return 0
    return int(cut)


def detect_screenshot_chrome(image, *, certain: bool = True) -> tuple[int, int]:
    """Rows of interface bar ``(top, bottom)`` on an upright screenshot image.

    ``certain`` says the image is known to be a screenshot, by name or
    metadata, rather than merely shaped like one; a lone bar with nothing
    in reach below it is then enough for a cut.
    """
    import numpy as np

    rgb = np.asarray(_flattened(image))
    height, width = rgb.shape[:2]
    if height < 64 or width < 64:
        return 0, 0
    sample = rgb[:, :: max(1, width // CHROME_SAMPLE_COLUMNS)]
    portrait = height >= width * 1.3
    top = _edge_chrome(
        sample, height, CHROME_TOP, center_empty=portrait, certain=certain, width=width
    )
    bottom = _edge_chrome(
        sample[::-1], height, CHROME_BOTTOM, center_empty=False, certain=certain, width=width
    )
    return top, bottom


def _box_for(upright, *, chrome: tuple[int, int] = (0, 0), speck: bool = False) -> CropBox | None:
    """Combine interface bars with black bars inside them into one crop.

    Where the walk took interface at an edge it took the status bar, where a
    privacy dot sits, so a speck past it is the picture's own.
    """
    width, height = upright.size
    chrome_top, chrome_bottom = chrome
    luma = _luma(upright)[chrome_top : height - chrome_bottom]
    specks = (speck and not chrome_top, speck and not chrome_bottom)
    bars = detect_borders_in_array(luma, speck=specks) or (0, 0, 0, 0)
    top = chrome_top + bars[0]
    bottom = chrome_bottom + bars[1]
    left, right = bars[2], bars[3]
    if not (top or bottom or left or right):
        return None
    if height - top - bottom < height * MIN_CONTENT_FRACTION:
        return None
    edges = tuple(
        name for name, rows in (("top", chrome_top), ("bottom", chrome_bottom)) if rows
    )
    box = CropBox(
        left=left,
        top=top,
        right=width - right,
        bottom=height - bottom,
        width=width,
        height=height,
        chrome=edges,
    )
    return box if box.is_valid() else None


def detect_black_borders(path: str | Path) -> CropBox | None:
    """Suggest the crop for ``path``: black bars, plus interface bars on screenshots."""
    from PIL import Image, ImageOps

    _register_heif()
    with Image.open(path) as source:
        full_size = source.size
        evidence = screenshot_evidence(path, source)
        if evidence is None:
            source.draft("RGB", (SCREEN_MAX_SIDE, SCREEN_MAX_SIDE))
        reduced = source.size != full_size
        screened = ImageOps.exif_transpose(source)
        screened.load()
    speck = evidence is not None  # a phone's privacy dot sits in a screenshot's bar
    if evidence is not None:
        chrome = detect_screenshot_chrome(screened, certain=evidence != "shape")
        if any(chrome):
            return _box_for(screened, chrome=chrome, speck=True)
    if detect_borders_in_array(_luma(screened), speck=speck) is None:
        return None

    upright = screened
    if reduced:
        # The quick pass found bars; measure them exactly at full resolution.
        with Image.open(path) as source:
            upright = ImageOps.exif_transpose(source)
            upright.load()
    return _box_for(upright, speck=speck)


def analyze_borders(
    records: list[FileRecord],
    *,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Fill ``border_crop`` for every croppable record lacking a current verdict.

    Records that already carry this signature's verdict (from the hash cache,
    including a user's dismissal) are skipped. Returns per-file error strings.
    """
    for record in records:
        if (
            record.border_signature in LEGACY_BORDER_SIGNATURES
            and record.border_crop == BORDER_DISMISSED
        ):
            record.border_signature = BORDER_SIGNATURE  # "no crop" still stands
    pending = [
        record
        for record in records
        if is_croppable(record) and record.border_signature != BORDER_SIGNATURE
    ]
    if not pending:
        return []

    def analyze(record: FileRecord) -> str | None:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        try:
            box = detect_black_borders(record.path)
        except Exception as exc:
            return f"border check failed for {record.path}: {exc}"
        record.border_crop = box.encode() if box else ""
        record.border_signature = BORDER_SIGNATURE
        return None

    results = map_parallel(
        analyze,
        pending,
        workers=max(1, min(workers, len(pending))),
        progress=progress,
        cancelled=cancelled,
    )
    return [error for error in results if error]


@dataclass
class CropOutcome:
    path: str
    original_in_trash: str | None
    size: int
    width: int
    height: int
    stat: os.stat_result


def _save_kwargs(source, upright, image_format: str) -> dict:
    """Save options that keep the file's format, color profile, and metadata."""
    kwargs: dict = {}
    icc = source.info.get("icc_profile")
    if icc:
        kwargs["icc_profile"] = icc
    dpi = source.info.get("dpi")
    if dpi:
        kwargs["dpi"] = dpi
    try:
        exif = upright.getexif()
        if exif:
            kwargs["exif"] = exif.tobytes()
    except Exception:
        pass
    if image_format == "JPEG":
        from PIL import JpegImagePlugin

        kwargs["quality"] = JPEG_SAVE_QUALITY
        sampling = JpegImagePlugin.get_sampling(source)
        if sampling != -1:
            kwargs["subsampling"] = sampling
        if source.info.get("progressive") or source.info.get("progression"):
            kwargs["progressive"] = True
    elif image_format in ("WEBP", "HEIF", "AVIF"):
        kwargs["quality"] = LOSSY_SAVE_QUALITY
    elif image_format == "TIFF":
        compression = source.info.get("compression")
        if compression and compression != "raw":
            kwargs["compression"] = compression
        kwargs.pop("exif", None)
    elif image_format == "BMP":
        kwargs.pop("exif", None)
        kwargs.pop("icc_profile", None)
    return kwargs


def write_cropped_copy(path: Path, box: CropBox, destination: Path) -> tuple[int, int]:
    """Write ``path`` cropped to ``box`` at ``destination``; return its size."""
    from PIL import Image, ImageOps

    _register_heif()
    with Image.open(path) as source:
        image_format = source.format or ""
        upright = ImageOps.exif_transpose(source)
        upright.load()
        if upright.size != (box.width, box.height):
            raise ValueError(
                "image dimensions changed since the scan "
                f"({box.width}×{box.height} -> {upright.size[0]}×{upright.size[1]})"
            )
        cropped = upright.crop((box.left, box.top, box.right, box.bottom))
        kwargs = _save_kwargs(source, upright, image_format)
    if not image_format:
        raise ValueError("unrecognized image format")
    cropped.save(destination, format=image_format, **kwargs)
    with Image.open(destination) as check:
        written = ImageOps.exif_transpose(check).size
    if written != (box.crop_width, box.crop_height):
        raise ValueError(
            f"cropped file has unexpected dimensions {written[0]}×{written[1]}"
        )
    return written


# --- locked files and file metadata ---------------------------------------
#
# Finder's "Locked" checkbox sets the user-immutable flag (uchg). A locked file
# can be neither moved to the Trash nor replaced, and a locked folder accepts
# no new entries, so a crop lifts the owner's lock for the moment it needs and
# puts it back. The system-immutable flags (schg) need an administrator and
# are refused with a clear reason instead.

USER_LOCK_FLAGS = getattr(stat, "UF_IMMUTABLE", 0) | getattr(stat, "UF_APPEND", 0)
SYSTEM_LOCK_FLAGS = getattr(stat, "SF_IMMUTABLE", 0) | getattr(stat, "SF_APPEND", 0)

# Extended attributes that describe the old file rather than the picture:
# kernel and privacy bookkeeping, iCloud's per-item identity, compression, and
# a resource fork (which may hold a custom icon of the uncropped image).
_SKIPPED_XATTR_PREFIXES = (
    "com.apple.provenance",
    "com.apple.macl",
    "com.apple.rootless",
    "com.apple.decmpfs",
    "com.apple.ResourceFork",
    "com.apple.fileprovider.",
)
_XATTR_NOFOLLOW = 0x0001


def file_flags(path: str | Path) -> int:
    """BSD file flags of ``path`` itself (0 where the platform has none)."""
    return int(getattr(os.lstat(path), "st_flags", 0))


def set_file_flags(path: str | Path, flags: int) -> None:
    if hasattr(os, "chflags"):
        os.chflags(path, flags, follow_symlinks=False)


def _refuse_system_lock(path: Path, flags: int) -> None:
    if flags & SYSTEM_LOCK_FLAGS:
        raise PermissionError(
            errno.EPERM,
            "locked by the system (schg); only an administrator can unlock it",
            str(path),
        )


def _relock(path: Path, lock_bits: int) -> None:
    """Put back a lifted lock; best effort, since the edit itself succeeded."""
    if not lock_bits:
        return
    with contextlib.suppress(OSError):
        set_file_flags(path, file_flags(path) | lock_bits)


@contextlib.contextmanager
def editable_folder(folder: Path) -> Iterator[None]:
    """Let entries be added to and renamed in ``folder`` for the duration.

    Lifts a Finder lock and adds the owner's write bit when either is missing,
    then restores both exactly as they were.
    """
    st = os.lstat(folder)
    flags = int(getattr(st, "st_flags", 0))
    _refuse_system_lock(folder, flags)
    lock_bits = flags & USER_LOCK_FLAGS
    mode = stat.S_IMODE(st.st_mode)
    add_write = not mode & stat.S_IWUSR
    if lock_bits:
        set_file_flags(folder, flags & ~lock_bits)
    try:
        if add_write:
            os.chmod(folder, mode | stat.S_IWUSR)
        yield
    finally:
        if add_write:
            with contextlib.suppress(OSError):
                os.chmod(folder, mode)
        _relock(folder, lock_bits)


def _unlock_file(path: Path) -> int:
    """Lift a Finder lock from ``path``; return the lock bits that were lifted."""
    flags = file_flags(path)
    _refuse_system_lock(path, flags)
    lock_bits = flags & USER_LOCK_FLAGS
    if lock_bits:
        set_file_flags(path, flags & ~lock_bits)
    return lock_bits


@functools.cache
def _darwin_libc():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint32, ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint32, ctypes.c_int,
    ]
    libc.setxattr.restype = ctypes.c_int
    return libc


def _list_xattrs(path: Path) -> list[str]:
    if hasattr(os, "listxattr"):
        return os.listxattr(path, follow_symlinks=False)
    if sys.platform != "darwin":
        return []
    libc = _darwin_libc()
    raw = os.fsencode(path)
    size = libc.listxattr(raw, None, 0, _XATTR_NOFOLLOW)
    if size <= 0:
        return []
    buffer = ctypes.create_string_buffer(size)
    size = libc.listxattr(raw, buffer, size, _XATTR_NOFOLLOW)
    if size <= 0:
        return []
    return [os.fsdecode(name) for name in buffer.raw[:size].split(b"\0") if name]


def _get_xattr(path: Path, name: str) -> bytes:
    if hasattr(os, "getxattr"):
        return os.getxattr(path, name, follow_symlinks=False)
    libc = _darwin_libc()
    raw, key = os.fsencode(path), os.fsencode(name)
    size = libc.getxattr(raw, key, None, 0, 0, _XATTR_NOFOLLOW)
    if size < 0:
        raise OSError(ctypes.get_errno(), "getxattr failed", str(path))
    buffer = ctypes.create_string_buffer(max(size, 1))
    size = libc.getxattr(raw, key, buffer, size, 0, _XATTR_NOFOLLOW)
    if size < 0:
        raise OSError(ctypes.get_errno(), "getxattr failed", str(path))
    return buffer.raw[:size]


def _set_xattr(path: Path, name: str, value: bytes) -> None:
    if hasattr(os, "setxattr"):
        os.setxattr(path, name, value, follow_symlinks=False)
        return
    libc = _darwin_libc()
    if libc.setxattr(os.fsencode(path), os.fsencode(name), value, len(value), 0,
                     _XATTR_NOFOLLOW) != 0:
        raise OSError(ctypes.get_errno(), "setxattr failed", str(path))


def copy_xattrs(source: Path, destination: Path) -> list[str]:
    """Copy ``source``'s extended attributes onto ``destination``.

    Finder tags and colour labels, "Where from" download URLs, and quarantine
    state survive the rewrite. Bookkeeping attributes are skipped, and one
    attribute the system refuses never blocks the rest. Returns the names copied.
    """
    copied: list[str] = []
    try:
        names = _list_xattrs(source)
    except OSError:
        return copied
    for name in names:
        if name.startswith(_SKIPPED_XATTR_PREFIXES):
            continue
        try:
            _set_xattr(destination, name, _get_xattr(source, name))
        except OSError:
            continue
        copied.append(name)
    return copied


def crop_file_in_place(
    record: FileRecord,
    box: CropBox,
    *,
    send_to_trash: Callable[[Path], Path | None],
) -> CropOutcome:
    """Replace ``record``'s file with its cropped version; original to Trash.

    The cropped image is written and verified beside the original first, the
    original is moved to the system Trash, and the cropped file then takes
    its place. The original's permissions, timestamps, extended attributes,
    and file flags carry over, so date-sorted folders keep their order, hidden
    files stay hidden, and a Finder-locked file comes back locked. Any failure
    before the swap leaves the original untouched; a failure during the swap
    moves it back.
    """
    original = Path(record.path)
    before = os.lstat(original)
    flags = int(getattr(before, "st_flags", 0))
    _refuse_system_lock(original, flags)
    with editable_folder(original.parent):
        temp = original.with_name(
            f".{original.stem}.dedupe-crop-{uuid.uuid4().hex[:8]}{original.suffix}"
        )
        try:
            width, height = write_cropped_copy(original, box, temp)
            copy_xattrs(original, temp)
            os.chmod(temp, stat.S_IMODE(before.st_mode))
            os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

        lock_bits = _unlock_file(original)
        try:
            trashed = send_to_trash(original)
        except BaseException:
            temp.unlink(missing_ok=True)
            _relock(original, lock_bits)
            raise
        try:
            os.replace(temp, original)
        except BaseException:
            if trashed is not None and Path(trashed).is_file() and not original.exists():
                os.replace(trashed, original)
                _relock(original, lock_bits)
            temp.unlink(missing_ok=True)
            raise
        # Flags go on last: a locked file could not have been renamed into place.
        if flags:
            with contextlib.suppress(OSError):
                set_file_flags(original, flags)
    after = os.lstat(original)
    return CropOutcome(
        path=str(original),
        original_in_trash=str(trashed) if trashed is not None else None,
        size=int(after.st_size),
        width=width,
        height=height,
        stat=after,
    )


def restore_original_in_place(
    path: str | Path,
    original_in_trash: str | Path,
    *,
    send_to_trash: Callable[[Path], Path | None],
) -> os.stat_result:
    """Undo a crop: the cropped file goes to the Trash, the original comes back.

    The cropped file's current flags — including a Finder lock, whether it
    carried over from the original or was set after the crop — are applied to
    the restored original. Returns the restored file's stat.
    """
    current = Path(path)
    backup = Path(original_in_trash)
    flags = file_flags(current)
    _refuse_system_lock(current, flags)
    with editable_folder(current.parent):
        lock_bits = _unlock_file(current)
        try:
            cropped_copy = send_to_trash(current)
        except BaseException:
            _relock(current, lock_bits)
            raise
        try:
            # The Trash copy was unlocked on its way out; clear any lock set since.
            backup_lock = file_flags(backup) & USER_LOCK_FLAGS
            if backup_lock:
                set_file_flags(backup, file_flags(backup) & ~backup_lock)
            shutil.move(str(backup), str(current))
        except BaseException:
            if cropped_copy is not None and not current.exists():
                shutil.move(str(cropped_copy), str(current))
                _relock(current, lock_bits)
            raise
        if flags:
            with contextlib.suppress(OSError):
                set_file_flags(current, flags)
    return os.lstat(current)
