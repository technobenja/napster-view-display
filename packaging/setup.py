"""Py2app build for `ImageView.app` — Step -1b packaging spike.

Build:

    ../.venv-build/bin/python setup.py py2app
    codesign --force --deep --sign - dist/ImageView.app

Every non-obvious option below is a trap with a build cycle behind
it; each is annotated with the reason rather than left to look arbitrary.

`display/` and `ui/` are real packages, so the **repo root**
is what goes on `sys.path` for modulegraph and every module is named explicitly
in `includes` using its package-qualified name.

**`includes`, not `packages`, and that is deliberate.** `packages:` is
the obvious-looking choice now that these are real packages, and it is
the wrong one: py2app copies a `packages:` entry's
`__pycache__` into the bundle *verbatim*, and those stale `.pyc` files
still carry a `co_filename` pointing at the build machine. That was the
whole of the 130-file identity leak. Modules listed in `includes:` are
re-byte-compiled with rewritten filenames and leak nothing. So the cost
of `includes` is this hand-maintained list; the benefit is that the
leak cannot come back. `display.sources` moved out of `packages:` for
exactly this reason — as a `packages:` entry it was leaking already.
"""

from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DISPLAY = REPO / "display"  # only for `resources` below; imports go via REPO

# modulegraph resolves `from display.cache import ...` the same way the
# interpreter would, so the directory that *contains* the two packages
# has to be importable at build time. Note this is REPO, not REPO/display
# — putting the package directory itself on the path would let the old
# flat names resolve and shadow the package form.
sys.path.insert(0, str(REPO))

# Imported for BUNDLE_ID below — the identifier has exactly one home.
from display import paths  # noqa: E402

# Explicit, not globbed: a glob would sweep in `test_*.py`, `.venv`
# (a symlink into the runtime venv), and `demo_transition.py`.
#
# The bare `display` entry is the package `__init__` itself. modulegraph
# pulls a parent package in when a submodule is named, but naming it is
# cheaper than depending on that.
DISPLAY_MODULES = [
    "display",
    "display.sources",
    "display.sources.base",
    "display.sources.factory",
    "display.sources.folder",
    "display.sources.image_server",
    "display.sources.json_url",
    "display.sources.net",
    "display.app",
    "display.atomic_io",
    "display.blank_schedule",
    "display.cache",
    "display.calibration",
    "display.config_store",
    "display.control",
    "display.display_target",
    "display.image_pool",
    "display.image_safety",
    "display.image_store",
    "display.log_rotation",
    "display.paths",
    "display.pattern",
    "display.rotation",
    "display.settings",
    "display.single_instance",
    "display.smoke_test",
    "display.source_settings",
    "display.window",
]

# `ui/` modules, same rule: explicit, so `test_*.py` stays out. Every
# one of these is reachable from `menubar` — `calibrate_window` only
# lazily (menubar.py imports it inside `calibrate_`, deliberately, so a
# broken calibration window cannot cost the status item), which is
# precisely the kind of import modulegraph's static analysis does not
# follow. Naming them all is the fix.
UI_MODULES = [
    "ui",
    "ui.calibrate_state",
    "ui.calibrate_window",
    "ui.first_run_state",
    "ui.first_run_window",
    "ui.identify",
    "ui.menubar",
    "ui.menubar_state",
    "ui.settings_state",
    "ui.settings_window",
    "ui.ui_agent",
]

# The six entries added at Step 5b (`display.blank_schedule` and the five
# `ui.*` above) were reachable through static imports and modulegraph did
# pull them in unlisted — so this is not a bug fix. It closes the gap the
# docstring warns about: the moment one of them moves behind a lazy
# import, as `calibrate_window` already is, an unlisted module is simply
# absent from the bundle and only fails when a user opens that window.
# `make_release.sh`'s coverage gate now fails the build on any module
# present in the tree and missing from the bundle.

# PyObjC's lazy framework loading defeats modulegraph's static analysis,
# so anything unnamed can be missing at runtime.
PYOBJC_MODULES = ["objc", "AppKit", "Foundation"]

OPTIONS = {
    # Carbon Apple Events: hangs at startup under launchd, and would
    # corrupt the `--display` flag imageview_main.py dispatches on.
    "argv_emulation": False,
    # Third-party only. Our own packages are in `includes` — see the
    # module docstring for why (__pycache__ leak).
    "packages": [
        "httpx",
        "httpcore",
        "h11",
        "anyio",
        "idna",
        "certifi",  # cacert.pem is a data file, not a module
    ],
    "includes": DISPLAY_MODULES + UI_MODULES + PYOBJC_MODULES,
    # The umbrella pyobjc install would drag ~200 framework wrappers in.
    # The build venv does not have them; exclude the rest defensively.
    "excludes": [
        "tkinter",
        "unittest",
        "pytest",
        "setuptools",
        "pip",
        "numpy",
        "PIL",
    ],
    "plist": {
        "CFBundleName": "ImageView",
        "CFBundleDisplayName": "ImageView",
        # Taken from paths.py, not spelled out again. This file predates
        # the identifier decision and still named a personal handle
        # — the exact identity leak the release gate rules out, and one that
        # would have shipped in the signature, the TCC record, and every
        # stranger's `launchctl list`. paths.BUNDLE_ID is the settled
        # value and the only place it is written down.
        "CFBundleIdentifier": paths.BUNDLE_ID,
        "CFBundleVersion": "1.0.1",
        "CFBundleShortVersionString": "1.0.1",
        "CFBundleExecutable": "ImageView",
        # app.py sets NSApplicationActivationPolicyAccessory at runtime,
        # but the Info.plist is consulted before main() runs, so without
        # this the agent can flash a Dock icon and steal focus at login.
        "LSUIElement": True,
        # LSBackgroundOnly is deliberately absent and must stay absent:
        # it looks like the tidier key for a background agent and it
        # denies the window-server connection this app depends on.
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    },
    # Seed config is read-only bundle data (fifth root).
    #
    # `menubar-template.pdf` is status item icon. Without it here
    # the menu bar loads the icon from the *source tree* whenever the
    # source tree happens to exist — which it does on the build machine,
    # so the bundle looks correct here and ships with no icon to
    # everyone else. py2app flattens these to the top of
    # `Contents/Resources/`, which is what `paths.menubar_template_path()`
    # expects.
    "resources": [
        str(DISPLAY / "config"),
        str(HERE / "menubar-template.pdf"),
    ],
    # app icon, generated by `make_app_icon.py`. Without this
    # py2app ships its own `PythonApplet.icns` — the generic Python
    # rocket — which is what the bundle carried until 2026-07-19.
    # Regenerate with:
    #     display/.venv/bin/python3 packaging/make_app_icon.py
    "iconfile": str(HERE / "ImageView.icns"),
}

setup(
    name="ImageView",
    app=[
        {
            "script": str(HERE / "imageview_main.py"),
            # Names the executable Contents/MacOS/ImageView, which is
            # what the LaunchAgent's ProgramArguments will point at.
            "plist": OPTIONS["plist"],
        }
    ],
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
