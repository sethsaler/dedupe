"""Border detection and in-place cropping for images and videos.

A scan with border detection enabled looks along each edge of every
croppable image for solid black bars — letterboxing, pillarboxing, scanner
margins, screenshots of videos — and suggests the crop that removes them.
Images get the same for a flat frame of any other colour: a story set on
white, a collage's outer border, a gray or coloured mat.
MP4-family videos get the same treatment on sampled frames: a bar counts
when most sampled frames carry it, and a human guard pulls every suggested
crop back wherever it would cut a detected person.
Images that look like screenshots are also checked for interface at the top
and bottom edges: a phone's status bar (clock, battery, notification icons),
a home indicator or navigation bar, a desktop menu bar or taskbar, and the
rows an app stacks between those and the picture — its header, the row naming
a post's author, a reply field, an action row — along with a gallery's filmstrip
of other photos under the picture and a media viewer's counter or page dots
alone on its black bands.
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
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import FileRecord, MediaType
from .parallel import map_parallel

# Bump when the detection rules change so cached verdicts are recomputed.
# A user's dismissal under an earlier signature still stands.
BORDER_SIGNATURE = "black-borders-v9"
LEGACY_BORDER_SIGNATURES = frozenset(
    {"black-borders-v1", "black-borders-v2", "black-borders-v3", "black-borders-v4",
     "black-borders-v5", "black-borders-v6", "black-borders-v7", "black-borders-v8"}
)
# Cached verdict for a file the user dismissed ("No crop needed").
BORDER_DISMISSED = "dismissed"

# --- crop aggressiveness levels ----------------------------------------------
# Every measurement computes one suggested box per level in a single pass:
# conservative keeps only what the strict detectors (black bars, flat colour
# bars, screenshot interface) prove, balanced adds the guarded aggressive
# pass over black bars (captions, encoder noise), and aggressive loosens
# that pass further — darker-gray and thinner bars, colour bars holding a
# mark — but only where the bar holds nothing and stops at a ruled line,
# still behind the human guard. The
# balanced box keeps living in ``border_crop`` under BORDER_SIGNATURE, so
# verdicts cached before levels existed stay valid; the other two boxes are
# stamped with their own signature, bumped when their rules change.
CROP_LEVELS = ("conservative", "balanced", "aggressive")
DEFAULT_CROP_LEVEL = "balanced"
BORDER_LEVELS_SIGNATURE = "crop-levels-v3"


def validate_crop_level(level: str | None) -> str:
    """``level`` as a known crop aggressiveness level (default balanced)."""
    if level is None:
        return DEFAULT_CROP_LEVEL
    if level not in CROP_LEVELS:
        raise ValueError(
            f"unknown crop aggressiveness {level!r}; choose one of: {', '.join(CROP_LEVELS)}"
        )
    return level

# Formats Pillow can both read and write back without changing the file type.
# RAW and GIF (animation) are deliberately excluded.
CROPPABLE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
}
# Video containers ffmpeg can re-encode back into themselves while preserving
# or copying audio tracks.
VIDEO_CROPPABLE_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".avi",
    ".mts",
    ".ts",
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
# A JPEG codes pixels in blocks this many rows tall; an edge inside one rings
# into the rows around it.
JPEG_BLOCK = 8
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

# --- videos -------------------------------------------------------------------
# Frame positions (shares of the duration) sampled for bars. The ends are
# avoided: fades and black intros/outros live there. An edge is suggested at
# the median thickness of the frames that carry it, when a majority do.
VIDEO_SAMPLE_FRACTIONS = (0.08, 0.27, 0.5, 0.73, 0.92)
# Frames carrying a picture needed before any bar is trusted.
VIDEO_MIN_USABLE_FRAMES = 3
# A frame with less than this share of pixels above the black-bar level is a
# fade or a black frame: it says nothing about bars and neither vetoes nor
# supports a crop.
VIDEO_BLACK_FRAME_FRACTION = 0.001
# Cropping a video means re-encoding it; there is no lossless crop. These
# CRF values keep the generation loss small (the original stays in the Trash).
VIDEO_H264_CRF = 18
VIDEO_HEVC_CRF = 22

# --- aggressive bars ----------------------------------------------------------
# The strict detector above is deliberately timid: a bar must be flat to
# 99.5% and end in a hard edge, so caption text burned into a bar, encoder
# noise in the blacks, or a dark-gray bar all defeat it — and real libraries
# are full of those. The aggressive pass measures the same edges with loose
# rules: a line belongs to the bar while most of it sits near the bar's
# level, sparse ink (captions, subtitles, watermarks) inside the bar is
# cropped with it, and no hard edge is required. What keeps this safe is not
# timidity but the human guard below: an aggressive edge never removes a
# detected face or body, and where the guard cannot run (no OpenCV), the
# aggressive pass does not contribute at all.
AGGRESSIVE_DARK_LUMA = 48
AGGRESSIVE_TOLERANCE = 20
# Share of a line that must sit near the bar's level; the rest may be ink.
AGGRESSIVE_LINE_FRACTION = 0.85
# A line at most this near the level is clearly picture and ends the bar;
# lines between the two (a big watermark's rows) are tolerated gaps.
AGGRESSIVE_PICTURE_FRACTION = 0.5
# Consecutive gap lines a bar may jump, as a share of the dimension.
AGGRESSIVE_GAP_FRACTION = 0.02
# The aggressive *level* runs this pass looser still: bars may be a clear
# dark gray, noisier, hold heavier ink, and be jumped across wider gaps,
# and thinner bars surface. The human guard arbitrates it exactly as it
# does the balanced pass; where the guard cannot run, neither contributes.
AGGRESSIVE_LEVEL_DARK_LUMA = 64
AGGRESSIVE_LEVEL_TOLERANCE = 28
AGGRESSIVE_LEVEL_LINE_FRACTION = 0.70
AGGRESSIVE_LEVEL_PICTURE_FRACTION = 0.35
AGGRESSIVE_LEVEL_GAP_FRACTION = 0.04
AGGRESSIVE_LEVEL_MIN_FRACTION = 0.002
# Rules that loose also take a dark beam, a shadowed wall or a night sky for
# a bar: most of each line sits near one dark level there too. What a bar has
# and a picture does not is nothing in it and a ruled line where it stops. The
# aggressive level's bar is therefore read again in blocks, which average
# noise and ringing away: at least AGGRESSIVE_LEVEL_FLAT_BLOCKS of them sit
# within BAR_BLOCK_TOLERANCE of the bar's level, and where the bar ends the
# picture steps off it by BAR_STEP_CONTRAST along at least
# AGGRESSIVE_LEVEL_STRAIGHT_EDGE of the line.
BAR_BLOCK_TOLERANCE = 6
BAR_BLOCKS_ACROSS = 128
BAR_STEP_CONTRAST = 16
AGGRESSIVE_LEVEL_FLAT_BLOCKS = 0.9
AGGRESSIVE_LEVEL_STRAIGHT_EDGE = 0.6

# --- solid-colour bars --------------------------------------------------------
# A picture set on white, gray or any other flat colour — a letterboxed story,
# a collage's outer frame, a scan's margin — is framed just as one on black.
# A colour bar is measured like a strict black bar, on all three channels, but
# must end harder: white walls, blown skies and paper are common in pictures,
# so at least this share of a line within BAR_EDGE_LINES past the bar is off
# its colour. A seamless backdrop behind a head, or a page's margin above a
# line of text, changes less of the line than that and is left alone.
COLOUR_BAR_EDGE_FRACTION = 0.5
# Lines of a colour bar that an edge's ringing may leave short of flat: an
# edge inside a JPEG block rings through the rest of that block.
COLOUR_BAR_RINGING_LINES = 7
# A frame's colour stays outside the picture. Where more than this share of
# the rows or columns kept are that colour from end to end, with content on
# both sides of them — the blank lines between a page's paragraphs — it is
# the ground the content sits on, a page's own margin, and trimming it is
# not a crop. A collage's dividers are a line or two and stay well under it;
# a band still standing at the kept part's own edge (the other side of the
# frame, where a mark or a pale picture kept it from being cut) is not
# between anything and does not count.
COLOUR_GROUND_FRACTION = 0.05
# Times the edges are measured again on what the bars found so far leave.
COLOUR_BAR_PASSES = 3
# The aggressive level also takes a colour bar that holds a little ink — a
# watermark, a mute icon, a counter — read loosely like a captioned black bar
# and held to the same two checks: nothing in its blocks, a ruled line at its
# end. It stays out of the balanced level because a caption on white is more
# often the point of the picture (a meme, a quoted post) than one on black.
COLOUR_LOOSE_TOLERANCE = 20


@dataclass(frozen=True)
class _LooseRules:
    """Thresholds for one run of the loose (aggressive) bar pass."""

    dark_luma: int
    tolerance: int
    line_fraction: float
    picture_fraction: float
    gap_fraction: float
    min_fraction: float
    # Share of the bar's blocks at its level, and of the line past its end
    # that steps off it; ``None`` skips the check.
    flat_blocks: float | None = None
    straight_edge: float | None = None
    # When True, straight_edge is only enforced for bars brighter than DARK_LUMA
    # (luma 29..48 dark skies / shadowed ceilings), preserving pure-black
    # night-sky gradients that brighten into the scene below.
    ruled_above_dark_only: bool = False


_BALANCED_LOOSE = _LooseRules(
    dark_luma=AGGRESSIVE_DARK_LUMA,
    tolerance=AGGRESSIVE_TOLERANCE,
    line_fraction=AGGRESSIVE_LINE_FRACTION,
    picture_fraction=AGGRESSIVE_PICTURE_FRACTION,
    gap_fraction=AGGRESSIVE_GAP_FRACTION,
    min_fraction=MIN_BORDER_FRACTION,
    flat_blocks=AGGRESSIVE_LEVEL_FLAT_BLOCKS,
    straight_edge=AGGRESSIVE_LEVEL_STRAIGHT_EDGE,
    ruled_above_dark_only=True,
)
_AGGRESSIVE_LOOSE = _LooseRules(
    dark_luma=AGGRESSIVE_LEVEL_DARK_LUMA,
    tolerance=AGGRESSIVE_LEVEL_TOLERANCE,
    line_fraction=AGGRESSIVE_LEVEL_LINE_FRACTION,
    picture_fraction=AGGRESSIVE_LEVEL_PICTURE_FRACTION,
    gap_fraction=AGGRESSIVE_LEVEL_GAP_FRACTION,
    min_fraction=AGGRESSIVE_LEVEL_MIN_FRACTION,
    flat_blocks=AGGRESSIVE_LEVEL_FLAT_BLOCKS,
    straight_edge=AGGRESSIVE_LEVEL_STRAIGHT_EDGE,
)
_COLOUR_LOOSE = _LooseRules(
    dark_luma=DARK_LUMA,
    tolerance=COLOUR_LOOSE_TOLERANCE,
    line_fraction=AGGRESSIVE_LINE_FRACTION,
    picture_fraction=AGGRESSIVE_PICTURE_FRACTION,
    gap_fraction=AGGRESSIVE_GAP_FRACTION,
    min_fraction=MIN_BORDER_FRACTION,
    flat_blocks=AGGRESSIVE_LEVEL_FLAT_BLOCKS,
    straight_edge=AGGRESSIVE_LEVEL_STRAIGHT_EDGE,
)
# A video edge is suggested when more than this share of pictured frames
# carry it (mixed boxed/full-frame clips: substantial full-frame sections
# such as 40% are never permanently cropped).
VIDEO_MAJORITY = 0.7
# Guard frames are shrunk to this side before detection; the detectors
# downscale internally anyway.
GUARD_MAX_SIDE = 960
# Face boxes grow by these shares of their size before guarding, so hair
# above a forehead and shoulders beside a face are kept too.
GUARD_FACE_EXPAND = (0.35, 0.6, 0.35, 0.35)  # left, top, right, bottom
GUARD_BODY_EXPAND = 0.05
GUARD_PERSON_CONFIDENCE = 0.3

JPEG_SAVE_QUALITY = 95
LOSSY_SAVE_QUALITY = 92

# --- screenshot interface ---------------------------------------------------
# Only images with some sign of being a screenshot are checked for interface
# bars: a screenshot-style name, the "Screenshot" tag iOS writes, the macOS
# screen-capture flag, or a screen-sized frame with no camera behind it.
SCREENSHOT_NAME_PATTERN = re.compile(
    r"screen[ _-]?shot|screen[ _-]?capture|screencap|bildschirmfoto"
    r"|captura de pantalla|capture d.?[ée]cran|clean[ _-]?shot|shottr",
    re.IGNORECASE,
)
IOS_SCREENSHOT_NAME_PATTERN = re.compile(r"^IMG_[A-Za-z0-9]+\.png$", re.IGNORECASE)
PHONE_SCREEN_WIDTHS = frozenset(
    {640, 720, 750, 828, 840, 960, 1008, 1080, 1125, 1170, 1179, 1206, 1220, 1240,
     1242, 1260, 1284, 1290, 1320, 1344, 1440}
)
IPAD_SHORT_SIDES = frozenset({768, 1488, 1536, 1620, 1640, 1668, 2048, 2064})
DESKTOP_SCREEN_WIDTHS = frozenset(
    {1280, 1366, 1440, 1470, 1512, 1536, 1600, 1680, 1710, 1728, 1920, 2048, 2560,
     2880, 2940, 3024, 3072, 3360, 3440, 3456, 3840, 5120}
)
SCREEN_CAPTURE_XATTR = "com.apple.metadata:kMDItemIsScreenCapture"
CHROME_ALL_EDGES = ("top", "bottom", "left", "right")
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
# A gallery's filmstrip — thumbnails of the photos around the one shown, in a
# row under it — reaches at most this share of the height in from where it
# starts to the picture, and holds at least CHROME_STRIP_TILES thumbnails,
# each within CHROME_STRIP_TILE of the width. Its band may darken toward the
# edge, so it is followed row by row within CHROME_STRIP_DRIFT of the row before.
CHROME_STRIP_ROWS = 0.1
CHROME_STRIP_TILES = 4
CHROME_STRIP_TILE = (0.02, 0.15)
CHROME_STRIP_DRIFT = 2 * CHROME_TOLERANCE
# A media viewer's counter ("3 / 20") is no wider than this share of the width.
CHROME_COUNTER_WIDTH = 0.25
CHROME_MIN_FRACTION = 0.012
CHROME_SAMPLE_COLUMNS = 720


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
    # A gallery's filmstrip of other photos sits under the picture too.
    strip: bool


# Status bar or menu bar, then an app's header and the row naming a post's author.
CHROME_TOP = ChromeEdge(
    max_fraction=0.22, first_ink_max=0.07, cluster_max=0.045, pill=False, dots=False,
    strip=False,
)
# Home indicator, navigation buttons, tab bar, taskbar, reply field, action
# row, a gallery's filmstrip.
CHROME_BOTTOM = ChromeEdge(
    max_fraction=0.30, first_ink_max=0.06, cluster_max=0.08, pill=True, dots=True,
    strip=True,
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
    # Edges ("top", "bottom", "left", "right") whose crop removes screenshot
    # interface rather than black bars.
    chrome: tuple[str, ...] = ()
    # Structured detector provenance tags (e.g. "black_bar", "colour_bar",
    # "blurred_bar", "chrome", "human_guarded"), excluded from value equality.
    provenance: tuple[str, ...] = field(default=(), compare=False)

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
        if self.chrome or self.provenance:
            parts.append("+".join(self.chrome))
        if self.provenance:
            parts.append("+".join(self.provenance))
        return ",".join(parts)

    @classmethod
    def decode(cls, value: str | None) -> CropBox | None:
        if not value or value == BORDER_DISMISSED:
            return None
        parts = value.split(",")
        chrome: tuple[str, ...] = ()
        provenance: tuple[str, ...] = ()
        if len(parts) == 8:
            provenance = tuple(tag for tag in parts.pop().split("+") if tag)
            chrome = tuple(edge for edge in parts.pop().split("+") if edge in CHROME_ALL_EDGES)
        elif len(parts) == 7:
            chrome = tuple(edge for edge in parts.pop().split("+") if edge in CHROME_ALL_EDGES)
        try:
            left, top, right, bottom, width, height = (int(part) for part in parts)
        except ValueError:
            return None
        box = cls(left, top, right, bottom, width, height, chrome, provenance)
        return box if box.is_valid() else None

    def is_valid(self) -> bool:
        return (
            0 <= self.left < self.right <= self.width
            and 0 <= self.top < self.bottom <= self.height
            and (self.left, self.top, self.right, self.bottom) != (0, 0, self.width, self.height)
        )

    def to_dict(self) -> dict:
        kind = next((p for p in self.provenance if p != "human_guarded"), None)
        if kind is None:
            kind = (
                self.provenance[0]
                if self.provenance
                else ("chrome" if self.chrome else "black_bar")
            )
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
            "kind": kind,
            "provenance": list(self.provenance),
        }


def is_croppable(record: FileRecord) -> bool:
    suffix = Path(record.path).suffix.lower()
    return (
        record.media_type == MediaType.IMAGE and suffix in CROPPABLE_EXTENSIONS
    ) or (
        record.media_type == MediaType.VIDEO and suffix in VIDEO_CROPPABLE_EXTENSIONS
    )


def suggested_crop(
    record: FileRecord, level: str = DEFAULT_CROP_LEVEL
) -> CropBox | None:
    """The record's crop suggestion at ``level``, or ``None``.

    ``None`` covers no suggestion at that level, a dismissal (which covers
    every level), and a stale or never-measured verdict. The balanced box
    is valid under ``BORDER_SIGNATURE`` alone, so records cached before
    levels existed still answer it; the other levels need the levels
    signature.
    """
    level = validate_crop_level(level)
    if record.border_crop == BORDER_DISMISSED:
        return None
    if level == "balanced":
        if record.border_signature != BORDER_SIGNATURE:
            return None
        return CropBox.decode(record.border_crop)
    if record.border_levels_signature != BORDER_LEVELS_SIGNATURE:
        return None
    encoded = (
        record.border_crop_conservative
        if level == "conservative"
        else record.border_crop_aggressive
    )
    return CropBox.decode(encoded)


def adjust_crop_box(suggestion: CropBox, edges, *, video: bool = False) -> CropBox:
    """Validate a user-moved crop zone against ``suggestion``'s frame.

    ``edges`` is the JSON shape the browser sends: integer ``left``/``top``/
    ``right``/``bottom`` in the same display-orientation pixels the
    suggestion was measured in. The frame size always comes from the
    suggestion, never from the request, so a moved zone cannot smuggle in a
    different image size. Video zones snap outward to the even offsets and
    dimensions 4:2:0 re-encoding requires, cutting a pixel more only where
    the frame edge leaves no room to grow. Raises ``TypeError`` for a
    malformed box and ``ValueError`` for one that is not a real crop of the
    frame, both with user-readable messages.
    """
    if not isinstance(edges, dict):
        raise TypeError("crop box must name its left, top, right and bottom edges")
    values: dict[str, int] = {}
    for name in ("left", "top", "right", "bottom"):
        value = edges.get(name)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"crop box {name} edge must be a whole number of pixels")
        values[name] = value
    left, top = values["left"], values["top"]
    right, bottom = values["right"], values["bottom"]
    if (left, top, right, bottom) == (0, 0, suggestion.width, suggestion.height):
        raise ValueError("the crop zone covers the whole picture; skip instead")
    if video:
        left -= left % 2
        top -= top % 2
        if (right - left) % 2:
            right = right + 1 if right < suggestion.width else right - 1
        if (bottom - top) % 2:
            bottom = bottom + 1 if bottom < suggestion.height else bottom - 1
    chrome = tuple(
        edge
        for edge in suggestion.chrome
        if (edge == "top" and top > 0)
        or (edge == "bottom" and bottom < suggestion.height)
        or (edge == "left" and left > 0)
        or (edge == "right" and right < suggestion.width)
    )
    box = CropBox(
        left,
        top,
        right,
        bottom,
        suggestion.width,
        suggestion.height,
        chrome,
        suggestion.provenance,
    )
    if not box.is_valid():
        raise ValueError("the crop zone must sit inside the picture and keep some of it")
    return box


def _register_heif() -> None:
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception:
        pass


def _scale_integer_luma(image):
    """Scale a 16-bit (``I;16*``) or 32-bit (``I``) integer grayscale image cleanly to ``uint8``."""
    import numpy as np

    array = np.asarray(image, dtype=np.float32)
    if array.min() < 0:
        array = np.clip(array, 0.0, None)
    if image.mode.startswith("I;16"):
        return np.clip(array * (255.0 / 65535.0), 0.0, 255.0).astype(np.uint8)
    peak = float(array.max())
    if peak <= 255.0:
        return array.astype(np.uint8)
    denom = max(65535.0, peak)
    return np.clip(array * (255.0 / denom), 0.0, 255.0).astype(np.uint8)


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
        return _scale_integer_luma(image)
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
    share = near.mean(axis=1)
    flat = share >= BAR_LINE_FRACTION
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
    last_flat = lines[run - 1].astype(np.int16)
    # Advance through JPEG MCU ringing lines at the end of a black bar, matching
    # _colour_edge_bar so bars ending inside 8x8/16x16 blocks leave no sliver.
    for _ in range(COLOUR_BAR_RINGING_LINES):
        if run >= len(lines) - 1 or share[run] < AGGRESSIVE_LINE_FRACTION:
            break
        run += 1
    if run < len(lines) - 1 and share[run] >= 0.5:
        line = lines[run].astype(np.int16)
        to_bar = np.abs(line - level)
        to_picture = np.abs(line - lines[run + 1].astype(np.int16))
        if float((to_bar < to_picture).mean()) >= AGGRESSIVE_LINE_FRACTION:
            run += 1
    after = lines[run : run + BAR_EDGE_LINES].astype(np.int16)
    stepped = (np.abs(after - last_flat) > BAR_TOLERANCE).mean(axis=1)
    off = np.minimum(1.0 - share[run : run + BAR_EDGE_LINES], stepped)
    return run if off.size and off.max() >= BAR_EDGE_FRACTION else 0


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
    if not top and not speck_top:
        top = _past_rounded_corners(luma[:reach_rows], _edge_bar, min_rows, is_rgb=False)[0]
    if not bottom and not speck_bottom:
        bottom = _past_rounded_corners(luma[::-1][:reach_rows], _edge_bar, min_rows, is_rgb=False)[0]
    if not left:
        left = _past_rounded_corners(luma.T[:reach_cols], _edge_bar, min_cols, is_rgb=False)[0]
    if not right:
        right = _past_rounded_corners(luma.T[::-1][:reach_cols], _edge_bar, min_cols, is_rgb=False)[0]
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


def _aggressive_edge_bar(lines, rules: _LooseRules = _BALANCED_LOOSE) -> int:
    """Thickness of a dark bar at the start of ``lines``, loosely measured.

    A line belongs to the bar while at least ``rules.line_fraction`` of
    it sits near the bar's level — caption glyphs and encoder noise ride
    along — and short stretches of heavier ink (a watermark) are jumped.
    The bar ends at clearly-picture lines; no hard edge is required, but
    picture must actually follow within reach, so a frame that is dark all
    the way through yields its whole length and is rejected by the caller.
    """
    import numpy as np

    level = float(np.median(lines[0]))
    if level > rules.dark_luma:
        return 0
    near = (np.abs(lines.astype(np.int16) - level) <= rules.tolerance).mean(axis=1)
    run = _loose_run(near, rules)
    if run == 0 or run >= len(lines):
        return run
    if rules.flat_blocks is not None and not _empty_and_ruled(lines, run, level, rules):
        return 0
    return run


def _loose_run(near, rules: _LooseRules) -> int:
    """Lines of bar at the start of ``near``, each line's share at the bar's level.

    The whole length when nothing ends the bar, and 0 when what follows it
    within reach is not clearly picture.
    """
    gap_allowed = max(4, int(len(near) * rules.gap_fraction))
    last = -1
    gap = 0
    for index, fraction in enumerate(near):
        if fraction >= rules.line_fraction:
            last, gap = index, 0
        elif fraction <= rules.picture_fraction:
            break
        else:
            gap += 1
            if gap > gap_allowed:
                break
    run = last + 1
    if run == 0 or run >= len(near):
        return run
    lookahead = near[run : run + max(BAR_EDGE_LINES * 3, gap_allowed)]
    return run if lookahead.size and float(lookahead.min()) <= rules.picture_fraction else 0


def _empty_and_ruled(lines, run: int, level, rules: _LooseRules) -> bool:
    """Whether the first ``run`` lines hold nothing and stop at a ruled line.

    ``lines`` is luma with a scalar ``level``, or RGB with a colour. Read in
    blocks, a bar is one level throughout (sparse ink aside) and the picture
    steps off it along most of the line where it ends; a dark part of a
    picture drifts from block to block and ends raggedly.
    """
    import numpy as np

    if lines.ndim == 2:
        lines = lines[:, :, None]
    across, channels = lines.shape[1], lines.shape[2]
    size = max(4, across // BAR_BLOCKS_ACROSS)
    tall = min(size, run)
    rows, columns = run // tall, across // size
    if columns == 0:
        return False
    blocks = (
        lines[: rows * tall, : columns * size]
        .reshape(rows, tall, columns, size, channels)
        .mean(axis=(1, 3), dtype=np.float32)
    )
    level_blocks = (np.abs(blocks - level) <= BAR_BLOCK_TOLERANCE).all(axis=2)
    if float(level_blocks.mean()) < rules.flat_blocks:
        return False
    if rules.straight_edge is None:
        return True
    if rules.ruled_above_dark_only:
        luma_level = (
            float(np.dot(level, (0.299, 0.587, 0.114)))
            if np.ndim(level) > 0
            else float(level)
        )
        if luma_level <= DARK_LUMA:
            return True
    # The step is where the bar stops, not somewhere in the lines after it: a
    # dark sky that fades for a hundred rows before a roofline has no edge.
    depth = min(3, run)
    outer = lines[run - depth : run].mean(axis=0, dtype=np.float32)
    for start in range(run, min(run + BAR_EDGE_LINES, len(lines) - depth) + 1):
        inner = lines[start : start + depth].mean(axis=0, dtype=np.float32)
        stepped = (np.abs(inner - outer) >= BAR_STEP_CONTRAST).any(axis=1)
        if float(stepped.mean()) >= rules.straight_edge:
            return True
    return False


def detect_borders_aggressive(
    luma, rules: _LooseRules = _BALANCED_LOOSE
) -> tuple[int, int, int, int] | None:
    """Loose black-bar thickness ``(top, bottom, left, right)`` for a luma array.

    The aggressive counterpart of :func:`detect_borders_in_array`: same size
    and content limits, but bars may be dark gray, noisy, and hold sparse
    ink. Callers must run the human guard before suggesting these bars.
    """
    import numpy as np

    height, width = luma.shape[:2]
    if height < 8 or width < 8:
        return None
    reach_rows = height - int(np.ceil(height * MIN_CONTENT_FRACTION)) + BAR_EDGE_LINES
    reach_cols = width - int(np.ceil(width * MIN_CONTENT_FRACTION)) + BAR_EDGE_LINES
    top = _aggressive_edge_bar(luma[:reach_rows], rules)
    if top >= height:
        return None
    bottom = _aggressive_edge_bar(luma[::-1][:reach_rows], rules)
    left = _aggressive_edge_bar(luma.T[:reach_cols], rules)
    right = _aggressive_edge_bar(luma.T[::-1][:reach_cols], rules)
    min_rows = max(MIN_BORDER_PIXELS, int(np.ceil(height * rules.min_fraction)))
    min_cols = max(MIN_BORDER_PIXELS, int(np.ceil(width * rules.min_fraction)))
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


def _colour_edge_bar(lines) -> int:
    """Thickness of the flat bar of one colour at the start of ``lines``.

    ``lines`` is an RGB array, one row per line running parallel to the edge,
    ordered from it. Black bars are :func:`_edge_bar`'s; this answers for
    every other colour, and returns 0 unless the bar is flat on all three
    channels and ends in a hard edge against the picture.
    """
    import numpy as np

    colour = np.median(lines[0], axis=0)
    if float(colour @ (0.299, 0.587, 0.114)) <= DARK_LUMA:
        return 0
    near = (np.abs(lines.astype(np.int16) - colour.astype(np.int16)) <= BAR_TOLERANCE).all(axis=2)
    flat = near.mean(axis=1) >= BAR_LINE_FRACTION
    if flat.all():
        return 0  # one colour as far as a bar may reach: a backdrop, not a frame
    run = int(np.argmin(flat))
    if run == 0:
        return 0
    last_flat = lines[run - 1].astype(np.int16)
    # The bar's last lines take the ringing of the edge beside them and are
    # no longer flat, yet are still bar while most of each sits at its colour:
    # left behind, a white hairline shows on the cropped picture as a black
    # one never would.
    share = near.mean(axis=1)
    for _ in range(COLOUR_BAR_RINGING_LINES):
        if run >= len(lines) - 1 or share[run] < AGGRESSIVE_LINE_FRACTION:
            break
        run += 1
    # A JPEG stores colour at half height, so a bar's last line can share its
    # colour with the picture's first and sit between the two: still bar
    # where most of it is nearer the bar's colour than the line after it.
    if run < len(lines) - 1:
        line = lines[run].astype(np.int16)
        to_bar = np.abs(line - colour.astype(np.int16)).sum(axis=1)
        to_picture = np.abs(line - lines[run + 1].astype(np.int16)).sum(axis=1)
        if float((to_bar < to_picture).mean()) >= AGGRESSIVE_LINE_FRACTION:
            run += 1
    # The edge is a step from the bar's last flat line, not a drift from its
    # first: a sky that shades smoothly out of tolerance has left the colour
    # without ever leaving the lines just before.
    after = lines[run : run + BAR_EDGE_LINES].astype(np.int16)
    stepped = (np.abs(after - last_flat) > BAR_TOLERANCE).any(axis=2).mean(axis=1)
    off = np.minimum(1.0 - share[run : run + BAR_EDGE_LINES], stepped)
    return run if off.size and off.max() >= COLOUR_BAR_EDGE_FRACTION else 0


def _loose_colour_edge_bar(lines, rules: _LooseRules = _COLOUR_LOOSE) -> int:
    """Thickness of a colour bar at the start of ``lines`` that may hold ink.

    The any-colour counterpart of :func:`_aggressive_edge_bar`: most of each
    line sits at the bar's colour, the bar's blocks hold nothing, and the
    picture steps off it along a ruled line.
    """
    import numpy as np

    colour = np.median(lines[0], axis=0)
    if float(colour @ (0.299, 0.587, 0.114)) <= rules.dark_luma:
        return 0
    off = np.abs(lines.astype(np.int16) - colour.astype(np.int16))
    near = (off <= rules.tolerance).all(axis=2).mean(axis=1)
    run = _loose_run(near, rules)
    if run == 0 or run >= len(lines):
        return 0
    return run if _empty_and_ruled(lines, run, colour.astype(np.float32), rules) else 0


def _past_rounded_corners(
    lines, measure, least: int, *, is_rgb: bool = True
) -> tuple[int, int]:
    """Peel past small symmetric outer rounded screen/card corners if a full bar follows."""
    import numpy as np

    across = lines.shape[1]
    margin = max(6, int(across * 0.08))
    max_corner = min(len(lines) // 3, margin)
    if across < margin * 3 or max_corner < 4:
        return 0, 0
    mid0 = lines[0, margin : across - margin]
    colour = np.median(mid0, axis=0)
    mid_diff = np.abs(mid0.astype(np.int16) - colour.astype(np.int16))
    mid_near = (mid_diff <= BAR_TOLERANCE).all(axis=1) if is_rgb else (mid_diff <= BAR_TOLERANCE)
    if float(mid_near.mean()) < BAR_LINE_FRACTION:
        return 0, 0
    probe = lines[: max_corner + 1].astype(np.int16)
    diff = np.abs(probe - colour.astype(np.int16))
    near = (diff <= BAR_TOLERANCE).all(axis=2) if is_rgb else (diff <= BAR_TOLERANCE)
    row_flat = near.mean(axis=1) >= BAR_LINE_FRACTION
    if row_flat[0] or not row_flat[1:].any():
        return 0, 0
    c_end = int(np.argmax(row_flat[1:])) + 1
    # Middle must stay flat across all corner rows, and deviations must stay
    # strictly inside the outer corner margins while shrinking inward.
    if not (near[:c_end, margin : across - margin].mean(axis=1) >= BAR_LINE_FRACTION).all():
        return 0, 0
    corner_off = ~near[:c_end]
    if corner_off[:, margin : across - margin].any():
        return 0, 0
    counts = corner_off.sum(axis=1)
    if c_end >= 2 and (np.diff(counts) > 2).any():
        return 0, 0
    behind = measure(lines[c_end:])
    if behind < least:
        return 0, 0
    bar_colour = np.median(lines[c_end], axis=0)
    if float(np.abs(bar_colour - colour).max()) > BAR_TOLERANCE:
        return 0, 0
    return c_end + behind, c_end


def detect_colour_bars(
    rgb, *, loose: bool = False, interface: tuple[bool, bool] = (False, False)
) -> tuple[int, int, int, int] | None:
    """Flat non-black bar thickness ``(top, bottom, left, right)`` for an RGB array.

    The any-colour counterpart of :func:`detect_borders_in_array`, with the
    same size and content limits: white, gray and coloured frames. ``loose``
    measures them as the aggressive level does, ink and all. ``interface``
    says the top or bottom of ``rgb`` is where a screenshot's interface was
    cut away: the walk that cut it already found where the picture begins,
    so a colour bar there counts only as one side of a frame, its colour cut
    from another edge too.
    """
    import numpy as np

    height, width = rgb.shape[:2]
    if height < 8 or width < 8:
        return None
    keep_rows = int(np.ceil(height * MIN_CONTENT_FRACTION))
    keep_cols = int(np.ceil(width * MIN_CONTENT_FRACTION))
    min_rows = max(MIN_BORDER_PIXELS, int(np.ceil(height * MIN_BORDER_FRACTION)))
    min_cols = max(MIN_BORDER_PIXELS, int(np.ceil(width * MIN_BORDER_FRACTION)))
    measure = _loose_colour_edge_bar if loose else _colour_edge_bar
    top = bottom = left = right = 0
    # Track (prev_thickness, new_thickness, bar_colour) per edge across passes so
    # _without_page_ground / _framed_past_interface inspect the peeled bar colour.
    layers: list[list[tuple[int, int, Any]]] = [[], [], [], []]
    # A side is a bar once the picture runs along most of it, which it may
    # only do after another side has gone: a picture set to one side of a
    # white canvas loses the wide margins first and then the one above it.
    for _ in range(COLOUR_BAR_PASSES):
        inner = rgb[top : height - bottom, left : width - right]
        reach_rows = inner.shape[0] - keep_rows + BAR_EDGE_LINES
        reach_cols = inner.shape[1] - keep_cols + BAR_EDGE_LINES
        if reach_rows <= BAR_EDGE_LINES or reach_cols <= BAR_EDGE_LINES:
            break
        sideways = inner.transpose(1, 0, 2)
        strips = (
            (inner[:reach_rows], min_rows),
            (inner[::-1][:reach_rows], min_rows),
            (sideways[:reach_cols], min_cols),
            (sideways[::-1][:reach_cols], min_cols),
        )
        raw_found = [_past_edge_artifact(measure, s, m) for s, m in strips]
        found = tuple(
            thickness if thickness >= least else 0
            for (thickness, _off), (_, least) in zip(raw_found, strips)
        )
        if not any(found):
            break
        if height - top - bottom - found[0] - found[1] < height * MIN_CONTENT_FRACTION:
            break
        if width - left - right - found[2] - found[3] < width * MIN_CONTENT_FRACTION:
            break
        cur = (top, bottom, left, right)
        for edge, ((thickness, offset), (strip, _)) in enumerate(zip(raw_found, strips)):
            if found[edge]:
                line_idx = min(offset, found[edge] - 1)
                bar_colour = np.median(strip[line_idx], axis=0).astype(np.int16)
                layers[edge].append((cur[edge], cur[edge] + found[edge], bar_colour))
        top, bottom = top + found[0], bottom + found[1]
        left, right = left + found[2], right + found[3]
    if not (top or bottom or left or right):
        return None
    bars = _without_page_ground(rgb, (top, bottom, left, right), layers=layers)
    if any(interface) and any(bars):
        bars = _framed_past_interface(rgb, bars, interface, layers=layers)
    return bars if any(bars) else None


def _past_edge_artifact(measure, lines, least: int) -> tuple[int, int]:
    """``(thickness, bar_start_offset)`` peeling past an outer hairline or corner mask."""
    import numpy as np

    found = measure(lines)
    if found >= least or least < 2:
        return found, 0
    hairline = found
    if not hairline:  # a black line is not this measure's to find
        outer = lines[:least].astype(np.int16)
        colour = np.median(outer[0], axis=0).astype(np.int16)
        flat = (np.abs(outer - colour) <= BAR_TOLERANCE).all(axis=2).mean(axis=1)
        flat = flat >= BAR_LINE_FRACTION
        if flat[0] and not flat.all():
            hairline = int(np.argmin(flat))
    if hairline:
        behind = measure(lines[hairline:])
        if behind >= least:
            return hairline + behind, hairline
    corner_found, corner_offset = _past_rounded_corners(lines, measure, least, is_rgb=True)
    if corner_found >= least:
        return corner_found, corner_offset
    return found, 0


def _past_hairline(measure, lines, least: int) -> int:
    """``measure(lines)``, or the bar standing behind a hairline at the edge.

    A file cut or exported a pixel wide of its frame carries a line or two
    of another colour outside the bar. Too thin to be a bar itself, it would
    hide the one behind it; when a full bar follows it, both go.
    """
    return _past_edge_artifact(measure, lines, least)[0]


def _edge_colours(rgb, bars: tuple[int, int, int, int] | None = None):
    """The bar colour for each edge: top, bottom, left, right.

    When ``bars`` is provided, samples the actual peeled bar interior (skipping
    outer hairlines/corner masks) rather than raw row/column 0.
    """
    import numpy as np

    lines = (rgb, rgb[::-1], rgb.transpose(1, 0, 2), rgb.transpose(1, 0, 2)[::-1])
    out = []
    for edge, strip in enumerate(lines):
        if bars and bars[edge] > 0:
            idx = bars[edge] // 2
            out.append(np.median(strip[idx], axis=0).astype(np.int16))
        else:
            out.append(np.median(strip[0], axis=0).astype(np.int16))
    return out


def _active_layer_colours(layers, bars):
    """Active peeled layers matching each edge's current thickness."""
    active = []
    for edge, thickness in enumerate(bars):
        kept_layers = [layer for layer in layers[edge] if layer[1] <= thickness]
        active.append(kept_layers)
    return active


def _framed_past_interface(
    rgb, bars, interface: tuple[bool, bool], *, layers=None
):
    """``bars`` less a top or bottom bar that follows interface and frames nothing."""
    import numpy as np

    result = list(bars)
    if layers is None:
        colours = _edge_colours(rgb, bars)
        for edge in (0, 1):
            if not (interface[edge] and result[edge]):
                continue
            framed = any(
                result[other]
                and int(np.abs(colours[other] - colours[edge]).max()) <= BAR_TOLERANCE
                for other in range(4)
                if other != edge
            )
            if not framed:
                result[edge] = 0
        return tuple(result)

    active = _active_layer_colours(layers, result)
    for edge in (0, 1):
        if not (interface[edge] and result[edge]):
            continue
        other_colours = [
            layer[2]
            for other in range(4)
            if other != edge and result[other]
            for layer in active[other]
        ]
        while active[edge]:
            top_layer = active[edge][-1]
            framed = any(
                int(np.abs(col - top_layer[2]).max()) <= BAR_TOLERANCE
                for col in other_colours
            )
            if framed:
                break
            active[edge].pop()
            result[edge] = top_layer[0]
    return tuple(result)


def _without_page_ground(
    rgb, bars: tuple[int, int, int, int], *, layers=None
) -> tuple[int, int, int, int]:
    """``bars`` less every edge whose colour is the ground of what is kept."""
    import numpy as np

    height, width = rgb.shape[:2]
    grounds: dict[tuple[int, ...], bool] = {}

    def is_ground(kept, colour) -> bool:
        key = (kept.shape[0], kept.shape[1], *(int(v) for v in colour))
        if key not in grounds:
            near = (np.abs(kept - colour) <= BAR_TOLERANCE).all(axis=2)
            rows = _share_between(near.mean(axis=1) >= BAR_LINE_FRACTION)
            columns = _share_between(near.mean(axis=0) >= BAR_LINE_FRACTION)
            grounds[key] = max(rows, columns) > COLOUR_GROUND_FRACTION
        return grounds[key]

    result = list(bars)
    if layers is None:
        top, bottom, left, right = bars
        kept = rgb[top : height - bottom, left : width - right].astype(np.int16)
        colours = _edge_colours(rgb, bars)
        for edge, thickness in enumerate(bars):
            if thickness and is_ground(kept, colours[edge]):
                result[edge] = 0
        return tuple(result)

    active = _active_layer_colours(layers, result)
    changed = True
    while changed:
        changed = False
        top, bottom, left, right = result
        kept = rgb[top : height - bottom, left : width - right].astype(np.int16)
        for edge in range(4):
            if not active[edge]:
                continue
            top_layer = active[edge][-1]
            if is_ground(kept, top_layer[2]):
                active[edge].pop()
                result[edge] = top_layer[0]
                changed = True
                break
    return tuple(result)


def _share_between(blank) -> float:
    """Share of ``blank`` (one flag per line) set with unset lines on both sides."""
    import numpy as np

    filled = np.flatnonzero(~blank)
    if filled.size == 0:
        return 1.0
    return float(blank[filled[0] : filled[-1] + 1].sum()) / blank.size


def _detect_gradient_bars(rgb, *, min_rows: int, min_cols: int) -> tuple[int, int, int, int] | None:
    """Smooth bilateral gradient/vignette bars (e.g. IG Stories vignette).

    Requires paired smooth bands on BOTH opposing edges (top+bottom or
    left+right) ending in ruler-straight horizontal/vertical discontinuities
    against markedly higher-texture interior picture content, so single-sided
    natural sky/sunset gradients remain untouched.
    """
    import numpy as np

    height, width = rgb.shape[:2]
    if height < 32 or width < 32:
        return None
    f = rgb.astype(np.int16)

    def axis_pair(arr, min_bar: int) -> tuple[int, int]:
        n, _m = arr.shape[:2]
        max_bar = int(n * 0.40)
        if max_bar < min_bar:
            return 0, 0
        # Horizontal smoothness along each line + per-line median.
        med = np.median(arr, axis=1)  # (n, 3)
        near_med = (np.abs(arr - med[:, None, :]).max(axis=2) <= BAR_TOLERANCE + 2).mean(axis=1) >= 0.99
        diff_along = np.abs(arr[:, 1:, :] - arr[:, :-1, :]).mean(axis=(1, 2))

        def walk(rev: bool) -> int:
            s_med = med[::-1] if rev else med
            s_near = near_med[::-1] if rev else near_med
            s_diff = diff_along[::-1] if rev else diff_along
            s_arr = arr[::-1] if rev else arr
            run = 0
            for i in range(max_bar):
                if not s_near[i] or s_diff[i] > 2.0:
                    break
                if i > 0 and float(np.abs(s_med[i] - s_med[i - 1]).max()) > 5.0:
                    break
                run = i + 1
            if run < min_bar or run + 4 >= n:
                return 0
            # Must genuinely vary along the gradient axis (flat bars belong to detect_colour_bars).
            if float(np.abs(s_med[0] - s_med[run - 1]).max()) < 10.0:
                return 0
            bar_hf = float(s_diff[:run].mean())
            outer = s_arr[run - 1].astype(np.float32)
            for d in range(3):
                inner = s_arr[run + d].astype(np.float32)
                stepped = (np.abs(inner - outer) >= BAR_STEP_CONTRAST).any(axis=1)
                if float(stepped.mean()) >= 0.80:
                    # Must enter textured content rather than another flat band.
                    after_hf = float(s_diff[run + d : min(n, run + d + 24)].mean())
                    if after_hf >= max(4.0, 2.5 * bar_hf):
                        return run
            return 0

        t1, t2 = walk(False), walk(True)
        if t1 >= min_bar and t2 >= min_bar and (n - t1 - t2 >= n * MIN_CONTENT_FRACTION):
            return t1, t2
        return 0, 0

    top, bottom = axis_pair(f, min_rows)
    left, right = axis_pair(f.transpose(1, 0, 2), min_cols)
    if not (top or bottom or left or right):
        return None
    return top, bottom, left, right


def _merge_bars_monotone(floor_bars, extra_bars, width: int, height: int):
    """Per-edge max of ``floor_bars`` and ``extra_bars``, preserving ``floor_bars``
    and ``MIN_CONTENT_FRACTION``."""
    if extra_bars is None:
        return floor_bars
    if floor_bars is None:
        return extra_bars
    merged = list(floor_bars)
    # Try merging vertical then horizontal independently if combining both overflows.
    cand_v = (max(floor_bars[0], extra_bars[0]), max(floor_bars[1], extra_bars[1]))
    if height - cand_v[0] - cand_v[1] >= height * MIN_CONTENT_FRACTION:
        merged[0], merged[1] = cand_v
    cand_h = (max(floor_bars[2], extra_bars[2]), max(floor_bars[3], extra_bars[3]))
    if width - cand_h[0] - cand_h[1] >= width * MIN_CONTENT_FRACTION:
        merged[2], merged[3] = cand_h
    return tuple(merged)


def _with_colour_bars(bars, colour, width: int, height: int):
    """``bars`` widened edge by edge to ``colour``'s, where a picture is left."""
    return _merge_bars_monotone(bars, colour, width, height)


def _combined_bars(luma, *, speck=False, aggressive: bool = True, rules=_BALANCED_LOOSE):
    """Per-edge maximum of the strict and (when allowed) loose bars."""
    height, width = luma.shape[:2]
    strict = detect_borders_in_array(luma, speck=speck)
    if not aggressive:
        return strict
    loose = detect_borders_aggressive(luma, rules)
    return _merge_bars_monotone(strict, loose, width, height)


def _level_bars(
    luma,
    *,
    speck=False,
    loose_allowed: bool = True,
    rgb=None,
    interface=(False, False),
    return_details: bool = False,
):
    """Bar tuples per aggressiveness level, ``None`` where a level declines.

    With ``rgb`` (the same frame in colour), flat bars of any other colour
    join every level: they are proven the way strict black bars are. Colour
    bars holding ink join the aggressive level alone, and like every loose
    bar only when the human guard can run.

    Conservative is the strict pass alone; balanced combines strict with the
    balanced loose pass; aggressive takes the per-edge maximum of balanced and
    the looser level pass, so each level crops at least as much as the one
    below it by construction. The loose passes contribute only when
    ``loose_allowed`` (the human guard can run); the caller still guards the
    result before suggesting it.
    """
    height, width = luma.shape[:2]
    strict = detect_borders_in_array(luma, speck=speck)
    if not loose_allowed:
        balanced_black = strict
        aggressive_black = strict
    else:
        balanced_black = _combined_bars(luma, speck=speck)
        loose = detect_borders_aggressive(luma, _AGGRESSIVE_LOOSE)
        aggressive_black = _merge_bars_monotone(balanced_black, loose, width, height)

    levels = {
        "conservative": strict,
        "balanced": balanced_black,
        "aggressive": aggressive_black,
    }
    flat_colour = None
    loose_colour = None
    gradient = None

    if rgb is not None:
        flat_colour = detect_colour_bars(rgb, interface=interface)
        if flat_colour is not None:
            levels = {
                name: _merge_bars_monotone(bars, flat_colour, width, height)
                for name, bars in levels.items()
            }
        if loose_allowed:
            loose_colour = detect_colour_bars(rgb, loose=True, interface=interface)
            if loose_colour is not None:
                levels["aggressive"] = _merge_bars_monotone(
                    levels["aggressive"], loose_colour, width, height
                )
            min_rows = max(MIN_BORDER_PIXELS, int(height * MIN_BORDER_FRACTION))
            min_cols = max(MIN_BORDER_PIXELS, int(width * MIN_BORDER_FRACTION))
            gradient = _detect_gradient_bars(rgb, min_rows=min_rows, min_cols=min_cols)
            if gradient is not None:
                levels["balanced"] = _merge_bars_monotone(
                    levels["balanced"], gradient, width, height
                )
                levels["aggressive"] = _merge_bars_monotone(
                    levels["aggressive"], gradient, width, height
                )

    # Guarantee strict monotonicity: conservative <= balanced <= aggressive.
    levels["balanced"] = _merge_bars_monotone(
        levels["conservative"], levels["balanced"], width, height
    )
    levels["aggressive"] = _merge_bars_monotone(
        levels["balanced"], levels["aggressive"], width, height
    )
    if return_details:
        return levels, {
            "strict_black": strict,
            "flat_colour": flat_colour,
            "loose_colour": loose_colour,
            "gradient": gradient,
        }
    return levels


# --- human guard --------------------------------------------------------------
# Faces (bundled YuNet) plus bodies (bundled YOLOX, the people-score model),
# one detector set per thread. Only files with a crop suggestion are ever
# guarded, so the cost stays on the candidate pile. Any failure to start the
# guard disables the aggressive pass entirely; the strict detector's
# timidity is the fallback. (HOG body detection was tried first and fired
# dozens of frame-spanning false boxes on ordinary footage.)

_guard_local = threading.local()


def _get_guard():
    """``(face_counter, people_scorer)`` for this thread, or ``None``."""
    cached = getattr(_guard_local, "guard", False)
    if cached is not False:
        return cached
    guard = None
    try:
        from .face_detection import _YuNetFaceCounter
        from .people_score import PeopleScorer

        kwargs: dict = {}
        if "load_genderage" in inspect.signature(_YuNetFaceCounter.__init__).parameters:
            kwargs["load_genderage"] = False
        counter = _YuNetFaceCounter(**kwargs)
        if hasattr(counter, "cv2") and hasattr(counter.cv2, "setNumThreads"):
            counter.cv2.setNumThreads(1)
        guard = (counter, PeopleScorer())
    except Exception:
        guard = None
    _guard_local.guard = guard
    return guard


def _human_guard_available() -> bool:
    return _get_guard() is not None


def _guard_shrunk(rgb):
    """``rgb`` (a numpy frame) shrunk to the guard's working size."""
    import cv2

    height, width = rgb.shape[:2]
    longest = max(width, height)
    if longest <= GUARD_MAX_SIDE:
        return rgb
    scale = GUARD_MAX_SIDE / longest
    return cv2.resize(
        rgb,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _human_guard_boxes_detailed(
    rgb,
) -> list[tuple[tuple[float, float, float, float], bool, tuple[float, float, float, float]]] | None:
    """Detect faces & bodies once, returning ``[(raw_box, is_face, expanded_box), ...]``."""
    guard = _get_guard()
    if guard is None:
        return None
    counter, scorer = guard
    cv2 = counter.cv2
    try:
        frame = _guard_shrunk(rgb)
        entries: list[
            tuple[tuple[float, float, float, float], bool, tuple[float, float, float, float]]
        ] = []
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        faces, candidate = counter._detect_best(bgr)
        if faces is not None and len(faces):
            cand_h, cand_w = candidate.shape[:2]
            expand_x0, expand_y0, expand_x1, expand_y1 = GUARD_FACE_EXPAND
            for face in faces:
                x, y, w, h = (float(value) for value in face[:4])
                raw = (
                    max(0.0, x / cand_w),
                    max(0.0, y / cand_h),
                    min(1.0, (x + w) / cand_w),
                    min(1.0, (y + h) / cand_h),
                )
                expanded = (
                    max(0.0, (x - expand_x0 * w) / cand_w),
                    max(0.0, (y - expand_y0 * h) / cand_h),
                    min(1.0, (x + w + expand_x1 * w) / cand_w),
                    min(1.0, (y + h + expand_y1 * h) / cand_h),
                )
                entries.append((raw, True, expanded))
        for x0, y0, x1, y1 in scorer.person_boxes(frame, threshold=GUARD_PERSON_CONFIDENCE):
            raw = (max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1))
            grow_x = GUARD_BODY_EXPAND * (x1 - x0)
            grow_y = GUARD_BODY_EXPAND * (y1 - y0)
            expanded = (
                max(0.0, x0 - grow_x),
                max(0.0, y0 - grow_y),
                min(1.0, x1 + grow_x),
                min(1.0, y1 + grow_y),
            )
            entries.append((raw, False, expanded))
        return entries
    except Exception:
        return None


def _human_guard_boxes(rgb) -> list[tuple[float, float, float, float]] | None:
    """Face and body boxes in ``rgb`` as fractions ``(x0, y0, x1, y1)``.

    ``None`` when the guard cannot run; an empty list when it ran and found
    nobody. Face boxes are expanded so hair and shoulders are kept too.
    """
    entries = _human_guard_boxes_detailed(rgb)
    if entries is None:
        return None
    return [expanded for _raw, _is_face, expanded in entries]


_DEFAULT_HUMAN_GUARD_BOXES = _human_guard_boxes


def _human_guard_boxes_pair(rgb):
    """Return ``(raw_entries, expanded_boxes)`` honoring any monkeypatch to ``_human_guard_boxes``."""
    if _human_guard_boxes is not _DEFAULT_HUMAN_GUARD_BOXES:
        boxes = _human_guard_boxes(rgb)
        if boxes is None:
            return None
        return [(box, True, box) for box in boxes], boxes
    entries = _human_guard_boxes_detailed(rgb)
    if entries is None:
        return None
    return entries, [expanded for _raw, _is_face, expanded in entries]


def _guarded_bars(bars, boxes, width: int, height: int):
    """Pull each bar back so no ``boxes`` person loses a pixel to the crop."""
    top, bottom, left, right = bars
    for x0, y0, x1, y1 in boxes:
        top = min(top, int(y0 * height))
        bottom = min(bottom, height - int(y1 * height + 0.999))
        left = min(left, int(x0 * width))
        right = min(right, width - int(x1 * width + 0.999))
    return (max(0, top), max(0, bottom), max(0, left), max(0, right))


def _guard_chrome_and_colour(bars, raw_entries, width: int, height: int):
    """Guard ``chrome`` and flat ``colour_bars`` against cutting into faces/people.

    A person cleanly inside a framed photo or screenshot post stays untouched,
    so intended chrome/mats still trim to the picture edge; when a raw face
    or body actually enters the cut zone, pull that edge back past the
    expanded person box so hair and face stay intact.
    """
    top, bottom, left, right = bars
    for raw, is_face, expanded in raw_entries:
        rx0, ry0, rx1, ry1 = raw
        ex0, ey0, ex1, ey1 = expanded
        tol_x = 0 if is_face else min(6, int(0.015 * (rx1 - rx0) * width))
        tol_y = 0 if is_face else min(6, int(0.015 * (ry1 - ry0) * height))
        if top > 0 and int(ry0 * height) < top - tol_y:
            top = min(top, int(ey0 * height))
        if bottom > 0 and int(ry1 * height + 0.999) > height - bottom + tol_y:
            bottom = min(bottom, height - int(ey1 * height + 0.999))
        if left > 0 and int(rx0 * width) < left - tol_x:
            left = min(left, int(ex0 * width))
        if right > 0 and int(rx1 * width + 0.999) > width - right + tol_x:
            right = min(right, width - int(ex1 * width + 0.999))
    return (max(0, top), max(0, bottom), max(0, left), max(0, right))


def _guard_floor(combined, strict, boxes, width: int, height: int):
    """Guarded bars, never below what the strict detector already proved.

    A strict bar is flat with a hard edge against the picture, so a person
    cannot be hiding in it; the guard only arbitrates the aggressive
    extension past it. This also keeps a detector false positive (HOG fires
    on some textures) from erasing a proven bar.
    """
    guarded = _guarded_bars(combined, boxes, width, height)
    return tuple(max(kept, proven) for kept, proven in zip(guarded, strict))


def _alpha_channel(image):
    """2-D ``uint8`` alpha array if ``image`` carries transparency, else ``None``."""
    import numpy as np

    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if image.mode not in ("RGBA", "LA", "PA"):
        return None
    return np.asarray(image.getchannel("A"), dtype=np.uint8)


def _see_through(image) -> bool:
    """Whether a meaningful fraction of ``image`` is translucent/transparent.

    A cut-out's empty canvas is flattened onto white to be read, which would
    make a white frame of it; a single stray pixel with alpha=254 does not
    disable colour-bar detection.
    """
    alpha = _alpha_channel(image)
    if alpha is None:
        return False
    return float((alpha < 250).mean()) >= 0.005


def _transparent_window_bars(image, *, evidence: str | None) -> tuple[int, int, int, int] | None:
    """Crop transparent drop-shadow/padding margins on macOS window screenshots.

    Requires either a soft drop-shadow ring (0 < alpha < 250) or explicit
    screenshot evidence (name/metadata), plus a solidly opaque rectangular
    window inside transparent outer edges, so cut-outs and stickers stay untouched.
    """
    import numpy as np

    alpha = _alpha_channel(image)
    if alpha is None:
        return None
    height, width = alpha.shape[:2]
    if height < 32 or width < 32:
        return None
    has_shadow = float(((alpha > 0) & (alpha < 250)).mean()) >= 0.002
    if not (has_shadow or evidence in ("name", "metadata")):
        return None
    opaque = alpha >= 245
    cols = np.flatnonzero(opaque.mean(axis=0) >= 0.25)
    rows = np.flatnonzero(opaque.mean(axis=1) >= 0.25)
    if not cols.size or not rows.size:
        return None
    left, right_end = int(cols[0]), int(cols[-1]) + 1
    top, bottom_end = int(rows[0]), int(rows[-1]) + 1
    bottom = height - bottom_end
    right = width - right_end
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
    if float(opaque[top : height - bottom, left : width - right].mean()) < 0.96:
        return None
    edge_strips = (
        (top, alpha[:top], alpha[0]),
        (bottom, alpha[height - bottom :], alpha[-1]),
        (left, alpha[:, :left], alpha[:, 0]),
        (right, alpha[:, width - right :], alpha[:, -1]),
    )
    for thickness, strip, outer_line in edge_strips:
        if thickness and (float(np.median(outer_line)) > 15 or float(strip.mean()) > 120):
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
        return Image.fromarray(_scale_integer_luma(image), mode="L").convert("RGB")
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
    if IOS_SCREENSHOT_NAME_PATTERN.match(path.name):
        return "name"
    width, height = image.size
    if exif.get(0x0112, 1) in (5, 6, 7, 8):
        width, height = height, width
    short_side, long_side = min(width, height), max(width, height)
    ratio = long_side / short_side if short_side > 0 else 0.0
    if height >= width * 1.3 and width in PHONE_SCREEN_WIDTHS:
        return "shape"
    if width >= height * 1.3 and height in PHONE_SCREEN_WIDTHS:
        return "shape"
    if short_side >= 320 and 1.73 <= ratio <= 2.50:
        return "shape"
    if short_side in IPAD_SHORT_SIDES and 1.30 <= ratio <= 1.55:
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
    one_colour = ink <= 1.0 - CHROME_ONE_COLOUR
    plain = _peel_colours(window, medians, far, one_colour)

    # Anti-aliased downsampling can shift a dense text row's median partway
    # between ink and background (leaving >55% of the row "far" from median);
    # when an outer margin yields a cleaner peel with less ink, adopt it.
    suspect = np.flatnonzero((ink > 0.35) & ((plain < CHROME_PLAIN) | (ink > 0.55)))
    if suspect.size and columns >= 16:
        margin_w = max(4, min(16, columns // 25))
        left_med = np.median(window[suspect, :margin_w], axis=1)
        right_med = np.median(window[suspect, -margin_w:], axis=1)
        for cand_med in (left_med, right_med):
            cand_dist = np.abs(window[suspect] - cand_med[:, None, :]).max(axis=2)
            cand_far = cand_dist > CHROME_TOLERANCE
            cand_ink = cand_far.mean(axis=1)
            cand_one = cand_ink <= 1.0 - CHROME_ONE_COLOUR
            cand_plain = _peel_colours(window[suspect], cand_med, cand_far, cand_one)
            take = (cand_plain >= CHROME_PLAIN) & (cand_ink < ink[suspect])
            if take.any():
                rows_idx = suspect[take]
                medians[rows_idx] = cand_med[take]
                distance[rows_idx] = cand_dist[take]
                far[rows_idx] = cand_far[take]
                ink[rows_idx] = cand_ink[take]
                one_colour[rows_idx] = cand_one[take]
                plain[rows_idx] = cand_plain[take]

    strong = (distance > CHROME_CONTRAST).mean(axis=1)
    inked = ink >= CHROME_INK_FRACTION
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
        & ((ink <= 0.5) | ((ink <= 0.72) & (runs >= CHROME_TEXT_RUNS) & (longest <= CHROME_GLYPH_RUN)))
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


def _is_dynamic_island_bar(rows, start: int, end: int, inked) -> bool:
    """Whether ``[start:end]`` holds an iPhone Dynamic Island pill flanked by status glyphs."""
    import numpy as np

    columns = rows.far.shape[1]
    gap_min = max(2, int(0.015 * columns))
    for row in inked:
        if rows.longest[row] < 0.14 or rows.runs[row] < 3:
            continue
        starts, ends = _runs_of(rows.far[row])
        if starts.size < 3:
            continue
        widths = ends - starts
        w_idx = int(np.argmax(widths))
        if w_idx == 0 or w_idx == starts.size - 1:
            continue
        pill_w = float(widths[w_idx]) / columns
        pill_mid = float(starts[w_idx] + ends[w_idx]) / (2.0 * columns)
        if not (0.14 <= pill_w <= 0.42 and 0.42 <= pill_mid <= 0.58):
            continue
        if (starts[w_idx] - ends[w_idx - 1]) < gap_min or (
            starts[w_idx + 1] - ends[w_idx]
        ) < gap_min:
            continue
        flank_widths = np.delete(widths, w_idx) / columns
        if flank_widths.size < 2 or float(flank_widths.max()) > CHROME_GLYPH_RUN:
            continue
        seg = rows.pixels[row, starts[w_idx] + 1 : max(starts[w_idx] + 2, ends[w_idx] - 1)]
        if seg.size == 0:
            continue
        pill_col = np.median(seg, axis=0)
        if float(np.dot(pill_col, (0.299, 0.587, 0.114))) > 45.0:
            continue
        if float((np.abs(seg - pill_col).max(axis=1) <= CHROME_TOLERANCE).mean()) < 0.80:
            continue
        return True
    return False


def _is_centered_clock_bar(rows, start: int, end: int, inked, height: int) -> bool:
    """Whether ``[start:end]`` holds a centered-clock status bar flanked by icons."""
    import numpy as np

    if end - start > height * 0.04 or rows.strong[inked].mean() >= 0.25:
        return False
    columns = rows.far.shape[1]
    gutter_min = max(4, int(0.06 * columns))
    c_lo, c_hi = int(0.36 * columns), int(0.64 * columns)
    for row in inked[rows.glyph[inked]]:
        starts, ends = _runs_of(rows.far[row])
        if starts.size < 4:
            continue
        in_center = (starts >= c_lo) & (ends <= c_hi)
        if int(in_center.sum()) < 3:
            continue
        c_starts, c_ends = starts[in_center], ends[in_center]
        if float(c_ends[-1] - c_starts[0]) / columns > 0.20:
            continue
        if float((c_ends - c_starts).max()) / columns > CHROME_LETTER_WIDTH:
            continue
        left_mask = ends <= c_starts[0] - gutter_min
        right_mask = starts >= c_ends[-1] + gutter_min
        if not (left_mask.any() and right_mask.any()):
            continue
        flank_w = np.concatenate((ends[left_mask] - starts[left_mask], ends[right_mask] - starts[right_mask]))
        if float(flank_w.max()) / columns > CHROME_GLYPH_RUN:
            continue
        # Only blank gutters between center block and flanks (no caption words bridging them).
        outside = ~in_center
        if (outside & ~left_mask & ~right_mask).any():
            continue
        return True
    return False


def _looks_like_a_bar(rows, start: int, end: int, height: int, edge: ChromeEdge, *,
                      center_empty: bool, pill_allowed: bool, busy_min: int,
                      words: bool = False) -> str | None:
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
    if rows.strong[inked].sum() < 0.5 * rows.ink[inked].sum():
        return None  # faint: a gradient crossing the tolerance, not glyphs
    if center_empty and _is_dynamic_island_bar(rows, start, end, inked):
        return "glyphs"
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
    longest = float(np.median(rows.longest[inked]))
    if (
        rows.strong[inked].mean() >= CHROME_DENSE_INK
        and longest <= CHROME_GLYPH_RUN
        and not (edge is CHROME_SIDE and float(np.median(rows.runs[inked])) <= 12)
    ):
        return None  # a caption or a headline
    if (
        center_empty
        and rows.centre[inked].mean() > 0.02
        and not _is_centered_clock_bar(rows, start, end, inked, height)
    ):
        return None  # phone status bars keep the middle clear; captions do not
    spread = rows.sides[start:end].any(axis=0).all()
    runs = float(np.median(rows.runs[inked]))
    # Three buttons after a blank margin are a navigation bar; three shapes
    # touching the edge are drawer knobs or rounded corners. A word across
    # the row is neither: a reply field an earlier crop cut into, its words
    # between its two sides.
    bar_runs = CHROME_BAR_RUNS - 1 if start >= height * CHROME_BAR_MARGIN else CHROME_BAR_RUNS
    if glyphs and spread and (runs >= bar_runs or words):
        return "glyphs"  # a clock on one side, icons on the other; buttons across
    if not pill_allowed:
        return None
    middle = float(np.median(rows.span[inked].mean(axis=1)))
    if runs <= 2 and 0.15 <= longest <= 0.6 and 0.4 <= middle <= 0.6:
        return "pill"
    return None


def _is_dark(colour) -> bool:
    """Whether an RGB colour is as dark as a black bar."""
    import numpy as np

    return float(np.dot(colour, (0.299, 0.587, 0.114))) <= DARK_LUMA


def _marks_a_dark_band(rows, start: int, end: int, height: int, band, edge: ChromeEdge, *,
                       scale: float) -> bool:
    """Whether the first cluster on a black band is a media viewer's marks
    rather than a bar: page dots under the picture, or a counter ("3 / 20"),
    a few letters tall enough to read, close together, no wider than a
    short word. A moon, a star or a light in a night sky is one mark or too
    small; the caller still asks that the band end at the picture's edge."""
    import numpy as np

    if not _is_dark(band):
        return False
    if edge.dots and _is_dots(rows, start, end, height, scale):
        return True
    tall = end - start
    if tall < max(4, int(height * CHROME_GLYPH_ROWS)) or tall > height * edge.cluster_max:
        return False
    if int((rows.two_tone | rows.busy)[start:end].sum()) > max(2, int(0.1 * tall)):
        return False
    glyph = np.flatnonzero(rows.glyph[start:end]) + start
    if glyph.size < 0.5 * tall:
        return False
    if float(rows.span[glyph, 1].max() - rows.span[glyph, 0].min()) > CHROME_COUNTER_WIDTH:
        return False
    if float(np.median(rows.longest[glyph])) > CHROME_LETTER_WIDTH:
        return False
    return float(np.median(rows.runs[glyph])) >= 3


def _hard_edge(rows, row: int, band) -> bool:
    """Whether the picture begins at ``row`` with a straight edge across the
    width, as a letterboxed picture does: the band up to within a JPEG block
    of the row (an edge inside a block rings into the rows beside it), and
    half the width or more far off the band within a few rows. A sky that
    brightens into the scene below crosses the band's tolerance at some row,
    but not by far."""
    import numpy as np

    before = range(row - 1, max(-1, row - 1 - JPEG_BLOCK), -1)
    if not any(
        rows.one_colour[r] and float(np.abs(rows.medians[r] - band).max()) <= CHROME_TOLERANCE
        for r in before
    ):
        return False
    off = np.abs(rows.pixels[row : row + BAR_EDGE_LINES] - band).max(axis=2) >= CHROME_CONTRAST
    return bool((off.mean(axis=1) >= 0.5).any())


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


def _strip_end(rows, start: int, height: int, band, busy_min: int) -> int | None:
    """Where the picture begins past a gallery's filmstrip starting at row
    ``start``, or ``None`` when the rows there are no filmstrip.

    A filmstrip is a row of thumbnails — little pictures of the photos
    around the one shown — a gap of the band apart: rows each holding
    several runs of ink a thumbnail wide, with the gaps between them
    standing in the same columns down the strip. Past it, beyond the
    thumbnails' rounded tops, rows of the band (or of the current photo's
    taller thumbnail alone) set it apart from the picture, which begins at
    the first row holding more. Its band may darken toward the edge, so
    each row's band is the colour near the last one's."""
    import numpy as np

    pixels = rows.pixels
    columns = pixels.shape[1]
    reach = min(len(pixels), start + int(height * CHROME_STRIP_ROWS) + 1)
    low, high = CHROME_STRIP_TILE
    level = np.asarray(band, dtype=np.float64)
    offs, tiles, quiet = [], [], []
    for row in range(start, reach):
        line = pixels[row]
        distance = np.abs(line - level).max(axis=1)
        near = distance <= CHROME_STRIP_DRIFT
        if near.sum() >= 0.02 * columns:
            # Follow a gradient, a level or two a row; a jump is the picture.
            drifted = np.median(line[near], axis=0)
            if float(np.abs(drifted - level).max()) <= CHROME_TOLERANCE / 3:
                level = drifted
                distance = np.abs(line - level).max(axis=1)
        off = distance > CHROME_TOLERANCE
        starts, ends = _runs_of(off)
        widths = (ends - starts) / columns
        sized = (widths >= low) & (widths <= high)
        offs.append(off)
        tiles.append(int(sized.sum()) >= CHROME_STRIP_TILES and widths[sized].sum() >= 0.5 * widths.sum())
        # The band, or the current photo's taller thumbnail alone.
        quiet.append(bool(off.mean() <= 0.12 and not (widths > high).any()))
    tiles, quiet = np.array(tiles, dtype=bool), np.array(quiet, dtype=bool)
    tiled = np.flatnonzero(tiles)
    near_rows = max(3, int(height * 0.03))
    if tiled.size == 0 or tiled[0] > near_rows:
        return None
    # The rows of tiles, but for a row or two that compression or a
    # thumbnail's dark patch breaks.
    end = int(tiled[0])
    for row in tiled[1:]:
        if row - end > 3:
            break
        end = int(row)
    block = tiled[tiled <= end]
    if block.size < height * 0.02:
        return None
    if int(rows.busy[start + int(block[0]) : start + end + 1].sum()) < busy_min:
        return None  # icons or a field, not little pictures
    # Past the thumbnails' rounded tops, the band sets the strip apart.
    gap = np.flatnonzero(quiet[end + 1 :]) + end + 1
    if gap.size == 0:
        return None
    held = np.flatnonzero(~quiet[gap[0] :]) + gap[0]
    if held.size == 0:
        return None
    # The tiles stand in columns: the gaps between them run down the strip
    # (but for a row or two of compression ringing).
    banded = (~np.array([offs[row] for row in block])).sum(axis=0)
    starts, ends = _runs_of(banded < 0.9 * block.size)
    widths = (ends - starts) / columns
    sized = (widths >= low) & (widths <= high)
    if int(sized.sum()) < CHROME_STRIP_TILES or widths[sized].sum() < 0.5 * widths.sum():
        return None
    return start + int(held[0])


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
                 certain: bool, width: int, require_picture: bool = False) -> int:
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
    gap = max(2, int(height * 0.004))
    # An earlier crop may have cut through a shape — a reply field's border —
    # that fills the edge row or more; the band is the first plain row past it.
    plain = np.flatnonzero(rows.one_colour[: gap + 1])
    if rows.ink[0] > 1.0 - CHROME_EDGE_FLAT and not plain.size:
        return 0  # the picture runs to the edge; anything there is drawn over it
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

    band = rows.medians[int(plain[0])] if plain.size else rows.medians[0]
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
            if edge.strip and busy >= busy_min and (clusters or start <= height * edge.first_ink_max):
                # A gallery's filmstrip of the photos around this one is
                # interface, however busy its thumbnails; the picture
                # begins past it.
                beyond = _strip_end(rows, start, height, band, busy_min)
                if beyond is not None:
                    clusters.append(_Cluster(
                        start, beyond, content=False, words=False, text=False, line=False,
                        letters=beyond - start, left=0.0, right=1.0, hole=0.0, ink=0.0,
                    ))
                    first_kind = first_kind or "strip"
                    picture = beyond
                    break
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
                    # A reply field's words, on a story viewer's black surround.
                    words=_is_dark(band) and _has_words(rows, start, end, scale),
                )
                if first_kind is None and _marks_a_dark_band(
                    rows, start, end, height, band, edge, scale=scale
                ):
                    first_kind = "marks"
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
    if require_picture and picture is None and not separators:
        return 0
    if require_picture and not certain and len(clusters) < 2 and first_kind != "pill":
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

    # A media viewer's black band holds its marks and nothing else, and ends
    # at the picture's straight edge.
    if first_kind == "marks" and not (
        picture is not None and len(clusters) == 1 and not switches
        and _hard_edge(rows, picture, band)
    ):
        return 0

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


CHROME_SIDE = ChromeEdge(
    max_fraction=0.25, first_ink_max=0.07, cluster_max=0.08, pill=True, dots=False,
    strip=False,
)


def _chrome_strip(rgb_image, width: int, height: int):
    """Anti-aliased horizontal downsampling of an RGB Pillow image to ``CHROME_SAMPLE_COLUMNS``."""
    import numpy as np
    from PIL import Image

    if width > CHROME_SAMPLE_COLUMNS:
        return np.asarray(
            rgb_image.resize((CHROME_SAMPLE_COLUMNS, height), Image.Resampling.BILINEAR)
        )
    return np.asarray(rgb_image)


def detect_screenshot_chrome(image, *, certain: bool = True) -> tuple[int, int, int, int]:
    """Rows/columns of interface bar ``(top, bottom, left, right)`` on an upright screenshot.

    ``certain`` says the image is known to be a screenshot, by name or
    metadata, rather than merely shaped like one; a lone bar with nothing
    in reach below it is then enough for a cut. Left/right edges are checked
    on landscape images (landscape phone screenshots, desktop sidebars/docks)
    with picture-boundary confirmation.
    """
    from PIL import Image

    flat_rgb = _flattened(image)
    width, height = flat_rgb.size
    if height < 64 or width < 64:
        return 0, 0, 0, 0
    sample = _chrome_strip(flat_rgb, width, height)
    portrait = height >= width * 1.3
    top = _edge_chrome(
        sample, height, CHROME_TOP, center_empty=portrait, certain=certain, width=width
    )
    bottom = _edge_chrome(
        sample[::-1], height, CHROME_BOTTOM, center_empty=False, certain=certain, width=width
    )
    if height - top - bottom < height * MIN_CONTENT_FRACTION:
        top = bottom = 0

    left = right = 0
    inner_h = height - top - bottom
    if width > height and inner_h >= 64:
        inner_img = flat_rgb.crop((0, top, width, height - bottom))
        side_img = inner_img.transpose(Image.Transpose.TRANSPOSE)
        side_sample = _chrome_strip(side_img, inner_h, width)
        left = _edge_chrome(
            side_sample, width, CHROME_SIDE, center_empty=False, certain=certain,
            width=inner_h, require_picture=True,
        )
        right = _edge_chrome(
            side_sample[::-1], width, CHROME_SIDE, center_empty=False, certain=certain,
            width=inner_h, require_picture=True,
        )
        if width - left - right < width * MIN_CONTENT_FRACTION:
            left = right = 0
    return top, bottom, left, right


def _has_candidate_edge(rgb_or_luma) -> bool:
    """Fast progressive peek of the outer 16 lines per edge to short-circuit clean photos."""
    import numpy as np

    height, width = rgb_or_luma.shape[:2]
    if height < 8 or width < 8:
        return False
    is_rgb = rgb_or_luma.ndim == 3
    min_rows = max(MIN_BORDER_PIXELS, int(np.ceil(height * MIN_BORDER_FRACTION)))
    min_cols = max(MIN_BORDER_PIXELS, int(np.ceil(width * MIN_BORDER_FRACTION)))
    reach_v = min(height, max(16, min_rows + 8))
    reach_h = min(width, max(16, min_cols + 8))

    def edge_looks_flat(strip) -> bool:
        across = strip.shape[1]
        margin = max(6, int(across * 0.08))
        # Peek row 0, middle slice of row 0 (for rounded corners), and interior lines
        # past any thin hairline.
        for idx in (0, min(len(strip) - 1, 2), min(len(strip) - 1, 6)):
            line = strip[idx].astype(np.int16)
            med = np.median(line, axis=0)
            diff = np.abs(line - med)
            near = (diff <= AGGRESSIVE_LEVEL_TOLERANCE).all(axis=1) if is_rgb else (
                diff <= AGGRESSIVE_LEVEL_TOLERANCE
            )
            if float(near.mean()) >= 0.65:
                return True
            if across >= margin * 3:
                mid = line[margin : across - margin]
                mid_med = np.median(mid, axis=0)
                mid_diff = np.abs(mid - mid_med)
                mid_near = (mid_diff <= BAR_TOLERANCE).all(axis=1) if is_rgb else (
                    mid_diff <= BAR_TOLERANCE
                )
                if float(mid_near.mean()) >= 0.85:
                    return True
        return False

    t_view = rgb_or_luma.transpose(1, 0, 2) if is_rgb else rgb_or_luma.T
    return any((
        edge_looks_flat(rgb_or_luma[:reach_v]),
        edge_looks_flat(rgb_or_luma[::-1][:reach_v]),
        edge_looks_flat(t_view[:reach_h]),
        edge_looks_flat(t_view[::-1][:reach_h]),
    ))


def _build_provenance(
    *,
    has_chrome: bool,
    strict_black,
    flat_colour,
    loose_colour,
    gradient,
    bars,
    human_guarded: bool,
) -> tuple[str, ...]:
    """Ordered unique provenance tags explaining a CropBox."""
    tags: list[str] = []
    if has_chrome:
        tags.append("chrome")
    if flat_colour is not None or loose_colour is not None or gradient is not None:
        tags.append("colour_bar")
    if strict_black is not None or (
        bars is not None
        and any(bars)
        and flat_colour is None
        and loose_colour is None
        and gradient is None
    ):
        tags.append("black_bar")
    if not tags and bars is not None and any(bars):
        tags.append("black_bar")
    if human_guarded:
        tags.append("human_guarded")
    return tuple(dict.fromkeys(tags))


def _boxes_for(
    upright,
    *,
    chrome: tuple[int, ...] = (0, 0, 0, 0),
    speck: bool = False,
    evidence: str | None = None,
) -> dict[str, CropBox | None]:
    """Combine interface bars with black/colour/gradient bars inside them, one box per level.

    Strict black bars remain in ``proven``, while ``chrome`` and ``colour_bars``
    cuts are checked against the Human Guard so false-positive cuts into faces
    or people are pulled back while screenshots of people inside a post still
    trim cleanly up to the picture edge. Lazy-initializes and runs the human
    guard only when bars or chrome are actually detected.
    """
    import numpy as np

    width, height = upright.size
    if len(chrome) == 2:
        chrome_4 = (chrome[0], chrome[1], 0, 0)
    else:
        chrome_4 = (chrome[0], chrome[1], chrome[2], chrome[3])
    see_through = _see_through(upright)

    if not any(chrome_4) and see_through:
        win = _transparent_window_bars(upright, evidence=evidence)
        if win is not None:
            w_top, w_bottom, w_left, w_right = win
            guard_data = _human_guard_boxes_pair(np.asarray(_flattened(upright))) if _human_guard_available() else None
            guarded = False
            if guard_data and guard_data[0]:
                g_win = _guard_chrome_and_colour(win, guard_data[0], width, height)
                guarded = g_win != win
                w_top, w_bottom, w_left, w_right = g_win
            if not (w_top or w_bottom or w_left or w_right):
                return {name: None for name in CROP_LEVELS}
            if height - w_top - w_bottom < height * MIN_CONTENT_FRACTION:
                return {name: None for name in CROP_LEVELS}
            if width - w_left - w_right < width * MIN_CONTENT_FRACTION:
                return {name: None for name in CROP_LEVELS}
            prov = ("chrome", "human_guarded") if guarded else ("chrome",)
            box = CropBox(
                left=w_left, top=w_top, right=width - w_right, bottom=height - w_bottom,
                width=width, height=height, provenance=prov,
            )
            return {name: (box if box.is_valid() else None) for name in CROP_LEVELS}

    rgb_full = np.asarray(_flattened(upright))
    luma_full = _luma(upright)
    if not any(chrome_4) and not _has_candidate_edge(luma_full if see_through else rgb_full):
        return {name: None for name in CROP_LEVELS}

    def measure_inside(c_tuple):
        ct, cb, cl, cr = c_tuple
        l_in = luma_full[ct : height - cb, cl : width - cr]
        r_in = None if see_through else rgb_full[ct : height - cb, cl : width - cr]
        sp = (speck and not ct, speck and not cb)
        return _level_bars(
            l_in,
            speck=sp,
            loose_allowed=True,
            rgb=r_in,
            interface=(bool(ct), bool(cb)),
            return_details=True,
        )

    levels, details = measure_inside(chrome_4)
    if not any(chrome_4) and levels["aggressive"] is None:
        return {name: None for name in CROP_LEVELS}

    guard_data = _human_guard_boxes_pair(rgb_full) if _human_guard_available() else None
    if guard_data is None:
        # Guard unavailable: fall back to strict passes only (no loose/gradient bars).
        ct, cb, cl, cr = chrome_4
        l_in = luma_full[ct : height - cb, cl : width - cr]
        r_in = None if see_through else rgb_full[ct : height - cb, cl : width - cr]
        sp = (speck and not ct, speck and not cb)
        levels, details = _level_bars(
            l_in, speck=sp, loose_allowed=False, rgb=r_in,
            interface=(bool(ct), bool(cb)), return_details=True,
        )
        raw_entries: list = []
        expanded_boxes: list = []
    else:
        raw_entries, expanded_boxes = guard_data

    kept_chrome = chrome_4
    if any(chrome_4) and raw_entries:
        g_chrome = _guard_chrome_and_colour(chrome_4, raw_entries, width, height)
        min_rows = max(MIN_BORDER_PIXELS, int(height * CHROME_MIN_FRACTION))
        min_cols = max(MIN_BORDER_PIXELS, int(width * CHROME_MIN_FRACTION))
        mins = (min_rows, min_rows, min_cols, min_cols)
        kept_chrome = tuple(
            val if (val == orig or val >= least) else 0
            for val, orig, least in zip(g_chrome, chrome_4, mins)
        )
        if kept_chrome != chrome_4:
            levels, details = measure_inside(kept_chrome)
            if guard_data is None:
                ct, cb, cl, cr = kept_chrome
                l_in = luma_full[ct : height - cb, cl : width - cr]
                r_in = None if see_through else rgb_full[ct : height - cb, cl : width - cr]
                sp = (speck and not ct, speck and not cb)
                levels, details = _level_bars(
                    l_in, speck=sp, loose_allowed=False, rgb=r_in,
                    interface=(bool(ct), bool(cb)), return_details=True,
                )

    strict_black = details["strict_black"] or (0, 0, 0, 0)
    flat_colour = details["flat_colour"] or (0, 0, 0, 0)
    k_top, k_bottom, k_left, k_right = kept_chrome
    # Only untouched chrome edges (where raw faces did not intersect chrome) and
    # strict black bars belong in proven; flat colour extensions and false-positive
    # chrome stay subject to raw-box guard without pulling back valid chrome.
    proven_chrome = tuple(
        kept_chrome[i] if kept_chrome[i] == chrome_4[i] else 0
        for i in range(4)
    )
    strict_floor = tuple(proven_chrome[i] + strict_black[i] for i in range(4))
    base_cut = (
        k_top + flat_colour[0],
        k_bottom + flat_colour[1],
        k_left + flat_colour[2],
        k_right + flat_colour[3],
    )
    guarded_base = (
        _guard_chrome_and_colour(base_cut, raw_entries, width, height)
        if raw_entries
        else base_cut
    )
    floor = tuple(max(a, b) for a, b in zip(guarded_base, strict_floor))

    def finish(level_name: str, bars) -> CropBox | None:
        bars = bars or (0, 0, 0, 0)
        total_cut = (
            k_top + bars[0],
            k_bottom + bars[1],
            k_left + bars[2],
            k_right + bars[3],
        )
        if not any(total_cut):
            return None
        human_guarded = False
        if expanded_boxes:
            guarded_full = _guarded_bars(total_cut, expanded_boxes, width, height)
            final_cut = tuple(max(kept, flr) for kept, flr in zip(guarded_full, floor))
            human_guarded = final_cut != total_cut
        else:
            final_cut = total_cut
        top, bottom, left, right = final_cut
        if not (top or bottom or left or right):
            return None
        if height - top - bottom < height * MIN_CONTENT_FRACTION:
            return None
        if width - left - right < width * MIN_CONTENT_FRACTION:
            return None
        edges = tuple(
            name
            for name, rows_c, kept_c in (
                ("top", k_top, top),
                ("bottom", k_bottom, bottom),
                ("left", k_left, left),
                ("right", k_right, right),
            )
            if rows_c and kept_c >= rows_c
        )
        prov = _build_provenance(
            has_chrome=bool(edges),
            strict_black=details["strict_black"],
            flat_colour=details["flat_colour"],
            loose_colour=details["loose_colour"] if level_name == "aggressive" else None,
            gradient=details["gradient"] if level_name != "conservative" else None,
            bars=bars,
            human_guarded=human_guarded,
        )
        box = CropBox(
            left=left,
            top=top,
            right=width - right,
            bottom=height - bottom,
            width=width,
            height=height,
            chrome=edges,
            provenance=prov,
        )
        return box if box.is_valid() else None

    return _order_level_boxes({name: finish(name, levels[name]) for name in CROP_LEVELS})


def _box_for(upright, *, chrome: tuple[int, ...] = (0, 0), speck: bool = False) -> CropBox | None:
    """The balanced box of :func:`_boxes_for`."""
    return _boxes_for(upright, chrome=chrome, speck=speck)["balanced"]


def detect_crop_levels(
    path: str | Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, CropBox | None]:
    """Suggest the crop for ``path`` at every aggressiveness level.

    Black bars, plus interface bars on screenshots. Non-JPEGs are downscaled to
    ``SCREEN_MAX_SIDE`` for fast screening with progressive edge peeking and
    lazy human-guard loading, avoiding duplicate detection work when already at
    screening resolution.
    """
    from PIL import Image, ImageOps

    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    _register_heif()
    with Image.open(path) as source:
        if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1:
            return {name: None for name in CROP_LEVELS}
        raw_size = source.size
        try:
            orient = source.getexif().get(0x0112, 1)
        except Exception:
            orient = 1
        full_oriented_size = (raw_size[1], raw_size[0]) if orient in (5, 6, 7, 8) else raw_size
        evidence = screenshot_evidence(path, source)
        if evidence is None:
            source.draft("RGB", (SCREEN_MAX_SIDE, SCREEN_MAX_SIDE))
        screened = ImageOps.exif_transpose(source)
        screened.load()
        if evidence is None and max(screened.size) > SCREEN_MAX_SIDE:
            screened.thumbnail((SCREEN_MAX_SIDE, SCREEN_MAX_SIDE), Image.Resampling.BOX)
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    reduced = screened.size != full_oriented_size
    speck = evidence is not None  # a phone's privacy dot sits in a screenshot's bar
    if evidence is not None:
        chrome = detect_screenshot_chrome(screened, certain=evidence != "shape")
        if any(chrome):
            return _boxes_for(screened, chrome=chrome, speck=True, evidence=evidence)
    if not reduced:
        return _boxes_for(screened, speck=speck, evidence=evidence)

    import numpy as np

    see_through = _see_through(screened)
    if see_through and _transparent_window_bars(screened, evidence=evidence) is not None:
        with Image.open(path) as source:
            upright = ImageOps.exif_transpose(source)
            upright.load()
        return _boxes_for(upright, speck=speck, evidence=evidence)
    luma_s = _luma(screened)
    rgb_s = None if see_through else np.asarray(_flattened(screened))
    if not _has_candidate_edge(luma_s if see_through else rgb_s):
        return {name: None for name in CROP_LEVELS}
    screen = _level_bars(
        luma_s,
        speck=speck,
        loose_allowed=True,
        rgb=rgb_s,
    )
    if screen["aggressive"] is None:
        return {name: None for name in CROP_LEVELS}

    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    # The quick pass found bars; measure them accurately at full resolution.
    with Image.open(path) as source:
        upright = ImageOps.exif_transpose(source)
        upright.load()
    boxes = _boxes_for(upright, speck=speck, evidence=evidence)
    if screen["balanced"] is None:
        boxes["balanced"] = None
        boxes["conservative"] = None
    return boxes


def detect_black_borders(path: str | Path) -> CropBox | None:
    """Suggest the crop for ``path``: black bars, plus interface bars on screenshots."""
    return detect_crop_levels(path)["balanced"]


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _probe_video_stream(path: str | Path) -> dict | None:
    """The first video stream's facts, in display orientation.

    ``width``/``height`` are the displayed size: a quarter-turn in the
    stream's rotation side data (or a legacy ``rotate`` tag) swaps the coded
    dimensions, matching what ffmpeg's auto-rotation produces on decode —
    the video counterpart of an image's EXIF transpose. Also reports color
    signaling tags so HDR / wide-gamut streams keep their transfer metadata.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                ("format=duration:stream=duration,width,height,codec_name,pix_fmt,"
                 "color_primaries,color_transfer,color_space,color_range"
                 ":stream_tags=rotate:stream_side_data=rotation"),
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
    except Exception:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    try:
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    duration = None
    for raw in (stream.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            continue
        if candidate > 0:
            duration = candidate
            break
    rotation = 0
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and side.get("rotation") is not None:
            try:
                rotation = round(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = round(float((stream.get("tags") or {}).get("rotate", 0)))
        except (TypeError, ValueError):
            rotation = 0
    if rotation % 360 in (90, 270):
        width, height = height, width
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "codec_name": stream.get("codec_name") or "",
        "pix_fmt": stream.get("pix_fmt") or "",
        "color_primaries": stream.get("color_primaries") or "",
        "color_trc": stream.get("color_transfer") or "",
        "color_space": stream.get("color_space") or "",
        "color_range": stream.get("color_range") or "",
    }


def _video_frame_luma(path: str | Path, timestamp: float):
    """One frame's luma at native (display) size, or ``None`` when unreadable.

    Decoding honours the stream's rotation, as every player does, so the
    frame is upright and bars are measured where the viewer sees them.
    """
    from io import BytesIO

    from PIL import Image

    from .similar_video import _hwaccel_args

    hwaccel = _hwaccel_args()
    for accel in (hwaccel, ()) if hwaccel else ((),):
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            *accel, "-ss", f"{timestamp:.6f}", "-threads", "1",
            "-i", str(path), "-an", "-sn", "-frames:v", "1",
            "-c:v", "ppm", "-f", "image2pipe", "pipe:1",
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=60, check=False)
        except Exception:
            return None
        if result.returncode == 0 and result.stdout.startswith(b"P6"):
            try:
                with Image.open(BytesIO(result.stdout)) as frame:
                    return _luma(frame)
            except Exception:
                return None
    return None


def _video_frame_rgb(path: str | Path, timestamp: float):
    """One frame as a guard-sized RGB array (boxes are fractions, so the
    downscale loses nothing the guard needs)."""
    from io import BytesIO

    import numpy as np
    from PIL import Image

    from .similar_video import _hwaccel_args

    hwaccel = _hwaccel_args()
    for accel in (hwaccel, ()) if hwaccel else ((),):
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            *accel, "-ss", f"{timestamp:.6f}", "-threads", "1",
            "-i", str(path), "-an", "-sn", "-frames:v", "1",
            "-vf", f"scale='min({GUARD_MAX_SIDE},iw)':-2",
            "-c:v", "ppm", "-f", "image2pipe", "pipe:1",
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=60, check=False)
        except Exception:
            return None
        if result.returncode == 0 and result.stdout.startswith(b"P6"):
            try:
                with Image.open(BytesIO(result.stdout)) as frame:
                    return np.asarray(frame.convert("RGB"))
            except Exception:
                return None
    return None


_DEFAULT_VIDEO_FRAME_LUMA = _video_frame_luma
_DEFAULT_VIDEO_FRAME_RGB = _video_frame_rgb


def _parse_ppm_frames(blob: bytes) -> list:
    """Parse concatenated binary ``P6`` PPM images into uint8 RGB arrays."""
    import numpy as np

    frames: list = []
    pos = 0
    total = len(blob)
    ws = b" \t\r\n"
    while pos < total:
        while pos < total and blob[pos : pos + 1] in ws:
            pos += 1
        if pos >= total or blob[pos : pos + 2] != b"P6":
            break
        pos += 2
        tokens: list[int] = []
        while len(tokens) < 3 and pos < total:
            while pos < total and blob[pos : pos + 1] in ws:
                pos += 1
            if pos < total and blob[pos : pos + 1] == b"#":
                nl = blob.find(b"\n", pos)
                pos = total if nl == -1 else nl + 1
                continue
            start = pos
            while pos < total and blob[pos : pos + 1] not in ws and blob[pos : pos + 1] != b"#":
                pos += 1
            if pos == start:
                break
            tokens.append(int(blob[start:pos]))
        if len(tokens) != 3 or pos >= total:
            break
        w, h, maxval = tokens
        if w <= 0 or h <= 0 or maxval != 255:
            break
        pos += 1  # single whitespace byte before raster data
        need = w * h * 3
        if pos + need > total:
            break
        arr = np.frombuffer(blob, dtype=np.uint8, count=need, offset=pos).reshape((h, w, 3)).copy()
        frames.append(arr)
        pos += need
    return frames


def _run_cancellable_command(
    command: list[str],
    *,
    cancelled: Callable[[], bool] | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes, bytes]:
    """Run ``command`` capturing stdout/stderr while honoring cooperative ``cancelled``."""
    import time

    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    if cancelled is None:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        return result.returncode, result.stdout, result.stderr
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def drain(stream, bucket):
        assert stream is not None
        bucket.append(stream.read())

    t_out = threading.Thread(target=drain, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=drain, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancelled():
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            t_out.join(timeout=1)
            t_err.join(timeout=1)
            raise InterruptedError("scan cancelled")
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait(timeout=2)
            t_out.join(timeout=1)
            t_err.join(timeout=1)
            raise subprocess.TimeoutExpired(command, timeout)
        time.sleep(0.02)
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    return proc.returncode, b"".join(out_chunks), b"".join(err_chunks)


def _replace_fps_mode_legacy(command: list[str]) -> list[str]:
    """Replace ``-fps_mode passthrough`` with ``-vsync passthrough`` for older ffmpeg."""
    legacy = list(command)
    for idx in range(len(legacy) - 1):
        if legacy[idx] == "-fps_mode":
            legacy[idx] = "-vsync"
    return legacy


def _single_screened_rgb_frame(
    path: str | Path,
    timestamp: float,
    sw: int,
    sh: int,
    *,
    cancelled: Callable[[], bool] | None = None,
):
    from .similar_video import _hwaccel_args

    hwaccel = _hwaccel_args()
    for accel in (hwaccel, ()) if hwaccel else ((),):
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            *accel, "-ss", f"{timestamp:.6f}", "-threads", "1",
            "-i", str(path), "-an", "-sn", "-frames:v", "1",
            "-vf", f"scale={sw}:{sh}:flags=area,setsar=1,format=rgb24",
            "-c:v", "ppm", "-f", "image2pipe", "pipe:1",
        ]
        try:
            rc, out, _err = _run_cancellable_command(cmd, cancelled=cancelled)
        except InterruptedError:
            raise
        except Exception:
            return None
        if rc == 0 and out.startswith(b"P6"):
            parsed = _parse_ppm_frames(out)
            if parsed and parsed[0].shape[:2] == (sh, sw):
                return parsed[0]
    return None


def _decode_video_sample_frames(
    path: str | Path,
    timestamps: list[float],
    width: int,
    height: int,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list:
    """Decode all sampled frames at screened RGB size in a single ffmpeg process."""
    from .similar_video import _hwaccel_args

    longest = max(width, height)
    if longest > SCREEN_MAX_SIDE:
        scale_f = SCREEN_MAX_SIDE / longest
        sw = max(8, round(width * scale_f))
        sh = max(8, round(height * scale_f))
    else:
        sw, sh = width, height

    hwaccel = _hwaccel_args()
    n = len(timestamps)
    filter_parts = [
        f"[{i}:v]trim=end_frame=1,scale={sw}:{sh}:flags=area,setsar=1,format=rgb24,setpts=N/TB[v{i}]"
        for i in range(n)
    ]
    filter_parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[out]")
    filter_graph = ";".join(filter_parts)

    for accel in (hwaccel, ()) if hwaccel else ((),):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
        for t in timestamps:
            cmd.extend([*accel, "-ss", f"{t:.6f}", "-threads", "1", "-i", str(path)])
        cmd.extend([
            "-filter_complex", filter_graph,
            "-map", "[out]", "-fps_mode", "passthrough",
            "-an", "-sn", "-c:v", "ppm", "-f", "image2pipe", "pipe:1",
        ])
        try:
            rc, out, err = _run_cancellable_command(cmd, cancelled=cancelled)
            if rc != 0 and b"fps_mode" in err.lower():
                rc, out, err = _run_cancellable_command(
                    _replace_fps_mode_legacy(cmd), cancelled=cancelled
                )
        except InterruptedError:
            raise
        except Exception:
            break
        if rc == 0 and out.startswith(b"P6"):
            frames = _parse_ppm_frames(out)
            if len(frames) == n and all(f.shape[:2] == (sh, sw) for f in frames):
                return frames
    return [
        _single_screened_rgb_frame(path, t, sw, sh, cancelled=cancelled)
        for t in timestamps
    ]


def _majority_bars(per_frame) -> tuple[int, int, int, int] | None:
    """Bars the majority of pictured frames carry, at their median thickness.

    ``per_frame`` holds one bar tuple (or ``None`` for a black/unreadable
    frame) per sampled position. Mixed clips — boxed main content with a
    full-frame intro or tail — only accept a crop when strictly more than
    ``VIDEO_MAJORITY`` (70%) of pictured frames share it, so substantial
    full-frame sections (e.g. 40%) are never permanently cropped. Fewer
    than ``VIDEO_MIN_USABLE_FRAMES`` pictured frames prove nothing.
    """
    import numpy as np

    pictured = [bars for bars in per_frame if bars is not None]
    if len(pictured) < VIDEO_MIN_USABLE_FRAMES:
        return None
    edges = []
    for edge in range(4):
        positives = [bars[edge] for bars in pictured if bars[edge] > 0]
        if len(positives) > len(pictured) * VIDEO_MAJORITY:
            edges.append(int(np.median(positives)))
        else:
            edges.append(0)
    return tuple(edges) if any(edges) else None


def _black_frame(luma) -> bool:
    import numpy as np

    return luma is None or (
        float(np.mean(luma > DARK_LUMA + BAR_TOLERANCE)) < VIDEO_BLACK_FRAME_FRACTION
    )


def _blurred_seam_axis(lumas, *, axis: int) -> tuple[int, int] | None:
    """Detect stationary bilateral blurred/mirrored bars along one axis.

    ``axis=1`` checks left/right pillarbox seams; ``axis=0`` checks top/bottom
    letterbox seams. Requires crisp central content, low outer high-frequency
    Laplacian energy on BOTH sides with non-flat macro colour variation (distinct
    from empty solid walls/sky), a sharp straight step aligned across horizontal/
    vertical strips, and stationarity across sampled frames.
    """
    import numpy as np

    ref_shape = lumas[0].shape
    h, w = ref_shape
    dim = w if axis == 1 else h
    cross = h if axis == 1 else w
    min_bar = max(16, int(dim * 0.08))
    max_bar = int(dim * 0.42)
    if min_bar >= max_bar or dim < 64 or cross < 48:
        return None

    laps = []
    diffs = []
    for frame in lumas:
        g = frame.astype(np.int16)
        if axis == 0:
            g = g.T
        c = g[1:-1, 1:-1]
        lap = np.abs(4 * c - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:])
        prof = np.zeros(dim, dtype=np.float32)
        prof[1:-1] = lap.mean(axis=0)
        prof[0] = prof[1]
        prof[-1] = prof[-2]
        laps.append((lap, prof))
        diffs.append(np.abs(g[:, 1:] - g[:, :-1]))

    med_prof = np.median(np.stack([p for _, p in laps], axis=0), axis=0)
    c0, c1 = int(dim * 0.35), int(dim * 0.65)
    e_center = float(np.median(med_prof[c0:c1]))
    if e_center < 4.5:
        return None
    blur_thresh = min(4.2, max(2.6, e_center * 0.28))

    def scan_inward(profile) -> int | None:
        idx = 3
        spikes = 0
        strong_seam = max(12.0, blur_thresh * 3.0)
        while idx < max_bar:
            if profile[idx] > blur_thresh:
                # Require either sustained detail (>=3 cols) or a sharp boundary seam spike.
                run = int(np.sum(profile[idx : min(max_bar + 4, idx + 4)] > blur_thresh))
                if run >= 3 or profile[idx] >= strong_seam:
                    return idx
                spikes += 1
                if spikes > 1:
                    return idx
            idx += 1
        return None

    left = scan_inward(med_prof)
    right = scan_inward(med_prof[::-1])
    if left is None or right is None:
        return None
    if left < min_bar or right < min_bar:
        return None
    if dim - left - right < dim * MIN_CONTENT_FRACTION:
        return None
    if abs(left - right) > max(24, int(dim * 0.12)):
        return None

    # Snap precisely to the strongest seam gradient step within ±5px of threshold crossing.
    med_step = np.median(np.stack([np.median(d, axis=0) for d in diffs], axis=0), axis=0)

    def refine_seam(est: int, from_left: bool) -> int:
        pos = est if from_left else (dim - est)
        lo = max(3, pos - 5)
        hi = min(dim - 4, pos + 5)
        if lo >= hi:
            return est
        window = med_step[lo:hi]
        best = int(np.argmax(window))
        if window[best] >= 5.0:
            col = lo + best + (1 if from_left else 0)
            return col if from_left else (dim - col)
        return est

    left = refine_seam(left, True)
    right = refine_seam(right, False)
    if left < min_bar or right < min_bar or dim - left - right < dim * MIN_CONTENT_FRACTION:
        return None

    # Verify outer blurred mean Laplacian, sharp inner/outer energy contrast, and macro std.
    margin = max(4, min(12, min_bar // 4))
    inner_zone = med_prof[left + 2 : dim - right - 2]
    if not len(inner_zone):
        return None
    e_in_mean = float(np.mean(inner_zone))
    e_out_l = float(np.mean(med_prof[margin : max(margin + 1, left - 3)]))
    e_out_r = float(np.mean(med_prof[dim - right + 3 : max(dim - right + 4, dim - margin)]))
    if e_out_l > 3.5 or e_out_r > 3.5:
        return None
    if e_in_mean / max(e_out_l, 0.35) < 3.5 or e_in_mean / max(e_out_r, 0.35) < 3.5:
        return None

    # Distinguish blurred footage from solid walls/clear sky: blurred bar varies across frame.
    outer_l = [f[:, : left - 3] if axis == 1 else f[: left - 3, :] for f in lumas]
    outer_r = [f[:, dim - right + 3 :] if axis == 1 else f[dim - right + 3 :, :] for f in lumas]
    if max(float(np.std(outer_l[0])), float(np.std(outer_r[0]))) < 3.0:
        return None

    # Strip-alignment check: at least 3 of 4 orthogonal strips transition cleanly near seam.
    lap_med = np.median(np.stack([lp for lp, _ in laps], axis=0), axis=0)
    sh_lap = lap_med.shape[0]
    strip_tol = max(6, int(dim * 0.02))
    l_strips: list[int] = []
    r_strips: list[int] = []
    for q in range(4):
        r0 = int(sh_lap * (0.1 + 0.2 * q))
        r1 = int(sh_lap * (0.3 + 0.2 * q))
        if r1 <= r0:
            continue
        sp = np.zeros(dim, dtype=np.float32)
        sp[1:-1] = lap_med[r0:r1].mean(axis=0)
        sl = scan_inward(sp)
        sr = scan_inward(sp[::-1])
        if sl is not None and abs(sl - left) <= strip_tol:
            l_strips.append(sl)
        if sr is not None and abs(sr - right) <= strip_tol:
            r_strips.append(sr)
    if len(l_strips) < 3 or len(r_strips) < 3:
        return None

    # Per-frame stationarity across at least VIDEO_MAJORITY of pictured frames.
    good_frames = 0
    for _, prof in laps:
        fl = scan_inward(prof)
        fr = scan_inward(prof[::-1])
        if fl is not None and fr is not None and abs(fl - left) <= strip_tol and abs(fr - right) <= strip_tol:
            good_frames += 1
    if good_frames <= len(lumas) * VIDEO_MAJORITY:
        return None
    return (left, right)


def _detect_blurred_video_bars(lumas) -> tuple[int, int, int, int] | None:
    """Detect stationary bilateral blurred/mirrored pillarbox or letterbox video bars."""
    pictured = [frame for frame in lumas if not _black_frame(frame)]
    if len(pictured) < VIDEO_MIN_USABLE_FRAMES:
        return None
    shapes = {frame.shape for frame in pictured}
    if len(shapes) != 1:
        return None
    lr = _blurred_seam_axis(pictured, axis=1)
    if lr is not None:
        return (0, 0, lr[0], lr[1])
    tb = _blurred_seam_axis(pictured, axis=0)
    if tb is not None:
        return (tb[0], tb[1], 0, 0)
    return None


def _detect_static_video_ui_bars(rgbs, lumas) -> tuple[int, int, int, int] | None:
    """Detect stationary screen-recording UI bars over a changing interior video.

    Requires temporal constancy (``std(frames, axis=0) <= 2.5`` along outer
    rows), real motion inside the kept picture (rejecting static clips), AND
    spatial interface structure from ``detect_screenshot_chrome`` so tripod sky/
    wall clips with static edges are never misclassified as screen recordings.
    """
    import numpy as np
    from PIL import Image

    usable_indices = [
        idx
        for idx, (rgb, luma) in enumerate(zip(rgbs, lumas))
        if rgb is not None
        and isinstance(rgb, np.ndarray)
        and rgb.ndim == 3
        and not _black_frame(luma)
        and rgb.shape[:2] == lumas[idx].shape[:2]
    ]
    if len(usable_indices) < VIDEO_MIN_USABLE_FRAMES:
        return None
    shapes = {rgbs[i].shape for i in usable_indices}
    if len(shapes) != 1:
        return None
    stack = np.stack([rgbs[i] for i in usable_indices], axis=0).astype(np.float32)
    _, height, _width, _ = stack.shape
    temp_std = np.std(stack, axis=0).max(axis=2)
    row_static = (temp_std <= 2.5).mean(axis=1)

    max_band = int(height * 0.40)

    def walk_static(rows) -> int:
        band = 0
        gaps = 0
        for row_frac in rows[:max_band]:
            if row_frac >= 0.90:
                band += 1
                gaps = 0
            elif gaps < 2 and row_frac >= 0.78:
                gaps += 1
                band += 1
            else:
                break
        return band - gaps

    static_top = walk_static(row_static)
    static_bottom = walk_static(row_static[::-1])
    min_rows = max(MIN_BORDER_PIXELS, int(height * CHROME_MIN_FRACTION))
    if static_top < min_rows and static_bottom < min_rows:
        return None
    if static_top + static_bottom > height * 0.45:
        return None
    interior_std = temp_std[static_top : height - static_bottom]
    if interior_std.size == 0 or float((interior_std >= 4.0).mean()) < 0.15:
        return None

    med_rgb = np.median(stack, axis=0).astype(np.uint8)
    chrome = detect_screenshot_chrome(Image.fromarray(med_rgb), certain=True)
    c_top, c_bottom = chrome[0], chrome[1]
    keep_top = (
        min(c_top, static_top + 2)
        if static_top >= min_rows and c_top >= min_rows and c_top <= static_top + 6
        else 0
    )
    keep_bottom = (
        min(c_bottom, static_bottom + 2)
        if static_bottom >= min_rows and c_bottom >= min_rows and c_bottom <= static_bottom + 6
        else 0
    )
    if not (keep_top or keep_bottom):
        return None
    if height - keep_top - keep_bottom < height * MIN_CONTENT_FRACTION:
        return None
    return (keep_top, keep_bottom, 0, 0)


def _frame_bars(lumas, *, aggressive: bool):
    """Per-frame bar tuples; ``None`` where a frame is black or unreadable."""
    return [
        None if _black_frame(luma) else _combined_bars(luma, aggressive=aggressive) or (0, 0, 0, 0)
        for luma in lumas
    ]


def _frame_level_bars(lumas, *, loose_allowed: bool, rgbs=None, return_details: bool = False):
    """Per-level majority bars over pictured frames (with optional flat colour bars).

    Each level takes its own majority, then strict monotonic merging from
    conservative -> balanced -> aggressive so higher levels never cut less than
    lower levels even when medians form over slightly different frame subsets.
    """
    import numpy as np

    per_level: dict[str, list] = {name: [] for name in CROP_LEVELS}
    per_strict_black: list = []
    per_flat_colour: list = []
    per_loose_colour: list = []
    for idx, luma in enumerate(lumas):
        if _black_frame(luma):
            for frames in per_level.values():
                frames.append(None)
            per_strict_black.append(None)
            per_flat_colour.append(None)
            per_loose_colour.append(None)
            continue
        rgb = None
        if loose_allowed and rgbs is not None:
            candidate = rgbs[idx]
            if (
                isinstance(candidate, np.ndarray)
                and candidate.ndim == 3
                and candidate.shape[:2] == luma.shape[:2]
            ):
                rgb = candidate
        levels, details = _level_bars(
            luma, loose_allowed=loose_allowed, rgb=rgb, return_details=True
        )
        for name in CROP_LEVELS:
            per_level[name].append(levels[name] or (0, 0, 0, 0))
        per_strict_black.append(details["strict_black"] or (0, 0, 0, 0))
        per_flat_colour.append(details["flat_colour"] or (0, 0, 0, 0))
        per_loose_colour.append(details["loose_colour"] or (0, 0, 0, 0))
    bars: dict[str, tuple[int, int, int, int] | None] = {
        name: _majority_bars(per_level[name]) for name in CROP_LEVELS
    }
    if bars["conservative"] is not None:
        bars["balanced"] = (
            bars["conservative"]
            if bars["balanced"] is None
            else tuple(max(a, b) for a, b in zip(bars["balanced"], bars["conservative"]))
        )
    if bars["balanced"] is not None:
        bars["aggressive"] = (
            bars["balanced"]
            if bars["aggressive"] is None
            else tuple(max(a, b) for a, b in zip(bars["aggressive"], bars["balanced"]))
        )
    if return_details:
        return bars, {
            "strict_black": _majority_bars(per_strict_black),
            "flat_colour": _majority_bars(per_flat_colour),
            "loose_colour": _majority_bars(per_loose_colour),
        }
    return bars


def _merge_crop_box(
    base: CropBox,
    other: CropBox,
    *,
    tighter: bool,
    merge_provenance: bool = False,
) -> CropBox | None:
    """Clamp or raise ``base`` against ``other`` on the same frame dimensions."""
    if tighter:
        left = max(base.left, other.left)
        top = max(base.top, other.top)
        right = min(base.right, other.right)
        bottom = min(base.bottom, other.bottom)
    else:
        left = min(base.left, other.left)
        top = min(base.top, other.top)
        right = max(base.right, other.right)
        bottom = max(base.bottom, other.bottom)
    width, height = base.width, base.height
    if (
        right - left < width * MIN_CONTENT_FRACTION
        or bottom - top < height * MIN_CONTENT_FRACTION
    ):
        return None
    edges = tuple(
        edge
        for edge, cut in (
            ("top", top),
            ("bottom", height - bottom),
            ("left", left),
            ("right", width - right),
        )
        if cut > 0 and (edge in base.chrome or edge in other.chrome)
    )
    prov = (
        tuple(dict.fromkeys((*base.provenance, *other.provenance)))
        if merge_provenance
        else base.provenance
    )
    box = CropBox(
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        width=width,
        height=height,
        chrome=edges,
        provenance=prov,
    )
    return box if box.is_valid() else None


def _order_level_boxes(boxes: dict[str, CropBox | None]) -> dict[str, CropBox | None]:
    """Enforce strict monotonicity ``conservative <= balanced <= aggressive`` on all 4 edges."""
    out = dict(boxes)
    conservative = out["conservative"]
    balanced = out["balanced"]
    aggressive = out["aggressive"]
    if balanced is None and conservative is not None:
        balanced = conservative
    if conservative is not None and balanced is not None:
        conservative = _merge_crop_box(conservative, balanced, tighter=False, merge_provenance=False)
    if balanced is not None:
        if aggressive is None:
            aggressive = balanced
        else:
            raised = _merge_crop_box(aggressive, balanced, tighter=True, merge_provenance=True)
            aggressive = raised if raised is not None else balanced
    out["conservative"] = conservative
    out["balanced"] = balanced
    out["aggressive"] = aggressive
    return out


def _screened(luma):
    """``luma`` shrunk to the quick-check size, like an image's first pass."""
    import numpy as np
    from PIL import Image

    height, width = luma.shape[:2]
    longest = max(width, height)
    if longest <= SCREEN_MAX_SIDE:
        return luma
    scale = SCREEN_MAX_SIDE / longest
    shrunk = Image.fromarray(luma).resize(
        (max(8, round(width * scale)), max(8, round(height * scale))),
        resample=Image.Resampling.BOX,
    )
    return np.asarray(shrunk)


def detect_video_crop_levels(
    path: str | Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, CropBox | None]:
    """Suggest the crop for a video at every level: majority bars per level.

    Samples all 5 frames in one batched ``ffmpeg`` decode at screening
    resolution (unless a test monkeypatched ``_video_frame_luma`` /
    ``_video_frame_rgb``), enables flat colour bars, blurred/mirrored bars,
    and temporal-constancy static UI bars behind a lazy Human Guard, and
    keeps every level monotonically ordered with structured provenance.
    """
    import numpy as np
    from PIL import Image

    empty = {name: None for name in CROP_LEVELS}
    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")
    if not _ffmpeg_available():
        return empty
    probe = _probe_video_stream(path)
    if probe is None or not probe["duration"]:
        return empty
    width, height = probe["width"], probe["height"]
    timestamps = [probe["duration"] * fraction for fraction in VIDEO_SAMPLE_FRACTIONS]

    custom_video_frames = (
        _video_frame_luma is not _DEFAULT_VIDEO_FRAME_LUMA
        or _video_frame_rgb is not _DEFAULT_VIDEO_FRAME_RGB
    )
    batched_rgbs: list | None = None
    if not custom_video_frames:
        batched_rgbs = _decode_video_sample_frames(
            path, timestamps, width, height, cancelled=cancelled
        )
        frames = [
            np.asarray(Image.fromarray(rgb).convert("L")) if rgb is not None else None
            for rgb in batched_rgbs
        ]
    else:
        raw_lumas = [_video_frame_luma(path, stamp) for stamp in timestamps]
        frames = [
            frame if frame is not None and frame.shape[:2] == (height, width) else None
            for frame in raw_lumas
        ]

    # Early exit when no pictured frames were decoded.
    first_shape = next((f.shape[:2] for f in frames if f is not None), None)
    if first_shape is None:
        return empty
    fh, fw = first_shape

    loose_allowed = _human_guard_available()
    screened_lumas = [
        _screened(frame) if frame is not None and not custom_video_frames else (
            _screened(frame) if frame is not None else None
        )
        for frame in frames
    ]
    if batched_rgbs is not None:
        screened_rgbs = batched_rgbs
        level_bars, details = _frame_level_bars(
            frames, loose_allowed=loose_allowed, rgbs=batched_rgbs, return_details=True
        )
    else:
        screened_bars = _frame_level_bars(screened_lumas, loose_allowed=loose_allowed)
        level_bars, details = _frame_level_bars(
            frames, loose_allowed=loose_allowed, rgbs=None, return_details=True
        )
        if screened_bars["balanced"] is None:
            level_bars["balanced"] = None
            level_bars["conservative"] = None
        if screened_bars["aggressive"] is None:
            level_bars["aggressive"] = None
        screened_rgbs = None

    blurred = _detect_blurred_video_bars(frames) if loose_allowed else None
    static_ui = (
        _detect_static_video_ui_bars(screened_rgbs, frames)
        if (loose_allowed and screened_rgbs is not None)
        else None
    )

    if level_bars["aggressive"] is None and blurred is None and static_ui is None:
        return empty

    if cancelled and cancelled():
        raise InterruptedError("scan cancelled")

    extra_bars = _merge_bars_monotone(blurred, static_ui, fw, fh)
    if extra_bars is not None:
        level_bars["balanced"] = _merge_bars_monotone(level_bars["balanced"], extra_bars, fw, fh)
        level_bars["aggressive"] = _merge_bars_monotone(
            level_bars["aggressive"], extra_bars, fw, fh
        )
    if level_bars["conservative"] is not None:
        level_bars["balanced"] = _merge_bars_monotone(
            level_bars["conservative"], level_bars["balanced"], fw, fh
        )
    if level_bars["balanced"] is not None:
        level_bars["aggressive"] = _merge_bars_monotone(
            level_bars["balanced"], level_bars["aggressive"], fw, fh
        )

    # Scale bar coordinates back to native display dimensions (width, height).
    scale_x = width / fw
    scale_y = height / fh

    def to_native(bars):
        if bars is None:
            return None
        t, b, l, r = bars
        if fw == width and fh == height:
            return bars
        return (
            round(t * scale_y),
            round(b * scale_y),
            round(l * scale_x),
            round(r * scale_x),
        )

    native_levels = {name: to_native(level_bars[name]) for name in CROP_LEVELS}
    native_blurred = to_native(blurred)
    native_static_ui = to_native(static_ui)
    native_strict = to_native(details["strict_black"])
    native_flat_colour = to_native(details["flat_colour"])
    native_loose_colour = to_native(details["loose_colour"])

    unguarded_levels = dict(native_levels)
    boxes: list[tuple[float, float, float, float]] = []
    if loose_allowed:
        guard_failed = False
        for idx, (stamp, frame) in enumerate(zip(timestamps, frames)):
            if cancelled and cancelled():
                raise InterruptedError("scan cancelled")
            if frame is None:
                guard_failed = True
                continue
            rgb = (
                batched_rgbs[idx]
                if batched_rgbs is not None
                else _video_frame_rgb(path, stamp)
            )
            found = _human_guard_boxes(rgb) if rgb is not None else None
            if found is None:
                guard_failed = True
            else:
                boxes.extend(found)
        if guard_failed:
            # Strict fallback honors people from frames whose guard succeeded; drop
            # loose/blurred/UI/colour suggestions that lacked complete guard coverage.
            strict_only = _frame_level_bars(frames, loose_allowed=False, rgbs=None)
            native_levels = {name: to_native(strict_only[name]) for name in CROP_LEVELS}
            unguarded_levels = dict(native_levels)
            native_blurred = None
            native_static_ui = None
            native_flat_colour = None
            native_loose_colour = None
            if native_levels["aggressive"] is None:
                return empty
        if boxes:
            for name in CROP_LEVELS:
                if native_levels[name] is not None:
                    guarded = _guarded_bars(native_levels[name], boxes, width, height)
                    native_levels[name] = guarded if any(guarded) else None
            if native_levels["aggressive"] is None:
                return empty

    direction = -1 if boxes else 1

    def finish(level_name: str, bars) -> CropBox | None:
        if bars is None:
            return None
        top, bottom, left, right = bars
        right, bottom = width - right, height - bottom
        left_e = left + direction * (left % 2)
        top_e = top + direction * (top % 2)
        right_e = right - direction * (right % 2)
        bottom_e = bottom - direction * (bottom % 2)
        if not (left_e or top_e or (width - right_e) or (height - bottom_e)):
            return None
        ui = native_static_ui if level_name != "conservative" else None
        ui_edges: tuple[str, ...] = ()
        if ui is not None:
            ui_edges = tuple(
                edge
                for edge, u_cut, f_cut in (
                    ("top", ui[0], top_e),
                    ("bottom", ui[1], height - bottom_e),
                )
                if u_cut > 0 and f_cut >= u_cut
            )
        blur = native_blurred if level_name != "conservative" else None
        prov_tags: list[str] = []
        if native_strict is not None and any(native_strict):
            prov_tags.append("black_bar")
        if native_flat_colour is not None or (
            level_name == "aggressive" and native_loose_colour is not None
        ):
            prov_tags.append("colour_bar")
        if blur is not None and any(blur):
            prov_tags.append("blurred_bar")
        if ui_edges:
            prov_tags.append("chrome")
        if not prov_tags:
            prov_tags.append("black_bar")
        unguarded = unguarded_levels.get(level_name)
        if boxes and unguarded is not None and any(
            u > 0 and s < u
            for u, s in zip(unguarded, (top_e, height - bottom_e, left_e, width - right_e))
        ):
            prov_tags.append("human_guarded")
        box = CropBox(
            left=left_e,
            top=top_e,
            right=right_e,
            bottom=bottom_e,
            width=width,
            height=height,
            chrome=ui_edges,
            provenance=tuple(dict.fromkeys(prov_tags)),
        )
        if not box.is_valid():
            return None
        if box.crop_height < height * MIN_CONTENT_FRACTION:
            return None
        if box.crop_width < width * MIN_CONTENT_FRACTION:
            return None
        return box

    return _order_level_boxes({name: finish(name, native_levels[name]) for name in CROP_LEVELS})


def detect_video_borders(path: str | Path) -> CropBox | None:
    """Suggest the crop for a video: the bars most sampled frames carry."""
    return detect_video_crop_levels(path)["balanced"]


def _needs_border_measure(record: FileRecord, level: str) -> bool:
    """Whether a scan at ``level`` still has to measure ``record``.

    A measurement fills every level at once. Records measured before
    levels existed carry only a balanced verdict: a balanced or
    conservative scan trusts it (balanced-clean settles conservative too)
    and re-measures just the candidates, so the review toggle has their
    other boxes; an aggressive scan cannot trust a balanced "clean" and
    measures everything it has no aggressive verdict for. A dismissal
    settles every level and is never re-measured.
    """
    if record.border_crop == BORDER_DISMISSED:
        return False
    if record.border_signature != BORDER_SIGNATURE:
        return True
    if record.border_levels_signature == BORDER_LEVELS_SIGNATURE:
        return False
    if level == "aggressive":
        return True
    return bool(record.border_crop)  # a candidate: its other levels are wanted


def analyze_borders(
    records: list[FileRecord],
    *,
    level: str = DEFAULT_CROP_LEVEL,
    workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Fill the crop verdicts for every croppable record a scan needs.

    ``level`` is the scan's aggressiveness: it decides which cached
    verdicts are still missing (see :func:`_needs_border_measure`), never
    what gets measured — one measurement computes every level's box.
    Returns per-file error strings.
    """
    level = validate_crop_level(level)
    for record in records:
        if (
            record.border_signature in LEGACY_BORDER_SIGNATURES
            and record.border_crop == BORDER_DISMISSED
        ):
            record.border_signature = BORDER_SIGNATURE  # "no crop" still stands
    # Without ffmpeg a video cannot be checked; leaving its verdict unset
    # (rather than "clean") keeps it eligible once ffmpeg is installed.
    videos_ok = _ffmpeg_available()
    pending = [
        record
        for record in records
        if is_croppable(record)
        and _needs_border_measure(record, level)
        and (record.media_type != MediaType.VIDEO or videos_ok)
    ]
    if not pending:
        return []

    def analyze(record: FileRecord) -> str | None:
        if cancelled and cancelled():
            raise InterruptedError("scan cancelled")
        try:
            if record.media_type == MediaType.VIDEO:
                boxes = (
                    detect_video_crop_levels(record.path, cancelled=cancelled)
                    if cancelled is not None
                    else detect_video_crop_levels(record.path)
                )
            else:
                boxes = (
                    detect_crop_levels(record.path, cancelled=cancelled)
                    if cancelled is not None
                    else detect_crop_levels(record.path)
                )
        except InterruptedError:
            raise
        except Exception as exc:
            return f"border check failed for {record.path}: {exc}"

        def encode(name: str) -> str:
            box = boxes[name]
            return box.encode() if box else ""

        record.border_crop = encode("balanced")
        record.border_signature = BORDER_SIGNATURE
        record.border_crop_conservative = encode("conservative")
        record.border_crop_aggressive = encode("aggressive")
        record.border_levels_signature = BORDER_LEVELS_SIGNATURE
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


_STD_LUMA_Q50 = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)
_EXIF_PIXEL_DIM_TAGS = (0xA002, 0xA003, 0x0100, 0x0101)
_TIFF_SKIP_TAGS = frozenset({
    256, 257, 258, 259, 262, 273, 274, 277, 278, 279, 284,
    317, 322, 323, 324, 325, 338, 347, 513, 514,
    34665, 34853, 40962, 40963,
})


@functools.cache
def _ijg_luma_tables() -> tuple[tuple[int, tuple[int, ...]], ...]:
    tables = []
    for q in range(1, 101):
        scale = 5000 // q if q < 50 else 200 - 2 * q
        table = tuple(max(1, min(255, (base * scale + 50) // 100)) for base in _STD_LUMA_Q50)
        tables.append((q, table))
    return tuple(tables)


def _estimate_jpeg_quality(source) -> int:
    """Estimate source JPEG quality from its luma quantization table."""
    qtables = getattr(source, "quantization", None) or {}
    luma_q = qtables.get(0)
    if not luma_q or len(luma_q) != 64:
        return JPEG_SAVE_QUALITY
    best_q = JPEG_SAVE_QUALITY
    best_err = None
    for q, table in _ijg_luma_tables():
        err = sum(abs(a - b) for a, b in zip(luma_q, table))
        if best_err is None or err < best_err:
            best_err = err
            best_q = q
            if err == 0:
                break
    return max(50, min(96, best_q))


def _sanitize_exif(upright, source=None) -> bytes | None:
    """Serialize EXIF reset to upright with stale Pixel*Dimension and IFD1 removed."""
    try:
        exif = upright.getexif()
        if not exif and source is not None:
            exif = source.getexif()
        if not exif:
            return None
        exif[0x0112] = 1
        for tag in (*_EXIF_PIXEL_DIM_TAGS, 0x0201, 0x0202):
            exif.pop(tag, None)
        if 0x8769 in exif:
            sub = exif.get_ifd(0x8769)
            for tag in _EXIF_PIXEL_DIM_TAGS:
                sub.pop(tag, None)
        ifds = getattr(exif, "_ifds", None)
        if isinstance(ifds, dict):
            ifds.pop(1, None)
        ifd1 = exif.get_ifd(1)
        if ifd1:
            ifd1.clear()
        return exif.tobytes()
    except Exception:
        return None


def _is_lossless_webp(path: Path) -> bool:
    """True when ``path`` holds a WebP VP8L lossless bitstream without lossy VP8."""
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return b"VP8L" in head and b"VP8 " not in head


def _jpeg_app13_segment(source) -> bytes | None:
    payload = (getattr(source, "app", None) or {}).get("APP13")
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        return None
    raw = bytes(payload)
    if len(raw) + 2 > 0xFFFF:
        return None
    return b"\xff\xed" + (len(raw) + 2).to_bytes(2, "big") + raw


def _png_info_from_source(source):
    from PIL import PngImagePlugin

    pnginfo = PngImagePlugin.PngInfo()
    has_entries = False
    has_xmp = False
    text = getattr(source, "text", None) or {}
    for key, value in text.items():
        if key in ("icc_profile", "exif") or not isinstance(value, str):
            continue
        if key.lower() == "xml:com.adobe.xmp" or not value.isascii():
            pnginfo.add_itxt(key, value)
        else:
            pnginfo.add_text(key, value)
        has_entries = True
        if key.lower() == "xml:com.adobe.xmp":
            has_xmp = True
    xmp = source.info.get("xmp")
    if xmp and not has_xmp:
        xmp_text = xmp.decode("utf-8", errors="replace") if isinstance(xmp, bytes) else str(xmp)
        pnginfo.add_itxt("XML:com.adobe.xmp", xmp_text)
        has_entries = True
    return pnginfo if has_entries else None


def _tiff_info_from_source(upright, source):
    from PIL.TiffImagePlugin import ImageFileDirectory_v2

    ifd = ImageFileDirectory_v2()
    has_entries = False
    try:
        exif = upright.getexif() or source.getexif()
    except Exception:
        exif = None
    if exif:
        for tag, value in exif.items():
            if tag in _TIFF_SKIP_TAGS:
                continue
            if isinstance(value, (dict, list, tuple)):
                continue
            ifd[tag] = value
            has_entries = True
    tag_v2 = getattr(source, "tag_v2", None)
    for meta_tag in (700, 33723, 34377):
        if tag_v2 is not None and meta_tag in tag_v2:
            val = tag_v2[meta_tag]
            if isinstance(val, (bytes, str)):
                ifd[meta_tag] = val.encode("utf-8") if (meta_tag == 700 and isinstance(val, str)) else val
                has_entries = True
    xmp = source.info.get("xmp")
    if xmp and 700 not in ifd:
        ifd[700] = xmp if isinstance(xmp, bytes) else str(xmp).encode("utf-8")
        has_entries = True
    return ifd if has_entries else None


def _splice_jpeg_exif(jpeg_bytes: bytes, exif_bytes: bytes | None) -> bytes:
    """Replace any Exif APP1 segment in ``jpeg_bytes`` with ``exif_bytes``, keeping ICC/XMP/IPTC."""
    if not jpeg_bytes.startswith(b"\xff\xd8"):
        return jpeg_bytes
    out = [b"\xff\xd8"]
    if exif_bytes:
        out.append(b"\xff\xe1" + (len(exif_bytes) + 2).to_bytes(2, "big") + exif_bytes)
    pos = 2
    n = len(jpeg_bytes)
    while pos < n:
        if jpeg_bytes[pos] != 0xFF:
            out.append(jpeg_bytes[pos:])
            break
        while pos < n and jpeg_bytes[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = jpeg_bytes[pos]
        pos += 1
        if marker == 0xDA or marker == 0xD9:
            out.append(b"\xff" + bytes([marker]) + jpeg_bytes[pos:])
            break
        if marker == 0x00 or (0xD0 <= marker <= 0xD7):
            out.append(b"\xff" + bytes([marker]))
            continue
        if pos + 2 > n:
            out.append(b"\xff" + bytes([marker]) + jpeg_bytes[pos:])
            break
        seg_len = int.from_bytes(jpeg_bytes[pos : pos + 2], "big")
        if seg_len < 2 or pos + seg_len > n:
            out.append(jpeg_bytes[pos - 2 :])
            break
        payload = jpeg_bytes[pos + 2 : pos + seg_len]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            pos += seg_len
            continue
        out.append(b"\xff" + bytes([marker]) + jpeg_bytes[pos : pos + seg_len])
        pos += seg_len
    return b"".join(out)


def _try_lossless_jpegtran(
    path: Path,
    box: CropBox,
    destination: Path,
    *,
    upright_already: bool,
    sampling: int,
    progressive: bool,
    exif_bytes: bytes | None,
) -> bool:
    """Attempt lossless MCU-aligned crop with ``jpegtran``; return False on fallback."""
    jpegtran = shutil.which("jpegtran")
    if not jpegtran or not upright_already:
        return False
    mcu_w, mcu_h = {0: (8, 8), 1: (16, 8), 2: (16, 16)}.get(sampling, (16, 16))
    if box.left % mcu_w != 0 or box.top % mcu_h != 0:
        return False
    cmd = [
        jpegtran,
        "-perfect",
        "-optimize",
        "-copy",
        "all",
    ]
    if progressive:
        cmd.append("-progressive")
    cmd.extend([
        "-crop",
        f"{box.crop_width}x{box.crop_height}+{box.left}+{box.top}",
        str(path),
    ])
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except Exception:
        return False
    if res.returncode != 0 or not res.stdout.startswith(b"\xff\xd8"):
        return False
    cleaned = _splice_jpeg_exif(res.stdout, exif_bytes)
    try:
        destination.write_bytes(cleaned)
        from PIL import Image, ImageOps

        with Image.open(destination) as check:
            written = ImageOps.exif_transpose(check).size
        if written == (box.crop_width, box.crop_height):
            return True
    except Exception:
        pass
    destination.unlink(missing_ok=True)
    return False


def _save_kwargs(source, upright, image_format: str, *, path: Path | None = None) -> dict:
    """Save options keeping format, color profile, XMP/IPTC/PNG/TIFF metadata, and quality."""
    kwargs: dict = {}
    icc = source.info.get("icc_profile")
    if icc:
        kwargs["icc_profile"] = icc
    dpi = source.info.get("dpi")
    if dpi:
        kwargs["dpi"] = dpi
    exif_bytes = _sanitize_exif(upright, source)
    if exif_bytes:
        kwargs["exif"] = exif_bytes
    xmp = source.info.get("xmp")
    if xmp:
        kwargs["xmp"] = xmp if isinstance(xmp, bytes) else str(xmp).encode("utf-8")

    if image_format == "JPEG":
        from PIL import JpegImagePlugin

        kwargs["quality"] = _estimate_jpeg_quality(source)
        sampling = JpegImagePlugin.get_sampling(source)
        if sampling != -1:
            kwargs["subsampling"] = sampling
        if source.info.get("progressive") or source.info.get("progression"):
            kwargs["progressive"] = True
        app13 = _jpeg_app13_segment(source)
        if app13:
            kwargs["extra"] = app13
    elif image_format == "PNG":
        kwargs.pop("xmp", None)
        pnginfo = _png_info_from_source(source)
        if pnginfo is not None:
            kwargs["pnginfo"] = pnginfo
    elif image_format == "WEBP":
        if path is not None and _is_lossless_webp(path):
            kwargs["lossless"] = True
        else:
            kwargs["quality"] = LOSSY_SAVE_QUALITY
    elif image_format in ("HEIF", "AVIF"):
        kwargs["quality"] = LOSSY_SAVE_QUALITY
    elif image_format == "TIFF":
        compression = source.info.get("compression")
        tiffinfo = _tiff_info_from_source(upright, source)
        if tiffinfo is not None:
            kwargs["tiffinfo"] = tiffinfo
        kwargs.pop("xmp", None)
        if compression and compression != "raw":
            kwargs["compression"] = compression
            # libtiff rejects sub-IFD dicts from raw EXIF on compressed TIFFs; tiffinfo keeps IFD0/XMP/IPTC.
            kwargs.pop("exif", None)
    elif image_format == "BMP":
        kwargs.pop("exif", None)
        kwargs.pop("icc_profile", None)
        kwargs.pop("xmp", None)
    return kwargs


def write_cropped_copy(path: Path, box: CropBox, destination: Path) -> tuple[int, int]:
    """Write ``path`` cropped to ``box`` at ``destination``; return its size."""
    from PIL import Image, ImageOps

    _register_heif()
    with Image.open(path) as source:
        if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1:
            raise ValueError("cannot crop multi-frame or animated image without discarding frames")
        image_format = source.format or ""
        try:
            orient = source.getexif().get(0x0112, 1)
        except Exception:
            orient = 1
        upright = ImageOps.exif_transpose(source)
        upright.load()
        if upright.size != (box.width, box.height):
            raise ValueError(
                "image dimensions changed since the scan "
                f"({box.width}×{box.height} -> {upright.size[0]}×{upright.size[1]})"
            )
        kwargs = _save_kwargs(source, upright, image_format, path=path)
        if image_format == "JPEG":
            from PIL import JpegImagePlugin

            sampling = JpegImagePlugin.get_sampling(source)
            progressive = bool(source.info.get("progressive") or source.info.get("progression"))
            if _try_lossless_jpegtran(
                path,
                box,
                destination,
                upright_already=(orient in (1, None)),
                sampling=sampling,
                progressive=progressive,
                exif_bytes=kwargs.get("exif"),
            ):
                return box.crop_width, box.crop_height
        cropped = upright.crop((box.left, box.top, box.right, box.bottom))
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


@functools.cache
def _ffmpeg_encoders() -> frozenset[str]:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception:
        return frozenset()
    names = set()
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return frozenset(names)


def _video_color_args(probe: dict) -> list[str]:
    """HDR / wide-gamut color signaling options preserved from source probe."""
    args: list[str] = []
    for key, flag in (
        ("color_primaries", "-color_primaries"),
        ("color_trc", "-color_trc"),
        ("color_space", "-colorspace"),
        ("color_range", "-color_range"),
    ):
        val = (probe.get(key) or "").strip()
        if val and val.lower() != "unknown":
            args.extend([flag, val])
    return args


def _video_mux_args(suffix: str) -> list[str]:
    """Container-specific muxer options (``-movflags`` only on MP4/MOV/M4V)."""
    if suffix.lower() in (".mp4", ".mov", ".m4v"):
        return ["-movflags", "+faststart+use_metadata_tags"]
    return []


def _video_encoder_args(
    probe: dict,
    *,
    suffix: str = ".mp4",
    prefer_hw: bool = True,
) -> tuple[list[str], bool]:
    """Encoder options keeping codec family, bit depth, and WebM/macOS VideoToolbox support.

    Returns ``(args, is_hw_encoder)`` so callers can fall back cleanly to
    software encoding if hardware initialization rejects dimensions or flags.
    """
    encoders = _ffmpeg_encoders()
    suffix_l = suffix.lower()
    pix_fmt = probe["pix_fmt"]
    ten_bit = "10" in pix_fmt or "12" in pix_fmt

    if suffix_l == ".webm":
        if "libvpx-vp9" in encoders:
            pixel = "yuv420p10le" if ten_bit else "yuv420p"
            return (["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-pix_fmt", pixel], False)
        if "libvpx" in encoders:
            return (["-c:v", "libvpx", "-crf", "24", "-b:v", "0", "-pix_fmt", "yuv420p"], False)
        raise RuntimeError("ffmpeg WebM encoder (libvpx-vp9/libvpx) is not available")

    use_hevc = probe["codec_name"] == "hevc" or ten_bit
    if (
        prefer_hw
        and sys.platform == "darwin"
        and not os.environ.get("DEDUPE_DISABLE_HWACCEL")
        and suffix_l not in (".avi",)
    ):
        hw_enc = "hevc_videotoolbox" if use_hevc else "h264_videotoolbox"
        if hw_enc in encoders:
            pixel = "p010le" if ten_bit else "yuv420p"
            extra = ["-tag:v", "hvc1"] if use_hevc else []
            return (["-c:v", hw_enc, "-q:v", "65", "-pix_fmt", pixel, *extra], True)

    if use_hevc:
        encoder, crf = "libx265", VIDEO_HEVC_CRF
        args = ["-tag:v", "hvc1"]
        pixel = "yuv420p10le" if ten_bit else "yuv420p"
    else:
        encoder, crf, args, pixel = "libx264", VIDEO_H264_CRF, [], "yuv420p"
    if encoder not in encoders:
        raise RuntimeError(f"ffmpeg encoder {encoder} is not available")
    return (
        ["-c:v", encoder, "-crf", str(crf), "-preset", "medium", "-pix_fmt", pixel, *args],
        False,
    )


def _run_video_encode(
    command: list[str],
    *,
    duration: float | None,
    timeout: float,
    destination: Path,
    progress_cb: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, str]:
    """Execute a video re-encode command, streaming progress and supporting cancel."""
    import time

    if cancelled and cancelled():
        raise InterruptedError("crop cancelled")
    if progress_cb is None and cancelled is None:
        res = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return res.returncode, res.stderr or ""

    prog_cmd = [*command[:-2], "-progress", "pipe:1", *command[-2:]]
    proc = subprocess.Popen(
        prog_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace"
    )
    err_lines: list[str] = []

    def drain_err():
        assert proc.stderr is not None
        err_lines.append(proc.stderr.read())

    t_err = threading.Thread(target=drain_err, daemon=True)
    t_err.start()
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    try:
        while True:
            if cancelled and cancelled():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                destination.unlink(missing_ok=True)
                raise InterruptedError("crop cancelled")
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=2)
                raise subprocess.TimeoutExpired(command, timeout)
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            if progress_cb and duration and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1].strip())
                    progress_cb(max(0.0, min(1.0, (us / 1_000_000.0) / duration)))
                except (ValueError, ZeroDivisionError):
                    pass
    finally:
        proc.stdout.close()
        t_err.join(timeout=5)
    rc = proc.wait(timeout=10)
    if rc == 0 and progress_cb is not None:
        progress_cb(1.0)
    return rc, "".join(err_lines)


def _video_vf_arg(box: CropBox, probe: dict) -> str:
    """Video crop filter plus ``setparams`` to preserve HDR signaling in bitstreams."""
    vf = f"crop={box.crop_width}:{box.crop_height}:{box.left}:{box.top}"
    params: list[str] = []
    for key, param in (
        ("color_primaries", "color_primaries"),
        ("color_trc", "color_trc"),
        ("color_space", "colorspace"),
        ("color_range", "range"),
    ):
        val = (probe.get(key) or "").strip()
        if val and val.lower() != "unknown":
            params.append(f"{param}={val}")
    if params:
        vf += ",setparams=" + ":".join(params)
    return vf


def write_cropped_video_copy(
    path: Path,
    box: CropBox,
    destination: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    """Write ``path`` re-encoded cropped to ``box`` at ``destination``.

    Preserves display orientation, VFR frame timing (with ``-vsync passthrough``
    fallback on older ffmpeg), HDR color signaling, QuickTime/container tags,
    audio/subtitles, and uses macOS VideoToolbox with automatic software
    fallback. Honors optional ``progress_cb`` and ``cancelled``.
    """
    if cancelled and cancelled():
        raise InterruptedError("crop cancelled")
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg/ffprobe not available")
    probe = _probe_video_stream(path)
    if probe is None:
        raise ValueError("could not read the video stream")
    if (probe["width"], probe["height"]) != (box.width, box.height):
        raise ValueError(
            "video dimensions changed since the scan "
            f"({box.width}×{box.height} -> {probe['width']}×{probe['height']})"
        )
    suffix = destination.suffix or Path(path).suffix
    color_args = _video_color_args(probe)
    mux_args = _video_mux_args(suffix)
    vf_arg = _video_vf_arg(box, probe)
    timeout = max(600.0, (probe["duration"] or 60.0) * 30 + 300)

    def build_command(*, prefer_hw: bool) -> tuple[list[str], bool]:
        enc_args, is_hw = _video_encoder_args(probe, suffix=suffix, prefer_hw=prefer_hw)
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(path),
            "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
            "-vf", vf_arg,
            "-fps_mode", "passthrough",
            *enc_args,
            *color_args,
            "-c:a", "copy", "-c:s", "copy",
            "-map_metadata", "0", *mux_args,
            "-y", str(destination),
        ]
        return cmd, is_hw

    command, is_hw = build_command(prefer_hw=True)
    try:
        rc, stderr = _run_video_encode(
            command,
            duration=probe["duration"],
            timeout=timeout,
            destination=destination,
            progress_cb=progress_cb,
            cancelled=cancelled,
        )
        if rc != 0 and "fps_mode" in stderr.lower():
            command = _replace_fps_mode_legacy(command)
            rc, stderr = _run_video_encode(
                command,
                duration=probe["duration"],
                timeout=timeout,
                destination=destination,
                progress_cb=progress_cb,
                cancelled=cancelled,
            )
        if rc != 0 and is_hw:
            command, _ = build_command(prefer_hw=False)
            rc, stderr = _run_video_encode(
                command,
                duration=probe["duration"],
                timeout=timeout,
                destination=destination,
                progress_cb=progress_cb,
                cancelled=cancelled,
            )
            if rc != 0 and "fps_mode" in stderr.lower():
                command = _replace_fps_mode_legacy(command)
                rc, stderr = _run_video_encode(
                    command,
                    duration=probe["duration"],
                    timeout=timeout,
                    destination=destination,
                    progress_cb=progress_cb,
                    cancelled=cancelled,
                )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("video re-encode timed out") from exc
    if rc != 0:
        tail = stderr.strip().splitlines()
        detail = tail[-1] if tail else f"ffmpeg exited {rc}"
        raise RuntimeError(f"ffmpeg could not crop the video: {detail}")
    written = _probe_video_stream(destination)
    if written is None:
        raise ValueError("cropped video could not be read back")
    if (written["width"], written["height"]) != (box.crop_width, box.crop_height):
        raise ValueError(
            "cropped file has unexpected dimensions "
            f"{written['width']}×{written['height']}"
        )
    if (
        probe["duration"] and written["duration"]
        and abs(written["duration"] - probe["duration"]) > max(2.0, probe["duration"] * 0.05)
    ):
        raise ValueError("cropped video has an unexpected duration")
    return written["width"], written["height"]


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
    progress_cb: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CropOutcome:
    """Replace ``record``'s file with its cropped version; original to Trash.

    The cropped image is written and verified beside the original first, the
    original is moved to the system Trash, and the cropped file then takes
    its place. The original's permissions, timestamps, extended attributes,
    and file flags carry over, so date-sorted folders keep their order, hidden
    files stay hidden, and a Finder-locked file comes back locked. Any failure
    before the swap leaves the original untouched; a failure during the swap
    moves it back (with ``shutil.move`` fallback across filesystems on ``EXDEV``).
    """
    if cancelled and cancelled():
        raise InterruptedError("crop cancelled")
    original = Path(record.path)
    before = os.lstat(original)
    flags = int(getattr(before, "st_flags", 0))
    _refuse_system_lock(original, flags)
    with editable_folder(original.parent):
        temp = original.with_name(
            f".{original.stem}.dedupe-crop-{uuid.uuid4().hex[:8]}{original.suffix}"
        )
        try:
            if record.media_type == MediaType.VIDEO:
                width, height = write_cropped_video_copy(
                    original, box, temp, progress_cb=progress_cb, cancelled=cancelled
                )
            else:
                width, height = write_cropped_copy(original, box, temp)
                if progress_cb is not None:
                    progress_cb(1.0)
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
                try:
                    os.replace(trashed, original)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.move(str(trashed), str(original))
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
