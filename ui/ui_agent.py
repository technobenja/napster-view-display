"""The menu bar's LaunchAgent — `dev.viewlab.imageview.ui`.

The menu bar has to survive logout, reboot, and the closing of whatever
Terminal happened to start it. Until now it did not: it ran as
`display/.venv/bin/python3 ui/menubar.py` from an interactive shell and
died with the session. This module generates, installs, and removes the
LaunchAgent that fixes that.

**Plists are built with `plistlib`, never string templating**.
That is not stylistic. The install path takes a bundle location, and a
future one takes user-supplied values (folder path, base URL); a path
containing `</string><key>ProgramArguments</key>...` would otherwise
inject keys into a file launchd executes at login. `plistlib` escapes by
construction and the label is additionally validated against
`^[a-z0-9.]+$`.

**`KeepAlive` is `{SuccessfulExit: false}`, matching the display's.**
The reasoning is *not* the same as the display's, though it lands in the
same place:

- Plain `KeepAlive: true` is actively hostile here. The menu has a
  `Quit` item, `Quit` calls `NSApp.terminate_`, and that exits 0 — so
  `true` would relaunch the app within seconds of the user quitting it,
  every time. An app that will not stay quit is a bug report, and a
  correct one.
- `KeepAlive: false` (or omitting it) would mean a crashed menu bar
  stays gone until the next login. The display agent's argument applies
  unchanged: a transient AppKit hiccup should self-heal.
- `{SuccessfulExit: false}` gives both. Exit 0 — `Quit`, or the
  single-instance guard finding another menu bar already running — is
  final. A crash or a signal death is respawned. The single-instance
  guard's "exit 0" is load-bearing for exactly this reason, and it is
  the same reason it is load-bearing for the display.

The consequence to document (and `ui/README.md` does): after `Quit` the
job is still *loaded*, just not running, so bringing it back is
`launchctl kickstart`, not `bootstrap`.

**`open -a` stays ruled out** as the program: it returns
immediately, so launchd would see a job that instantly exits, and
LaunchServices refuses to start a second instance of a running bundle
ID. `ProgramArguments` points straight at the executable inside the
bundle, with no arguments — `imageview_main.py`'s no-arg path is the
menu bar.

Install (or reinstall after a rebuild):

    display/.venv/bin/python3 ui/ui_agent.py install
    display/.venv/bin/python3 ui/ui_agent.py uninstall
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

from display import paths
from display.atomic_io import atomic_write_bytes

#: Validation. Deliberately narrower than what launchd accepts:
#: every label this project ships is derived from `paths.BUNDLE_ID`, so
#: anything outside this set is a bug or an injection attempt, and there
#: is no legitimate case to keep open.
LABEL_PATTERN = re.compile(r"^[a-z0-9.]+$")

#: Where the installed app lives. The plist must point at the *installed*
#: bundle rather than a build directory: `dist/ImageView.app` gets
#: deleted by the next build, and a LaunchAgent pointing at a missing
#: binary fails at login with nothing on screen to explain it.
INSTALLED_APP = Path("/Applications/ImageView.app")

#: Floor between respawn attempts, matching the display agent's. Long
#: enough that a menu bar crashing on startup (a broken rebuild, say)
#: does not spin the window server; short enough that a real transient
#: failure recovers without anyone noticing.
THROTTLE_INTERVAL_S = 30


class LabelError(ValueError):
    """A LaunchAgent label that failed validation. Raised rather than
    logged-and-ignored: every other error path in this project degrades
    toward "keep running", but writing a plist built from an unvalidated
    label is the one operation where continuing is worse than stopping."""


def validate_label(label: str) -> str:
    """Return `label` if it is safe to put in a filename and a plist."""
    if not LABEL_PATTERN.match(label):
        raise LabelError(
            f"refusing to use {label!r} as a LaunchAgent label: "
            f"labels must match {LABEL_PATTERN.pattern}"
        )
    return label


def executable_path(app: Path = INSTALLED_APP) -> Path:
    """`Contents/MacOS/ImageView` inside `app`."""
    return app / "Contents" / "MacOS" / paths.APP_NAME


def plist_path(label: str) -> Path:
    """`~/Library/LaunchAgents/<label>.plist`."""
    return Path.home() / "Library" / "LaunchAgents" / f"{validate_label(label)}.plist"


def build_plist(
    label: str = paths.UI_AGENT_LABEL,
    app: Path = INSTALLED_APP,
    *,
    args: tuple[str, ...] = (),
    log_prefix: str = "ui",
    log_dir: Path | None = None,
) -> dict[str, object]:
    """The LaunchAgent definition, as a plain dict `plistlib` can dump.

    Serves both agents the app installs: the menu bar (no `args`) and the
    display (`args=("--display",)`). The two differ only in the argv and
    the log filenames; everything else — `KeepAlive{SuccessfulExit:
    false}`, `RunAtLoad`, `ProcessType Interactive` — is identical and is
    correct for both. `ProcessType Interactive` matters especially for the
    display: it is what puts the job in a session with a window-server
    connection, without which the display cannot draw at all.

    Separated from writing it so the *contents* are testable without
    touching `~/Library/LaunchAgents/`.

    No `WorkingDirectory`: nothing in the bundle resolves anything
    relative to cwd. Every path the app uses comes from `paths.py`, which
    is anchored to `Path.home()` and `sys.executable`.
    """
    validate_label(label)
    logs = paths.log_dir() if log_dir is None else log_dir
    return {
        "Label": label,
        # The bundle's single executable, dispatched by argv: no args is
        # the menu bar, `--display` is the display agent. `open -a` is
        # ruled out — it returns immediately and launchd would see a job
        # that instantly exits.
        "ProgramArguments": [str(executable_path(app)), *args],
        "RunAtLoad": True,
        # `true` would defeat the Quit item (menu bar) / would respawn a
        # deliberately-stopped display; `false` leaves a *crash* gone
        # until login. `{SuccessfulExit: false}` gives both the clean
        # behaviours. This is why the Quit item, which boots the display
        # out with a clean exit, actually stays stopped.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_INTERVAL_S,
        "StandardOutPath": str(logs / f"{log_prefix}.stdout.log"),
        "StandardErrorPath": str(logs / f"{log_prefix}.stderr.log"),
        "ProcessType": "Interactive",
    }


def write_plist(path: Path, data: dict[str, object]) -> bool:
    """Serialise `data` to `path` atomically. Returns success.

    Never raises, per this project's standing convention for writes —
    the caller reports the failure and the user's existing setup is left
    exactly as it was, which is the right outcome for a partial install.
    """
    try:
        paths.ensure_dir(path.parent)
        atomic_write_bytes(path, plistlib.dumps(data), suffix=".plist")
    except (OSError, TypeError, ValueError) as exc:
        print(f"ui_agent.py: could not write {path} ({exc}).", file=sys.stderr)
        return False
    return True


# -- launchctl ---------------------------------------------------------


def _launchctl(args: list[str], *, timeout: float = 15.0) -> tuple[int, str]:
    """Run one launchctl subcommand. argv list, never `shell=True`.

    Returns `(returncode, stderr)`. A failure to run launchctl at all is
    reported as a non-zero code rather than raised, so every caller has
    exactly one thing to check.
    """
    try:
        done = subprocess.run(
            ["/bin/launchctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, done.stderr.strip()


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


def bootout(label: str) -> bool:
    """Unload `label`, treating "not loaded" as success.

    `bootout` exits non-zero when the service is not loaded — i.e. for
    every user except the one who already had it installed. Treating
    that as failure is what makes a `set -e` installer abort on the
    common path, which is called out specifically. The relevant code is
    113 (`EINPROGRESS`/no such process in this domain); 3 also shows up
    for an unloaded label on some releases, so both are accepted, and
    the error text is checked as a backstop rather than trusting the
    numbers alone.
    """
    validate_label(label)
    code, err = _launchctl(["bootout", f"{gui_domain()}/{label}"])
    if code == 0:
        return True
    if code in (3, 113) or "No such process" in err or "not find" in err:
        return True
    print(f"ui_agent.py: bootout {label} -> {code}: {err}", file=sys.stderr)
    return False


def bootstrap(label: str, path: Path) -> bool:
    """Load the plist at `path` into the GUI domain."""
    validate_label(label)
    code, err = _launchctl(["bootstrap", gui_domain(), str(path)])
    if code != 0:
        print(f"ui_agent.py: bootstrap {path} -> {code}: {err}", file=sys.stderr)
        return False
    return True


def kickstart(label: str, *, restart: bool = False) -> bool:
    """Start `label` (or restart it with `-k`)."""
    validate_label(label)
    args = ["kickstart"]
    if restart:
        args.append("-k")
    args.append(f"{gui_domain()}/{label}")
    code, err = _launchctl(args)
    if code != 0:
        print(f"ui_agent.py: kickstart {label} -> {code}: {err}", file=sys.stderr)
        return False
    return True


def set_enabled(label: str, enabled: bool) -> bool:
    """Enable or disable `label` **persistently** (checkboxes).

    `bootout` alone is not "off at login" and `bootstrap` alone is not
    reliably "on": launchd keeps a per-user disabled set that survives
    reboots and outranks a plist's `RunAtLoad`, and it is exactly the
    mechanism System Settings' Login Items uses. That is the same set
    the concern here is a checkbox that can read "on" while
    nothing runs — so the checkbox has to write the same switch the
    system UI writes, not a different one that merely looks similar.

    `enable`/`disable` are ordered around the load/unload deliberately:
    enable *before* bootstrap, because bootstrapping a disabled label
    fails; disable *after* bootout, because disabling a loaded job is
    what leaves the confusing half-state this function exists to avoid.

    Returns whether the *persistent* half succeeded. A failure to
    load/unload right now is reported but not fatal — the setting is
    still recorded and takes effect at the next login, which is what the
    checkbox promises.
    """
    validate_label(label)
    service = f"{gui_domain()}/{label}"
    if enabled:
        code, err = _launchctl(["enable", service])
        if code != 0:
            print(f"ui_agent.py: enable {label} -> {code}: {err}", file=sys.stderr)
            return False
        path = plist_path(label)
        if path.is_file() and not bootstrap(label, path):
            # Already loaded is the common reason and is not a failure.
            kickstart(label)
        return True
    if not bootout(label):
        print(f"ui_agent.py: could not unload {label} before disabling.", file=sys.stderr)
    code, err = _launchctl(["disable", service])
    if code != 0:
        print(f"ui_agent.py: disable {label} -> {code}: {err}", file=sys.stderr)
        return False
    return True


def install(
    label: str = paths.UI_AGENT_LABEL,
    app: Path = INSTALLED_APP,
    *,
    args: tuple[str, ...] = (),
    log_prefix: str = "ui",
) -> bool:
    """Write the plist and (re)load it. Safe to run repeatedly.

    Order matters: `bootout` first — ignoring "not
    loaded" — *then* write, then `bootstrap`. Writing first would leave
    launchd holding the old definition while the file on disk says
    something else, which is the state that produces "I reinstalled it
    and nothing changed".
    """
    validate_label(label)
    executable = executable_path(app)
    if not executable.is_file():
        print(
            f"ui_agent.py: {executable} does not exist. Build and install "
            f"the app before installing the agent.",
            file=sys.stderr,
        )
        return False

    path = plist_path(label)
    if not bootout(label):
        return False
    if not write_plist(path, build_plist(label, app, args=args, log_prefix=log_prefix)):
        return False
    if not bootstrap(label, path):
        return False
    print(f"ui_agent.py: installed {label} -> {executable} {' '.join(args)}".rstrip())
    return True


def install_display(app: Path = INSTALLED_APP) -> bool:
    """Install and start the **display** agent — the process that draws
    pictures on the View. `--display` selects it out of the bundle's
    single executable.

    This is the piece a fresh install needs and v1.0.0 shipped without:
    nothing generated a display-agent plist, so `startDisplay_` and
    first-run both dead-ended at "No display agent installed" on any
    machine where one had not been placed by hand.
    """
    return install(
        paths.DISPLAY_AGENT_LABEL,
        app,
        args=("--display",),
        log_prefix="display",
    )


def uninstall(label: str = paths.UI_AGENT_LABEL) -> bool:
    """Unload the agent and remove its plist.

    `bootout` alone is not uninstall: it does not remove the plist, so
    the agent re-bootstraps at the next login and the user concludes it
    cannot be removed.
    """
    validate_label(label)
    ok = bootout(label)
    path = plist_path(label)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"ui_agent.py: could not remove {path} ({exc}).", file=sys.stderr)
        return False
    print(f"ui_agent.py: removed {label}")
    return ok


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    if command == "install":
        return 0 if install() else 1
    if command == "uninstall":
        return 0 if uninstall() else 1
    print(f"usage: {Path(sys.argv[0]).name} install|uninstall", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
