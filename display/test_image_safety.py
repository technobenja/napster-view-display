"""Unit tests for image_safety.py — shared decode boundary.

The three hazards are tested separately because they fail separately: a
body can be the right type and too large, the right size and the wrong
type, or both right and still declare dimensions that expand to gigabytes.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from display import image_safety


def png_bytes(width: int = 64, height: int = 64, trailing: int = 0) -> bytes:
    """A real PNG signature and IHDR. Only the header is ever parsed, so
    the pixel data is irrelevant — which is exactly the point of the
    dimension check: a file this small can declare any size it likes."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = (
        struct.pack(">I", len(ihdr))
        + b"IHDR"
        + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )
    return signature + chunk + b"\x00" * trailing


def jpeg_bytes(width: int = 64, height: int = 64, app0_padding: int = 0) -> bytes:
    """A JPEG with an APP0 segment before the SOF0, so the dimension
    parser has to actually walk the segment chain rather than read a
    fixed offset. `app0_padding` grows that segment the way a real EXIF
    thumbnail would."""
    app0_payload = b"JFIF\x00" + b"\x00" * (9 + app0_padding)
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload
    sof_payload = (
        b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    )
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


class SniffFormatTests(unittest.TestCase):
    def test_png_and_jpeg_are_recognized(self) -> None:
        self.assertEqual(image_safety.sniff_format(png_bytes()), image_safety.FORMAT_PNG)
        self.assertEqual(
            image_safety.sniff_format(jpeg_bytes()), image_safety.FORMAT_JPEG
        )

    def test_everything_else_is_rejected(self) -> None:
        """An allow-list, not a blocklist: GIF, SVG, PDF and a bare
        script are all simply 'not PNG or JPEG'. Nobody has to have
        thought of them in advance."""
        for hostile in (
            b"GIF89a",
            b"<svg xmlns='http://www.w3.org/2000/svg'><script>",
            b"%PDF-1.7",
            b"#!/bin/sh\nrm -rf /",
            b"",
            b"\x89PN",  # truncated signature
        ):
            self.assertIsNone(image_safety.sniff_format(hostile), hostile[:12])


class DimensionTests(unittest.TestCase):
    def test_png_dimensions_are_parsed_from_the_header(self) -> None:
        self.assertEqual(image_safety.read_dimensions(png_bytes(800, 600)), (800, 600))

    def test_jpeg_dimensions_are_parsed_past_an_app0_segment(self) -> None:
        self.assertEqual(image_safety.read_dimensions(jpeg_bytes(1024, 768)), (1024, 768))

    def test_jpeg_dimensions_survive_a_large_leading_segment(self) -> None:
        data = jpeg_bytes(320, 240, app0_padding=4096)
        self.assertEqual(image_safety.read_dimensions(data), (320, 240))

    def test_truncated_header_yields_no_dimensions(self) -> None:
        self.assertIsNone(image_safety.read_dimensions(png_bytes()[:20]))

    def test_png_signature_without_an_ihdr_yields_no_dimensions(self) -> None:
        """Magic bytes prove a file *starts* like a PNG and nothing more."""
        fake = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        self.assertIsNone(image_safety.read_dimensions(fake))

    def test_unparseable_dimensions_are_not_ok(self) -> None:
        """"We could not tell how big it is" is not a bound on a decode."""
        self.assertFalse(image_safety.dimensions_ok(None))

    def test_zero_and_negative_dimensions_are_not_ok(self) -> None:
        self.assertFalse(image_safety.dimensions_ok((0, 100)))
        self.assertFalse(image_safety.dimensions_ok((100, 0)))

    def test_the_boundary_itself_is_allowed(self) -> None:
        limit = image_safety.MAX_DIMENSION
        self.assertTrue(image_safety.dimensions_ok((limit, limit)))
        self.assertFalse(image_safety.dimensions_ok((limit + 1, limit)))


class ValidateBytesTests(unittest.TestCase):
    def test_a_normal_png_passes(self) -> None:
        self.assertTrue(image_safety.validate_bytes(png_bytes(1024, 1024)))

    def test_the_decompression_bomb_is_rejected(self) -> None:
        """Worked example: a tiny PNG declaring 60000x60000
        expands to roughly 14GB, and that decode happens on the main
        thread inside drawRect_ — the UI is gone before anything can
        react to it."""
        bomb = png_bytes(60000, 60000)
        self.assertLess(len(bomb), 100, "the whole point is that the file is tiny")
        self.assertFalse(image_safety.validate_bytes(bomb))

    def test_a_non_image_is_rejected(self) -> None:
        self.assertFalse(image_safety.validate_bytes(b"GIF89a" + b"\x00" * 100))


class ReadStreamTests(unittest.TestCase):
    def test_chunks_are_reassembled(self) -> None:
        data = png_bytes(64, 64, trailing=5000)
        chunks = [data[i : i + 512] for i in range(0, len(data), 512)]
        self.assertEqual(image_safety.read_stream(chunks), data)

    def test_first_chunk_magic_check_aborts_before_reading_the_rest(self) -> None:
        """Check magic bytes on the *first* chunk and abort. The
        assertion that matters is not the None — it is that the rest of
        the body was never pulled off the wire."""
        consumed = []

        def hostile_chunks():
            consumed.append("first")
            yield b"GIF89a" + b"\x00" * 100
            consumed.append("second")  # must never happen
            yield b"\x00" * 10_000_000

        self.assertIsNone(image_safety.read_stream(hostile_chunks()))
        self.assertEqual(consumed, ["first"])

    def test_cap_is_enforced_incrementally_not_after_the_fact(self) -> None:
        """A cap checked after resp.content is decoration — the body is
        already in memory by then, and httpx transparently decodes gzip
        so Content-Length is not a bound on decoded bytes. This asserts
        the generator is abandoned partway through."""
        produced = 0
        chunk = b"\x00" * (1024 * 1024)

        def endless_chunks():
            nonlocal produced
            yield png_bytes(64, 64)
            while True:
                produced += 1
                yield chunk

        self.assertIsNone(image_safety.read_stream(endless_chunks()))
        # Stopped near the cap, not at some arbitrary later point.
        self.assertLessEqual(produced, image_safety.MAX_IMAGE_BYTES // len(chunk) + 2)

    def test_empty_body_is_rejected(self) -> None:
        self.assertIsNone(image_safety.read_stream([]))
        self.assertIsNone(image_safety.read_stream([b"", b""]))

    def test_a_valid_prefix_followed_by_a_bomb_header_is_rejected(self) -> None:
        chunks = [png_bytes(60000, 60000)]
        self.assertIsNone(image_safety.read_stream(chunks))


class ValidateFileTests(unittest.TestCase):
    """The `caches=False` half of the safety boundary. FolderSource hands paths straight
    to the renderer, so a check that lived only in cache-sync would never
    run for the source most people use."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_a_normal_local_png_passes(self) -> None:
        self.assertTrue(image_safety.validate_file(self._write("a.png", png_bytes())))

    def test_a_normal_local_jpeg_passes(self) -> None:
        self.assertTrue(image_safety.validate_file(self._write("a.jpg", jpeg_bytes())))

    def test_a_local_decompression_bomb_is_rejected(self) -> None:
        """The user's own Pictures folder is not a trusted source of
        dimensions — this is the case that motivated moving the check to
        the decode boundary."""
        path = self._write("bomb.png", png_bytes(60000, 60000))
        self.assertFalse(image_safety.validate_file(path))

    def test_a_renamed_non_image_is_rejected(self) -> None:
        """The extension is a filter, not evidence."""
        path = self._write("not-really.png", b"GIF89a" + b"\x00" * 100)
        self.assertFalse(image_safety.validate_file(path))

    def test_a_missing_file_is_rejected_without_raising(self) -> None:
        self.assertFalse(image_safety.validate_file(self.dir / "nope.png"))

    def test_a_directory_is_rejected_without_raising(self) -> None:
        (self.dir / "subdir.png").mkdir()
        self.assertFalse(image_safety.validate_file(self.dir / "subdir.png"))

    def test_an_oversized_file_is_rejected_without_reading_it(self) -> None:
        path = self.dir / "huge.png"
        with path.open("wb") as f:
            f.write(png_bytes())
            f.truncate(image_safety.MAX_IMAGE_BYTES + 1)  # sparse, costs no disk
        self.assertFalse(image_safety.validate_file(path))


if __name__ == "__main__":
    unittest.main()
