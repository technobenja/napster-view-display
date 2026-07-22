"""Shared image-safety boundary.

Every path that turns *untrusted bytes* into *something the renderer will
decode* goes through this module. That is the whole point of it existing
as one module rather than three copies: byte retrieval moved into
`ImageSource.fetch()` specifically so these checks would have exactly one
enforcement point.

Three separate hazards, three separate checks:

1. **Unbounded body.** A cap checked after `resp.content` is decoration —
   the body is already resident in memory by then, and httpx transparently
   decodes gzip, so `Content-Length` is not a bound on *decoded* bytes.
   `read_stream()` therefore counts decoded chunks as they arrive and
   aborts the moment the running total crosses `MAX_IMAGE_BYTES`.

2. **Wrong file type.** Magic bytes are checked on the **first chunk** and
   the transfer abandoned before the rest is read, let alone written. PNG
   and JPEG only — an allow-list, so a format nobody vetted (SVG with a
   script, a TIFF with a decoder CVE, a 300MB GIF) is rejected by
   construction rather than by remembering to blocklist it.

3. **Decompression bomb.** Magic bytes only prove a file *starts* like a
   PNG. A 40KB PNG whose IHDR declares 60000x60000 expands to roughly
   14GB, and on this app that decode happens on the main thread inside
   `drawRect_` — the UI is gone before anything can react. So the
   dimension header is parsed and range-checked *before* any decode, and
   because the default `FolderSource` is `caches=False` and hands paths
   straight to the renderer, `validate_file()` exists to run the identical
   check on a local file that was never downloaded at all.

Nothing here raises. A rejected image is logged and reported by return
value, matching cache.py / settings.py / rotation.py.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# explicit numbers.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_DIMENSION = 8192

# How much of a local file to read when validating it without downloading.
# A PNG's IHDR is at a fixed offset; a JPEG's SOF marker can sit behind an
# arbitrary run of APPn/COM segments (EXIF thumbnails are the usual
# culprit), so allow a generous header window before giving up.
HEADER_SCAN_BYTES = 128 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

FORMAT_PNG = "png"
FORMAT_JPEG = "jpeg"

# Extensions this app is willing to even consider. Deliberately narrower
# than "what PIL can open" — the allow-list above is the real gate, this
# just avoids reading every file in a user's folder to find out.
ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})

# JPEG start-of-frame markers carry the dimensions. Excluded from this
# set on purpose: 0xC4 (DHT), 0xC8 (JPG), 0xCC (DAC) share the 0xC0-0xCF
# range but are not frame headers, and reading dimensions out of them
# yields garbage.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def sniff_format(head: bytes) -> str | None:
    """Identify the format from the first few bytes, or None if it is
    neither PNG nor JPEG. Safe on a short or empty buffer."""
    if head.startswith(PNG_MAGIC):
        return FORMAT_PNG
    if head.startswith(JPEG_MAGIC):
        return FORMAT_JPEG
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    # 8-byte signature, 4-byte length, 4-byte "IHDR", then width/height as
    # big-endian uint32. Verify the chunk type rather than trusting the
    # offset: a file can start with the PNG signature and then contain
    # anything at all.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    # Walk the segment chain from just past the SOI looking for a SOF.
    # Bounds are re-checked on every step because `data` is a truncated
    # header window, not necessarily a whole file.
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            # Not aligned on a marker — a padded or malformed stream.
            # Skip forward to the next 0xFF rather than giving up, but
            # never loop without advancing.
            next_marker = data.find(b"\xff", i + 1)
            if next_marker < 0:
                return None
            i = next_marker
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers, no length field
            continue
        if marker == 0xDA:
            return None  # start of scan: past every header, no SOF found
        segment_length = int.from_bytes(data[i + 2 : i + 4], "big")
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if i + 9 > n:
                return None
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return width, height
        i += 2 + segment_length
    return None


def read_dimensions(data: bytes) -> tuple[int, int] | None:
    """Parse (width, height) out of a PNG or JPEG header without
    decoding a single pixel. None if the format is unrecognized or the
    header is truncated/malformed."""
    fmt = sniff_format(data)
    if fmt == FORMAT_PNG:
        return _png_dimensions(data)
    if fmt == FORMAT_JPEG:
        return _jpeg_dimensions(data)
    return None


def dimensions_ok(dimensions: tuple[int, int] | None) -> bool:
    """True only for positive dimensions within MAX_DIMENSION on both
    axes. Unparseable dimensions are *not* ok: this check exists to
    bound a decode, and "we could not tell how big it is" is not a bound."""
    if dimensions is None:
        return False
    width, height = dimensions
    if width <= 0 or height <= 0:
        return False
    return width <= MAX_DIMENSION and height <= MAX_DIMENSION


def validate_bytes(data: bytes, label: str = "image") -> bool:
    """Magic-byte allow-list plus a pre-decode dimension bound. The last
    gate before bytes are written to the cache or handed to a decoder."""
    fmt = sniff_format(data)
    if fmt is None:
        logger.warning(
            "image_safety: rejecting %s — not a PNG or JPEG (magic bytes).", label
        )
        return False
    if len(data) > MAX_IMAGE_BYTES:
        logger.warning(
            "image_safety: rejecting %s — %d bytes exceeds the %d byte cap.",
            label,
            len(data),
            MAX_IMAGE_BYTES,
        )
        return False
    dimensions = read_dimensions(data)
    if not dimensions_ok(dimensions):
        logger.warning(
            "image_safety: rejecting %s — unusable or oversized dimensions %r "
            "(max %d on either axis).",
            label,
            dimensions,
            MAX_DIMENSION,
        )
        return False
    return True


def read_stream(chunks: Iterable[bytes], label: str = "image") -> bytes | None:
    """Accumulate `chunks` into bytes, enforcing the incremental cap and
    checking magic bytes on the first chunk.

    Returns None (having stopped consuming `chunks`) on any rejection, so
    a hostile server sending an endless body is disconnected rather than
    read to completion. Never raises on the checks themselves — an
    exception from the underlying iterator is the caller's to handle."""
    buffer = bytearray()
    checked_magic = False
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        if not checked_magic:
            # Deliberately not "wait until we have 8 bytes": a first
            # chunk shorter than the PNG signature is already anomalous,
            # and sniff_format handles a short buffer by returning None.
            if sniff_format(bytes(buffer[:16])) is None:
                logger.warning(
                    "image_safety: aborting %s — first chunk is not PNG or "
                    "JPEG; not reading the rest.",
                    label,
                )
                return None
            checked_magic = True
        if len(buffer) > MAX_IMAGE_BYTES:
            logger.warning(
                "image_safety: aborting %s — body exceeded the %d byte cap "
                "mid-stream.",
                label,
                MAX_IMAGE_BYTES,
            )
            return None
    if not checked_magic:
        logger.warning("image_safety: rejecting %s — empty body.", label)
        return None
    data = bytes(buffer)
    if not validate_bytes(data, label):
        return None
    return data


def validate_file(path: Path) -> bool:
    """The `caches=False` counterpart to validate_bytes(): run the same
    magic-byte and dimension checks against a local file by reading only
    its header.

    This is what stops a 60000x60000 PNG sitting in the user's own
    Pictures folder from reaching `drawRect_`. It never reads the whole
    file, so it stays cheap enough to run on every entry of a folder
    listing."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("image_safety: cannot stat %s (%s).", path, exc)
        return False
    if size > MAX_IMAGE_BYTES:
        logger.warning(
            "image_safety: rejecting %s — %d bytes exceeds the %d byte cap.",
            path,
            size,
            MAX_IMAGE_BYTES,
        )
        return False
    try:
        with path.open("rb") as f:
            head = f.read(HEADER_SCAN_BYTES)
    except OSError as exc:
        logger.warning("image_safety: cannot read %s (%s).", path, exc)
        return False
    if sniff_format(head) is None:
        logger.warning(
            "image_safety: rejecting %s — not a PNG or JPEG (magic bytes).", path
        )
        return False
    dimensions = read_dimensions(head)
    if not dimensions_ok(dimensions):
        logger.warning(
            "image_safety: rejecting %s — unusable or oversized dimensions %r "
            "(max %d on either axis).",
            path,
            dimensions,
            MAX_DIMENSION,
        )
        return False
    return True


__all__ = [
    "ALLOWED_EXTENSIONS",
    "FORMAT_JPEG",
    "FORMAT_PNG",
    "HEADER_SCAN_BYTES",
    "MAX_DIMENSION",
    "MAX_IMAGE_BYTES",
    "dimensions_ok",
    "read_dimensions",
    "read_stream",
    "sniff_format",
    "validate_bytes",
    "validate_file",
]
