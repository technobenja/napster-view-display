"""Structural tripwire — single most consequential shared-infra
guardrail, enforced rather than left as a docstring's good intentions.

GUARDRAILS.md: the ComfyUI queue behind Image Server is shared with
Podcast Studio and Music Studio. A rotation that calls /api/generate in a
loop or on a timer would starve them. This test fails the moment any
future edit reintroduces that call anywhere under display/.
"""

from __future__ import annotations

import unittest
from pathlib import Path

DISPLAY_DIR = Path(__file__).parent
FORBIDDEN = "/api/generate"
_SELF_NAME = Path(__file__).name


class NoGenerateCallsTest(unittest.TestCase):
    def test_no_source_file_references_api_generate(self) -> None:
        offenders = []
        for path in DISPLAY_DIR.rglob("*.py"):
            if path.name == _SELF_NAME:
                continue  # this file's own docstring names the string
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            if FORBIDDEN in text:
                offenders.append(str(path.relative_to(DISPLAY_DIR)))
        self.assertEqual(
            offenders,
            [],
            f"Found the literal string {FORBIDDEN!r} in: {offenders}. "
            f"Per GUARDRAILS.md, this client's surface area must never "
            f"include /api/generate.",
        )


if __name__ == "__main__":
    unittest.main()
