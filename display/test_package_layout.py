"""Guards for the package conversion.

Two things here are load-bearing and neither is visible at a glance, so
each gets a test rather than only a comment:

1. **`display/sources/__init__.py` must stay import-free.** `image_pool`
   imports `sources.base`, and `sources.image_server` imports
   `image_pool`. Re-exporting the concrete sources from the package
   `__init__` closes that loop — and closes it *conditionally*: it works
   when `display.sources` is imported first and raises `ImportError:
   partially initialized module` when `display.image_pool` is imported
   first. Verified both ways while writing this. A comment does not
   survive a tidy-up that "just adds the obvious re-exports"; this does.

2. **No module may go back to flat imports.** `import paths` resolves
   fine whenever `display/` happens to be on `sys.path`, which is true
   under the old `cd display && python3 -m unittest` habit and false
   under `python3 -m display.app`. A reverted import would therefore
   pass a casual local run and take the display agent down at launch.

The subprocess test is the only honest way to check (1): once anything
has imported these modules, `sys.modules` hides the ordering entirely,
so a same-process assertion would pass no matter what `__init__` said.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Bare module names that must never appear as a top-level import again.
#: Both package directories, flattened — a name in either one resolving
#: bare means `sys.path` is doing work the package form removed.
FLAT_NAMES = frozenset(
    {p.stem for p in (REPO / "display").glob("*.py") if not p.stem.startswith("test_")}
    | {p.stem for p in (REPO / "display" / "sources").glob("*.py")}
    | {p.stem for p in (REPO / "ui").glob("*.py") if not p.stem.startswith("test_")}
    | {"sources"}
) - {"__init__"}

#: Every participant in the `image_pool` <-> `sources` loop described
#: above. Each has to survive being the *first* thing imported.
CYCLE_PARTICIPANTS = (
    "display.image_pool",
    "display.cache",
    "display.sources",
    "display.sources.base",
    "display.sources.factory",
    "display.sources.image_server",
)


def project_sources() -> list[Path]:
    """Every first-party `.py` file, excluding vendored trees."""
    out: list[Path] = []
    for pattern in ("display/**/*.py", "ui/**/*.py", "packaging/*.py"):
        for path in REPO.glob(pattern):
            parts = set(path.parts)
            if parts & {".venv", ".venv-build", "__pycache__", "build", "dist"}:
                continue
            out.append(path)
    return sorted(out)


class TestSourcesPackageStaysImportFree(unittest.TestCase):
    def test_the_sources_init_imports_nothing_from_this_project(self) -> None:
        """The cycle guard. `from __future__ import annotations` is the
        only import this file is allowed to carry."""
        init = REPO / "display" / "sources" / "__init__.py"
        tree = ast.parse(init.read_text(), str(init))

        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                offenders.append(f"line {node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                offenders.append(f"line {node.lineno}: import {node.names[0].name}")

        self.assertEqual(
            offenders,
            [],
            "display/sources/__init__.py must stay import-free — see this "
            "module's docstring for the cycle it would close",
        )


class TestImportOrderIndependence(unittest.TestCase):
    """Each cycle participant, imported first, in a fresh interpreter."""

    def test_each_participant_imports_first_without_a_cycle(self) -> None:
        for module in CYCLE_PARTICIPANTS:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-c", f"import {module}"],
                    cwd=str(REPO),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"importing {module} first failed:\n{result.stderr}",
                )


class TestNoFlatImportsRemain(unittest.TestCase):
    def test_no_module_imports_a_sibling_by_bare_name(self) -> None:
        offenders = []
        for path in project_sources():
            rel = path.relative_to(REPO)
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in FLAT_NAMES:
                            offenders.append(f"{rel}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    # `level > 0` is an explicit relative import, which is
                    # package-qualified by definition and not what this
                    # test is looking for.
                    if node.level or not node.module:
                        continue
                    if node.module.split(".")[0] in FLAT_NAMES:
                        offenders.append(f"{rel}:{node.lineno}: from {node.module} import ...")

        self.assertEqual(
            offenders,
            [],
            "these imports resolve only when a package directory is on "
            "sys.path; use the package-qualified form",
        )

    def test_the_guard_can_actually_see_the_module_names(self) -> None:
        """A `FLAT_NAMES` that silently came out empty would make the
        test above pass on anything. Pin a few names that must be in it."""
        for name in ("paths", "cache", "image_pool", "menubar_state", "sources"):
            self.assertIn(name, FLAT_NAMES)


class TestBothPackagesAreReal(unittest.TestCase):
    def test_display_and_ui_have_init_files(self) -> None:
        for pkg in ("display", "ui", "display/sources"):
            self.assertTrue(
                (REPO / pkg / "__init__.py").is_file(),
                f"{pkg} is not a package; the conversion is incomplete",
            )


if __name__ == "__main__":
    unittest.main()
