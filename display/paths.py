"""Every writable path the app uses.

Before this module, every writable path was bundle-relative
(`Path(__file__).parent / ...` in calibration.py, settings.py, cache.py,
rotation.py, app.py). Inside a py2app `ImageView.app` that resolves under
`Contents/Resources/`, where each consequence is independently fatal: `/Applications` is `root:admin` so a non-admin user gets
`PermissionError` on the first cache write; an admin user succeeds and
then state is shared across every account on the Mac; and — the one that
cannot be worked around — **writing into a signed bundle invalidates its
code signature**, so macOS kills the app on next launch. The packaging commits to
ad-hoc `codesign -s -`, so in-bundle writes and this app's packaging are
mutually exclusive.

The five roots (table):

| What                                              | Where                             |
|---------------------------------------------------|-----------------------------------|
| calibration.json, settings.json                   | `~/.viewlab/`                     |
| rotation_state.json, status.json, command.json    | `~/.viewlab/state/`               |
| image cache + manifest                            | `~/Library/Caches/<bundle-id>/`   |
| logs                                              | `~/Library/Logs/ImageView/`       |
| bundled seed config (READ-ONLY)                   | `display/config/` (in the bundle) |

Everything is a *function*, not a module-level constant, for two
reasons: `Path.home()` is resolved at call time (so a test can patch it
and get a fully isolated tree), and nothing is computed at import time in
a process whose home directory might not be readable yet.

Directory creation is on demand and never raises — a failure to create a
directory is logged and reported by return value, matching this
project's standing "defensive reads/writes never raise" convention
(calibration.py, settings.py, cache.py, rotation.py all already work this
way).
"""

from __future__ import annotations

import sys
from pathlib import Path

# SETTLED 2026-07-19, per requirement that this be decided before
# Step 5b — it is baked into plists, the code signature, TCC records, and
# any future certificate, so changing it after release is painful.
#
# Why not the obvious candidates:
#   a personal handle     — ruled out outright by the packaging rules. Such an
#                           identifier would land on every stranger's
#                           machine (in ~/Library/LaunchAgents/,
#                           launchctl list, Info.plist, the signature),
#                           which is exactly the identity leak the
#                           plan removes one section earlier, and the
#                           release grep rejects it.
#   a personally-owned    — reversing a domain you own is the textbook
#   domain                  answer and is wrong here for exactly that
#                           reason: it ties the project permanently to an
#                           individual's identity.
#   io.github.<repo>      — the previous placeholder was malformed:
#                           `io.github.X` expects a USER or ORG, and
#                           `napster-view-display` is a repository name.
#   *.napsterview.*       — implies affiliation with a trademark the
#                           README explicitly disclaims.
#
# `dev.viewlab.*` is project-scoped, carries no personal identity, implies
# no affiliation, and matches the `~/.viewlab/` config directory that is
# already public-facing by design (notes that path needs no
# justification in public docs).
#
# Any pre-existing LaunchAgent label from an earlier source-tree install
# is explicitly out of scope and stays as it is; only the shipped
# identifier changes. Shipped labels are
# `dev.viewlab.imageview.display` / `.ui`.
BUNDLE_ID = "dev.viewlab.imageview"

# User-visible application name ("App name — DECIDED (2026-07-18):
# ImageView.app"). Used for the log directory, which is the one root a
# user is ever told to look in by name.
APP_NAME = "ImageView"

CONFIG_DIR_NAME = ".viewlab"

CALIBRATION_FILENAME = "calibration.json"
SETTINGS_FILENAME = "settings.json"
ROTATION_STATE_FILENAME = "rotation_state.json"
STATUS_FILENAME = "status.json"
# Written by the UI, read by the display. Step 1 owns its
# contents; the path is defined here now so that both processes agree on
# it from day one and neither has to invent it later.
COMMAND_FILENAME = "command.json"
LOCK_FILENAME = "display.lock"
# The menu bar's own single-instance lock. A *separate* file from
# LOCK_FILENAME on purpose: the two processes are independent by design
# ("force-quit the UI and the pictures keep rotating"), so sharing
# one lock would make each one's presence exclude the other, which is the
# exact opposite of what is wanted. One lock per role.
UI_LOCK_FILENAME = "ui.lock"

#: The shipped LaunchAgent labels. Derived from BUNDLE_ID rather
#: than spelled out, so the identifier decision above has exactly one
#: place it can be changed. Any pre-existing label from an earlier
#: source-tree install is out of scope and is not represented here.
DISPLAY_AGENT_LABEL = f"{BUNDLE_ID}.display"
UI_AGENT_LABEL = f"{BUNDLE_ID}.ui"

#: Menu bar icon, generated by `packaging/make_menubar_template.py`.
#: Lives in `packaging/` in the source tree and at the top level of
#: `Contents/Resources/` in the bundle — see `menubar_template_path()`.
MENUBAR_TEMPLATE_FILENAME = "menubar-template.pdf"

STDOUT_LOG_FILENAME = "display.stdout.log"
STDERR_LOG_FILENAME = "display.stderr.log"


# -- the five roots ----------------------------------------------------


def config_dir() -> Path:
    """`~/.viewlab/` — calibration.json and settings.json. Hand-editable
    by design, and may be read by other tools, which is why it is a
    plain dotfile directory in $HOME rather than
    `~/Library/Application Support/<bundle-id>/`."""
    return Path.home() / CONFIG_DIR_NAME


def state_dir() -> Path:
    """`~/.viewlab/state/` — machine-written state. Deliberately a
    subdirectory of config_dir() rather than a sixth root: uninstall flow prompts before deleting `~/.viewlab/` because
    calibration.json is hand-measured, and keeping state underneath it
    means that one prompt covers everything the user might care about."""
    return config_dir() / "state"


def cache_dir() -> Path:
    """`~/Library/Caches/<bundle-id>/` — downloaded images + manifest.
    Regenerable by definition, so it lives where macOS expects
    regenerable data and where uninstall can delete it without
    asking."""
    return Path.home() / "Library" / "Caches" / BUNDLE_ID


def log_dir() -> Path:
    """`~/Library/Logs/ImageView/` — where Console.app looks, and the one
    path a user might be asked to open by name."""
    return Path.home() / "Library" / "Logs" / APP_NAME


def bundled_config_dir() -> Path:
    """Read-only seed data shipped inside the bundle
    (`Contents/Resources/config/`). **Never written to** — it is the
    source for the first-run copy into `~/.viewlab/`, and writing
    here is precisely what breaks the code signature.

    Frozen and source layouts differ, and getting this wrong is silent:
    py2app byte-compiles this module into `Contents/Resources/lib/
    python3NN.zip`, so `Path(__file__).parent` resolves to a path *inside
    the zip* — which cannot be read as a directory — while the actual
    resource is placed alongside it at `Contents/Resources/config`. The
    Step -1b packaging spike hit exactly this: the seed was unreachable,
    so the app silently fell back to an unconfigured source and drew the
    empty fill instead of a picture. `sys.frozen` is what py2app sets to
    distinguish the two cases.
    """
    resources = bundled_resources_dir()
    if resources is not None:
        candidate = resources / "config"
        if candidate.is_dir():
            return candidate
        # Fall through to the source layout rather than returning a path
        # that is known not to exist: a wrong-but-present directory is
        # worse than letting the caller's own missing-seed handling run.
    return Path(__file__).resolve().parent / "config"


def bundled_resources_dir() -> Path | None:
    """`Contents/Resources/` when running from a py2app bundle, else None.

    None rather than a source-tree guess, because the two callers want
    *different* source-tree fallbacks (`display/config/` for the seed
    config, `packaging/` for the menu bar icon) and neither can be
    derived from the other. `sys.frozen` is what py2app sets; returning
    None outside a bundle keeps every "am I frozen" test in one place.
    """
    if not getattr(sys, "frozen", False):
        return None
    # sys.executable is Contents/MacOS/<name>; Resources is its sibling.
    return Path(sys.executable).resolve().parent.parent / "Resources"


def menubar_template_path() -> Path:
    """Template PDF, wherever it actually is.

    py2app flattens `resources` entries to the top of
    `Contents/Resources/`, so the bundled copy is *not* under a
    `packaging/` subdirectory — assuming it is would load nothing and
    leave a status item with no icon, which looks like a broken build
    rather than a wrong path. Outside a bundle the source tree's copy is
    used, which is what makes `python3 ui/menubar.py` still work.

    Returns the source path when frozen but the resource is missing, for
    the same reason `bundled_config_dir()` does: the caller already
    handles an unloadable image, and a real path it can report beats a
    fabricated one.
    """
    resources = bundled_resources_dir()
    if resources is not None:
        candidate = resources / MENUBAR_TEMPLATE_FILENAME
        if candidate.is_file():
            return candidate
    return (
        Path(__file__).resolve().parent.parent
        / "packaging"
        / MENUBAR_TEMPLATE_FILENAME
    )


# -- individual files --------------------------------------------------


def calibration_path() -> Path:
    return config_dir() / CALIBRATION_FILENAME


def settings_path() -> Path:
    return config_dir() / SETTINGS_FILENAME


def bundled_calibration_path() -> Path:
    return bundled_config_dir() / CALIBRATION_FILENAME


def bundled_settings_path() -> Path:
    return bundled_config_dir() / SETTINGS_FILENAME


def rotation_state_path() -> Path:
    return state_dir() / ROTATION_STATE_FILENAME


def status_path() -> Path:
    return state_dir() / STATUS_FILENAME


def command_path() -> Path:
    return state_dir() / COMMAND_FILENAME


def lock_path() -> Path:
    """The single-instance `flock` file. Lives in config_dir()
    rather than state_dir() so it is present and lockable before any
    state has ever been written."""
    return config_dir() / LOCK_FILENAME


def ui_lock_path() -> Path:
    """The menu bar's single-instance `flock` file.

    Distinct from `lock_path()`: without a UI guard, the LaunchAgent-
    started menu bar and a double-clicked `ImageView.app` each install
    their own `NSStatusItem`, so the user gets two identical icons in the
    menu bar with no way to tell which is which. Sharing the display's
    lock instead would be worse — it would mean starting the UI killed
    the display, or vice versa.
    """
    return config_dir() / UI_LOCK_FILENAME


def stdout_log_path() -> Path:
    return log_dir() / STDOUT_LOG_FILENAME


def stderr_log_path() -> Path:
    return log_dir() / STDERR_LOG_FILENAME


# -- creation ----------------------------------------------------------


def ensure_dir(path: Path) -> bool:
    """Create `path` (and parents) if absent. Returns True if the
    directory exists afterwards, False if it could not be created.

    Never raises: a read-only or otherwise unwritable home directory
    degrades this app to "keeps showing the last good frame, writes
    nothing", which is a far better outcome than a crash at
    startup — and every caller of the paths below already treats a failed
    write as non-fatal."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"paths.py: cannot create {path} ({exc}); anything written "
            f"there will be skipped.",
            file=sys.stderr,
        )
        return False
    return True


def ensure_all() -> bool:
    """Create all four writable roots up front, at startup. Returns True
    only if every one of them now exists.

    Called once from app.py's main() so that a permissions problem shows
    up as four clear log lines at startup rather than as scattered
    write failures hours later."""
    ok = True
    for directory in (config_dir(), state_dir(), cache_dir(), log_dir()):
        ok = ensure_dir(directory) and ok
    return ok
