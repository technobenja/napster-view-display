# The menu bar — installing, quitting, rebuilding, removing

The menu bar is `ImageView.app`, a real installed application in
`/Applications`.

**Scope: this document describes a source-tree install**, where the menu
bar's own LaunchAgent (`dev.viewlab.imageview.ui`) was installed by hand
with `ui/ui_agent.py install`. **A `.dmg` install does not have it** —
the app installs an agent for the *display* only, and the menu bar is
started by opening the app. So every `launchctl ... dev.viewlab.imageview.ui`
command below applies only if you installed that agent yourself; on a
`.dmg` install it answers `No such process`, which is correct and not a
fault.

It is a **separate process from the display agent** and neither can take
the other down. Force-quit the menu bar and the pictures keep rotating.

---

## Quit it

The `Quit` item in the menu. That is all — and it stays quit.

The LaunchAgent uses `KeepAlive { SuccessfulExit: false }`, which means
launchd respawns the app only when it exits *abnormally*. `Quit` exits 0,
so nothing brings it back until you ask. A plain `KeepAlive: true` would
have relaunched it within seconds of every Quit, which is a bug report
waiting to happen.

After quitting, the job is still **loaded**, just not running. That is
why the command to bring it back is `kickstart` and not `bootstrap`.

## Bring it back

```sh
launchctl kickstart gui/$(id -u)/dev.viewlab.imageview.ui
```

Or just open `ImageView` from `/Applications` (or Spotlight) — a
double-click does the same thing. Or log out and back in.

To force a restart of a running menu bar — the "it is up but wedged"
case — add `-k`:

```sh
launchctl kickstart -k gui/$(id -u)/dev.viewlab.imageview.ui
```

## Only one at a time

The menu bar takes an exclusive lock on `~/.viewlab/ui.lock` at startup.
If a second copy is launched — the LaunchAgent started one at login and
you then double-clicked the app — the second one exits immediately and
cleanly, and you keep the single status item you already had.

Without this you would get two identical icons in the menu bar with no
way to tell them apart, and two processes writing `command.json`.

**Known rough edge:** launching ImageView while it is already running
does nothing visible. No bounce, no window, no message — the status item
is simply already there. The right fix is for the second instance to
flash or highlight the existing status item before exiting, which needs a
channel between the two UI processes that does not exist yet. Being told
"already running" in an alert would be worse: it is noise attached to a
non-problem.

It is no longer *silent*, though: the second instance writes the reason —
which lock, which pid holds it, and that exiting 0 was deliberate — to
`ui.stderr.log` before it goes. That matters when the status item is
**not** already there, because then the holder is stuck rather than
healthy, and "the app will not launch" is the only symptom you get.

The display agent has its own separate lock (`~/.viewlab/display.lock`).
The two never contend.

## Telling a wedged menu bar from a healthy one

A lock proves the holder **exists**. It cannot prove the holder is
**serving** — `flock` is released only when the process dies, so a menu
bar that has stopped responding keeps the lock indefinitely, every
subsequent launch finds it held and exits cleanly, and the symptom is an
app that simply will not start.

So the menu bar stamps the time into `~/.viewlab/state/ui_status.json`
every couple of seconds, from the same timer that redraws the menu. If
that file is more than a few seconds old while the process is still
running, its run loop has stopped servicing timers — which is what a
wedge is, and what a lock file cannot tell you:

From a source checkout, ask the app's own reader rather than
reimplementing it — this is the same `read_ui_heartbeat` and the same
`is_stale` the app uses, so the answer here cannot drift from the answer
the app would give:

```sh
display/.venv/bin/python3 -c '
import time
from display import paths
from ui.menubar_state import is_stale, read_ui_heartbeat
beat = read_ui_heartbeat(paths.ui_status_path())
now = time.time()
print("no readable heartbeat on file" if beat == 0.0 else
      f"last beat {now - beat:.1f}s ago, stale={is_stale(beat, now)}")'
```

With only the installed app, `ls -l ~/.viewlab/state/ui_status.json` is
enough: the write is a rename, so the file's modification time *is* the
last beat. Neither form can raise on a machine that has no such file.

The display agent has carried the same stamp, as `heartbeat_at` in
`status.json`, since it was first written — this is the other half of it.

Three things this does **not** mean:

- **A missing file is not by itself a wedge.** Older builds never wrote
  one, and a menu bar that has not yet reached its run loop has not
  written one yet either. Check that the process exists at all first
  (`pgrep -fl ImageView`).
- **A stale stamp while a menu, the About box, a settings window or a
  file picker is on screen is EXPECTED — not a fault.** The stamp is
  written from a timer registered in the default run-loop mode, and
  measurement says such a timer fires **zero** times while the run loop
  is tracking a menu or running a modal panel. That is deliberate: it is
  exactly what lets a stale stamp catch a modal that has wedged. The cost
  is that it cannot distinguish that from a modal a user opened on
  purpose. **Look at the screen before concluding anything**, and never
  kill the process on this signal alone — browsing for a picture folder
  for thirty seconds looks identical to a thirty-second hang.
- **A stale stamp is not proof of a hang** for a second reason: if
  `~/.viewlab/state/` has become unwritable, a perfectly healthy menu bar
  goes stale. It says so in `ui.stderr.log` when that happens.

Nothing in the app acts on this file; it is there to be read by a person,
and by whatever gets built on top of it.

## Rebuild after a code change

Changing anything under `ui/`, `display/`, or `packaging/` requires a
rebuild — the installed app has its own copy of every module, so editing
the source tree changes nothing about what is running.

> **Do not build in place for anything you intend to ship.** Since the
> package conversion, `display/` is a real package, so py2app
> filesystem-copies the *whole directory* into the bundle — including
> `display/.venv`, `display/logs/`, `display/cache/`, and `.claude/`.
> Measured: an in-place build leaks `$HOME` into **1114** files.
> A neutral-path build was already required; the conversion turned that
> from advice into a hard requirement. The recipe below is the fast
> local loop for testing a code change, **not** the release path — that
> is [`packaging/make_release.sh`](#the-release-path), below.

```sh
# 1. build (from the repo root)
rm -rf packaging/build packaging/dist
.venv-build/bin/python packaging/setup.py py2app

# 2. ad-hoc sign, AFTER the last file is written
codesign --force --deep --sign - packaging/dist/ImageView.app

# 3. install
rm -rf /Applications/ImageView.app
cp -R packaging/dist/ImageView.app /Applications/ImageView.app

# 4. restart the menu bar so it picks up the new code
launchctl kickstart -k gui/$(id -u)/dev.viewlab.imageview.ui
```

Notes:

- **Step 2 is not optional.** On Apple Silicon an arm64 binary with no
  signature is killed by the kernel at exec. Do not add
  `--options runtime`: hardened runtime without a real identity blocks
  the bundle from loading its own dylibs.
- **Quit the app before step 3.** Replacing a bundle underneath a running
  process is unsafe — py2app imports lazily, so a running copy that hits
  its first import after the swap loads new code into old interpreter
  state and produces an incoherent traceback minutes later.
- If the build venv is missing:
  `python3 -m venv .venv-build && .venv-build/bin/pip install py2app
  pyobjc-core pyobjc-framework-Cocoa httpx`. Deliberately *not* the full
  `pyobjc` umbrella — modulegraph would chase ~200 framework wrappers
  into the bundle.
- `--selftest` is a fast check that the built bundle can do HTTPS and
  reach its own resources:
  `/Applications/ImageView.app/Contents/MacOS/ImageView --selftest`.

## The release path

Anything a stranger will run is built by one script, not by the recipe
above:

```sh
packaging/make_release.sh            # or: packaging/make_release.sh /tmp/somewhere
```

It does the whole job under `/tmp/ivbuild` and never writes to the repo:
clean tracked-files-only checkout at a neutral path, a build venv *also*
at a neutral path, prune, py2app, ad-hoc sign, five verification gates,
and a `.dmg` with a drag-to-Applications layout. It prints the `.dmg`'s
SHA-256, which is what goes in the README and the release notes — with
no Developer ID, the checksum is the user's only tamper-evidence.

Two things it does that the manual recipe cannot:

- **The build venv is at a neutral path too.** py2app writes the build
  interpreter's absolute path into `Info.plist`. Building with the
  repo's `.venv-build` put `$HOME/dev/view-lab/.venv-build/bin/python`
  in the one bundle file anyone can read from Finder. That was the last
  first-party leak, and it is invisible to any source-tree grep.
- **It prunes dev artifacts from the build tree.** py2app's directory
  copy of `display/` ignores `setup.py`'s `includes:`, so
  `display/launchd/` (hardcoded `$HOME` paths, a personal-handle label
  labels), `STEP{0,1}_INSTRUCTIONS.md`, and `test_*.py` all shipped
  inside the installed app until this script existed. The only reliable
  filter is what is on disk when py2app runs.

The seven gates fail the build rather than warn — a leaking or broken
build produces no `.dmg`:

| Gate | Passing means |
|---|---|
| 0 · Test suite | the whole suite passes, run against the *build* tree before anything is packaged. It runs here rather than with the gates below because the prune deletes `test_*.py`, so the same check later would discover **zero** tests and pass having measured nothing. The count is printed for exactly that reason |
| 1 · Homebrew linkage | `otool -L` over `lib-dynload/*.so` finds **0** `/opt/homebrew` references. Missing one fails only on machines without Homebrew — i.e. every recipient |
| 2 · Bundle identity sweep | **binary- and zip-aware**, reading inside `.pyc` and DEFLATE-compressed zip members. `grep -rl` silently skips binary files and once reported 1 file where there were 130 |
| 3 · Module coverage | every non-test module in the tree is really in the bundle. `setup.py` names modules by hand, and modulegraph does not follow a lazy import |
| 4 · `--selftest` | the bundled interpreter does HTTPS 200, finds `certifi`'s `cacert.pem`, loads PyObjC, and reaches its seed config |
| 5 · Signature | `codesign --verify --deep --strict` |
| 6 · `.dmg` sweep | the finished disk image, **mounted** and swept. Every other gate runs against an input to the artifact; this is the only one that sees the artifact. A compressed disk image hides its contents from a byte scan, so it has to be mounted |

Gate 0 earned its place immediately: adding it shipped the *compiled* test
suite into the bundle, because running the tests inside the build tree
writes `__pycache__` and the prune removed the test sources but not their
`.pyc`. Gate 2 caught it.

Gates 2 and 6 are driven by a `release_gate.py` that is deliberately not
part of this repository — it holds the catalogue of strings that must
never appear in a public build, so publishing it would defeat its own
purpose. Building from a clone therefore stops at gate 2. The `.app` and
`.dmg` steps themselves are ordinary py2app and `hdiutil`.

The build interpreter is **pinned** rather than following whatever
`python3` resolves to; override with `VIEWLAB_PYTHON`. It embeds a whole
Python framework in the bundle, so letting it float changes what users
run whenever Homebrew moves.

If the working tree is dirty the script still builds, but says so loudly
— a published SHA-256 should correspond to a commit.

If the agent's plist needs regenerating too (it points at the installed
path, so normally it does not):

```sh
display/.venv/bin/python3 ui/ui_agent.py install
```

That is safe to re-run: it boots out the old job, rewrites the plist, and
bootstraps the new one.

## Remove it entirely

```sh
display/.venv/bin/python3 ui/ui_agent.py uninstall
rm -rf /Applications/ImageView.app
```

`uninstall` boots the job out **and deletes the plist**. Both matter:
`bootout` alone leaves the plist in place, so the agent re-bootstraps at
the next login and it looks like it cannot be removed.

Left behind on purpose, because they are yours and not the app's:

- `~/.viewlab/` — `calibration.json` is hand-measured and is read by
  other tools, so nothing deletes it for you. `ui.lock` lives here too
  and is harmless.
- `~/Library/Logs/ImageView/` — `ui.stdout.log`, `ui.stderr.log`,
  `ui.stacks.log`.
- `~/Library/Caches/dev.viewlab.imageview/` — downloaded images.
  Regenerable; delete freely.

Removing the app does **not** touch the display agent. It has its own
LaunchAgent and keeps running; removing that is deliberately out of scope
here.

## Where things are

| What | Where |
|---|---|
| The app | `/Applications/ImageView.app` |
| Its executable | `Contents/MacOS/ImageView` (no args = menu bar, `--display` = display agent) |
| LaunchAgent plist | `~/Library/LaunchAgents/dev.viewlab.imageview.ui.plist` |
| Logs | `~/Library/Logs/ImageView/ui.{stdout,stderr,stacks}.log` |
| Instance lock | `~/.viewlab/ui.lock` |
| Menu bar icon | `Contents/Resources/menubar-template.pdf` (inside the bundle, not the source tree) |

## Checking on it

```sh
launchctl print gui/$(id -u)/dev.viewlab.imageview.ui
```

The useful lines are `state`, `pid`, and `last exit code`. A `state = not
running` with `last exit code = 0` is the normal, healthy state after you
chose Quit — not a failure.

If it is running but not responding — the icon is missing, or the menu
does not open, or Quit does nothing while Force Quit works — ask it where
it is stuck **before** killing it, because a Force Quit leaves no trace at
all:

**Check first that this build arms stack dumps.** They exist from
**v1.1.3** onward, and only once the app has started successfully. With
no handler installed, `SIGUSR1`'s default action is to **terminate the
process** — so on an older build this kills the very thing you were
being careful not to lose.

```sh
grep 'kill -USR1' ~/Library/Logs/ImageView/ui.stderr.log
```

If that names a pid and a path, dumps are armed. If it prints nothing,
**stop** — do not send the signal.

```sh
kill -USR1 $(pgrep -f 'ImageView$')
cat ~/Library/Logs/ImageView/ui.stacks.log
```

That dumps every thread's Python stack, and it works on a process too
stuck to run any Python of its own.

The display agent answers the same signal, into `display.stacks.log`.
`pgrep -f 'ImageView$'` deliberately matches the menu bar only, because
the display agent's command line ends in `--display` — so reach it with:

```sh
kill -USR1 $(pgrep -f 'ImageView --display')
```

**Read these before you share them.** A stack dump lists absolute source
paths, and `ui.stderr.log` records the absolute path of the instance
lock, so both contain your account name and something of your directory
layout. That is fine in a bug report you are happy to attach; check them
first if you are pasting into a public issue.

If a log is unexpectedly *empty*, look beside it for a `.old` — a known
rotation defect can move the live log there at startup, and everything
the running process writes goes to the `.old` copy.
