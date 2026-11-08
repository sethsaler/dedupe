"""Black-border detection and in-place cropping for still images.

A scan with border detection enabled looks along each edge of every
croppable image for solid black bars — letterboxing, pillarboxing, scanner
margins, screenshots of videos — and suggests the crop that removes them.
Images that look like screenshots are also checked for the device's own
interface at the top and bottom edges: a phone's status bar (clock, battery,
notification icons), a home indicator or navigation bar, a desktop menu bar
or taskbar.
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
BORDER_SIGNATURE = "black-borders-v3"
LEGACY_BORDER_SIGNATURES = frozenset({"black-borders-v1", "black-borders-v2"})
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
# A bar is a band of one background colour running in from the edge, holding
# small glyphs (clock, icons, a home-indicator pill). Pixels within this
# per-channel distance of the band's colour count as background.
CHROME_TOLERANCE = 14
# The outermost row must be almost entirely background.
CHROME_EDGE_FLAT = 0.9
# A row holds glyphs when at least this share of it differs from the band.
CHROME_INK_FRACTION = 0.003
# A row with less background than this is picture content, never a bar.
CHROME_BUSY_FLAT = 0.3
CHROME_MIN_FRACTION = 0.012
CHROME_SAMPLE_COLUMNS = 480


@dataclass(frozen=True)
class ChromeEdge:
    """How far interface bars may reach in from one edge (shares of height)."""

    max_fraction: float
    # The first glyphs sit this close to the edge (a clock, a nav pill).
    first_ink_max: float
    # Glyphs are small: the first cluster of glyph rows is at most this tall.
    cluster_max: float


# Status bar or menu bar.
CHROME_TOP = ChromeEdge(max_fraction=0.08, first_ink_max=0.035, cluster_max=0.03)
# Home indicator, navigation buttons, tab bar, taskbar.
CHROME_BOTTOM = ChromeEdge(max_fraction=0.12, first_ink_max=0.05, cluster_max=0.06)


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


def _edge_bar(lines) -> int:
    """Thickness of the black bar at the start of ``lines`` (ordered from the edge).

    ``lines`` is a 2-D luma array, one row per line running parallel to the
    edge. Returns 0 when the edge is not a flat black band ending in a hard
    edge against the picture.
    """
    import numpy as np

    level = float(np.median(lines[0]))
    if level > DARK_LUMA:
        return 0
    near = np.abs(lines.astype(np.int16) - level) <= BAR_TOLERANCE
    flat = near.mean(axis=1) >= BAR_LINE_FRACTION
    run = int(flat.size) if flat.all() else int(np.argmin(flat))
    if run == 0 or run >= len(lines):
        return run
    off = 1.0 - near[run : run + BAR_EDGE_LINES].mean(axis=1)
    return run if off.max() >= BAR_EDGE_FRACTION else 0


def detect_borders_in_array(luma) -> tuple[int, int, int, int] | None:
    """Black-bar thickness ``(top, bottom, left, right)`` for a luma array.

    Returns ``None`` when no edge carries a bar worth cropping or when the
    picture left over would be too small to be the real content.
    """
    import numpy as np

    height, width = luma.shape[:2]
    if height < 8 or width < 8:
        return None
    # A bar never reaches past the share the picture must keep, so only that
    # much of each edge is examined.
    reach_rows = height - int(np.ceil(height * MIN_CONTENT_FRACTION)) + BAR_EDGE_LINES
    reach_cols = width - int(np.ceil(width * MIN_CONTENT_FRACTION)) + BAR_EDGE_LINES
    top = _edge_bar(luma[:reach_rows])
    if top >= height:
        return None  # the whole frame is black
    bottom = _edge_bar(luma[::-1][:reach_rows])
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


def _edge_chrome(strip, height: int, edge: ChromeEdge, *, center_empty: bool) -> int:
    """Rows of interface bar at the start of ``strip`` (rows ordered from the edge).

    The bar is a band of the edge row's colour holding small glyphs. It ends
    where the background changes (a bar of its own colour, or a hairline) or,
    on a page of the same colour, halfway across the gap between the bar's
    glyphs and the first text below them. Returns 0 when the edge does
    not look like an interface bar.
    """
    import numpy as np

    limit = min(len(strip) - 2, int(height * edge.max_fraction))
    if limit < 4:
        return 0
    window = strip[: limit + 2].astype(np.int16)
    background = np.median(window[:2].reshape(-1, 3), axis=0)
    far = np.abs(window - background).max(axis=2) > CHROME_TOLERANCE
    flat = 1.0 - far.mean(axis=1)
    if flat[0] < CHROME_EDGE_FLAT:
        return 0  # the picture runs to the edge; anything there is drawn over it
    columns = far.shape[1]
    ink = far.sum(axis=1) >= max(2, columns * CHROME_INK_FRACTION)
    # A separator is a row of one other flat colour: a bar of its own colour
    # below the status bar, a hairline, another window.
    medians = np.median(window, axis=1)
    own_flat = 1.0 - (
        np.abs(window - medians[:, None, :]).max(axis=2) > CHROME_TOLERANCE
    ).mean(axis=1)
    separator = (np.abs(medians - background).max(axis=1) > CHROME_TOLERANCE) & (
        own_flat >= CHROME_EDGE_FLAT
    )
    boundary = next((row for row in range(1, limit + 1) if separator[row]), None)

    stop = boundary if boundary is not None else limit + 1
    ink_rows = np.flatnonzero(ink[:stop])
    if ink_rows.size == 0:
        return 0  # a plain coloured margin, not an interface bar
    first = int(ink_rows[0])
    if first > height * edge.first_ink_max:
        return 0
    gap = max(2, int(height * 0.004))
    end = first
    for row in ink_rows[1:]:
        if row - end > gap:
            break
        end = int(row)
    if end + 1 - first > height * edge.cluster_max:
        return 0  # tall glyphs: a caption or headline, not a status bar
    if center_empty:
        # Phone status bars keep the middle clear (the clock sits left, the
        # battery right, the camera cut-out is not captured); a centred
        # caption is not a status bar.
        middle = far[first : end + 1, columns // 3 : 2 * columns // 3]
        if middle.mean() > 0.02:
            return 0

    later = ink_rows[ink_rows > end + gap]
    if boundary is not None:
        cut = boundary
    elif later.size and flat[later[0]] < CHROME_BUSY_FLAT:
        cut = int(later[0])  # a picture starts here
    elif later.size:
        cut = (end + 1 + int(later[0])) // 2  # halfway to the first text below
    else:
        cut = min(end + 1 + first, limit)  # a margin like the one above the glyphs
    if cut < height * CHROME_MIN_FRACTION or cut > limit:
        return 0
    if (flat[:cut] < CHROME_BUSY_FLAT).any():
        return 0
    return int(cut)


def detect_screenshot_chrome(image) -> tuple[int, int]:
    """Rows of interface bar ``(top, bottom)`` on an upright screenshot image."""
    import numpy as np

    rgb = np.asarray(_flattened(image))
    height, width = rgb.shape[:2]
    if height < 64 or width < 64:
        return 0, 0
    sample = rgb[:, :: max(1, width // CHROME_SAMPLE_COLUMNS)]
    portrait = height >= width * 1.3
    top = _edge_chrome(sample, height, CHROME_TOP, center_empty=portrait)
    bottom = _edge_chrome(sample[::-1], height, CHROME_BOTTOM, center_empty=False)
    return top, bottom


def _box_for(upright, *, chrome: tuple[int, int] = (0, 0)) -> CropBox | None:
    """Combine interface bars with black bars inside them into one crop."""
    width, height = upright.size
    chrome_top, chrome_bottom = chrome
    luma = _luma(upright)[chrome_top : height - chrome_bottom]
    bars = detect_borders_in_array(luma) or (0, 0, 0, 0)
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
    if evidence is not None:
        chrome = detect_screenshot_chrome(screened)
        if any(chrome):
            return _box_for(screened, chrome=chrome)
    if detect_borders_in_array(_luma(screened)) is None:
        return None

    upright = screened
    if reduced:
        # The quick pass found bars; measure them exactly at full resolution.
        with Image.open(path) as source:
            upright = ImageOps.exif_transpose(source)
            upright.load()
    return _box_for(upright)


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
