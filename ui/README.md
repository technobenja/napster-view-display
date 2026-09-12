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

🔵 **Corrected twice, and this is the current behaviour.** This section
used to say that launching an already-running ImageView "does nothing
visible", and that fixing it "needs a channel between the two UI
processes that does not exist yet". Neither is true any more.

There is no channel and none was needed: the arriving instance asks the
window server directly where the holder's status item is, and whether it
has one at all — no cooperation from the holder, and no permission
prompt.

**Launching an already-running ImageView now shows you a small window**,
placed directly underneath the running copy's menu bar icon, so you can
see which icon it means without being told a screen coordinate. It says
which of the situations below it found. The only one that closes itself
is "already running, and here is your icon", after eight seconds;
everything else carries an instruction and stays until you close it.

It never becomes the front window, it never takes your keyboard focus,
and it never asks you to confirm anything. **It is not an alert** — an
alert would be noise attached to a non-problem, and a modal alert is the
mechanism suspected in the fault this whole thing exists to explain.

A LaunchAgent respawn that loses the lock shows nothing at all. That is
routine housekeeping and you should not hear about it.

The same verdict also goes to `ui.stderr.log`, so it outlives the window:

| what it found | what it says |
|---|---|
| running, icon visible | already running — its icon is at these coordinates, click it |
| running, icon hidden | already running, but its only icon is hidden or is on the picture display — make room in the menu bar |
| **running, but it has never reported** | **almost certainly a copy older than v1.1.5, which never wrote a heartbeat at all — click the icon; if the menu opens, nothing is wrong** |
| running, cannot write `state/` | it cannot write `~/.viewlab/state/`, so it cannot say whether it is working; it may be perfectly healthy, and it recovers on its own |
| stopped, a window on screen | a window is open, possibly behind another one — look at your screen |
| stopped, nothing to explain it | not responding; **this pid**, and the other ImageView is the one drawing your pictures |
| the lock is held by something else | the pid is absent, foreign, or disagrees with the heartbeat file — reported, nothing assumed, nothing signalled |

The third row is the one most people will ever see, because it is what a
contended launch produces the first time you run a new copy while an old
one is still going. It is not a fault.

All of it matters most when the status item is **not** already there,
because then the holder is stuck rather than healthy and "the app will
not launch" is the only symptom you get.

It never acts on any of that. It does not quit, kill, or take over the
holder — an ordinary modal dialog freezes the heartbeat exactly the way a
wedge does, so "the stamp is stale" includes a perfectly healthy app that
is simply showing you something and waiting.

The measurement behind that sentence is an About box that froze the stamp
for **51 seconds**, on 2026-09-10. From v1.1.6 the About box is no longer
a modal, so **that particular reproduction no longer works** — the reading
is still right, and the windows that still do it are named under
*Telling a wedged menu bar from a healthy one* below.

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

**From v1.1.6 the file carries four more fields**, written by that same
timer in the same atomic write, so they always describe the same instant
as the stamp:

| field | what it is for |
|---|---|
| `pid` | which process was serving. A *stale* file then names the process that stopped, which is the thing a diagnosis starts from |
| `exe` | its executable path, so a recycled pid cannot be mistaken for a live menu bar |
| `stacks_armed` | whether `kill -USR1` on that pid is a stack dump or a **kill** — see below |
| `stacks_path` | where the dump would land |

Older readers ignore fields they do not know, and this reader treats all
four as optional: a v1.1.5 file carrying only the stamp still reads
correctly. The timer remains the **only** writer, which is what keeps the
file's existence meaningful — it exists because something serviced a
timer, not because something intended to.

Three things this does **not** mean:

- **A missing file is not by itself a wedge.** Older builds never wrote
  one, and a menu bar that has not yet reached its run loop has not
  written one yet either. Check that the process exists at all first
  (`pgrep -fl ImageView`).
- **A stale stamp while a menu or certain windows are on screen is
  EXPECTED — not a fault.** The stamp is written from a timer registered
  in the default run-loop mode, and measurement says such a timer fires
  **zero** times while the run loop is tracking a menu or running a modal
  panel. That is deliberate: it is exactly what lets a stale stamp catch
  a modal that has wedged. The cost is that it cannot distinguish that
  from a modal somebody opened on purpose. **Look at the screen before
  concluding anything**, and never kill the process on this signal alone.

  **From v1.1.6 onward, only some of this app's windows still do it.**
  The list shrank twice and is worth checking against your version rather
  than assuming:

  | on screen | freezes the stamp? |
  |---|---|
  | an open menu | **yes** — menus track in their own run-loop mode |
  | Settings' three alerts, and two of *Adjust the circle*'s | **yes** — still modal, deliberately |
  | *Adjust the circle*'s Save / Discard / Cancel question | **yes** — it has to block, something depends on the answer |
  | the About box | **no, from v1.1.6** — it was the 51-second measurement |
  | *Couldn't start showing pictures* | **no, from v1.1.6** |
  | *The View isn't connected* | **no, from v1.1.6** |
  | first run's *Couldn't save your settings* and *That source isn't complete* | **no, from v1.1.6** — sheets |
  | the folder picker | **no, from v1.1.6** — a sheet |
  | a settings window merely being open | **no, on any build** |

  **On builds before v1.1.6 every row above is yes except the last**, and
  the picker in particular is held open for as long as someone browses,
  so a thirty-second browse looked identical to a thirty-second hang.

  The rows that changed in v1.1.6 are the ones that could be reached with
  **no window of this app on screen to open in front of**. That is the
  shape that produced 2026-09-08, and one fewer benign cause of a stale
  stamp is one fewer way to read a healthy app as a broken one.
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

The eight gates fail the build rather than warn — a leaking or broken
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

🔴 **`stacks_armed` is only true of the pid the same file names.** Read
both, and signal *that* pid — never one found some other way:

```sh
PID=$(plutil -extract pid raw ~/.viewlab/state/ui_status.json) || exit
ARMED=$(plutil -extract stacks_armed raw ~/.viewlab/state/ui_status.json) || exit
LIVE=$(pgrep -f 'ImageView$')

[ "$ARMED" = true ] || { echo "stack dumps are NOT armed - do not signal"; exit; }
[ "$PID" = "$LIVE" ] || { echo "the file names pid $PID, but pid $LIVE is running - STOP"; exit; }

kill -USR1 "$PID"
plutil -extract stacks_path raw ~/.viewlab/state/ui_status.json
```

If any of that exits early, **stop** and do not send the signal.

**Why the pid comparison is the load-bearing half, not a nicety.** An
earlier version of this section read `stacks_armed` and then signalled a
pid from `pgrep`, on the reasoning that the file "describes the process
running now, or it is stale, and either way it cannot describe a
different one". That is wrong, and the case is ordinary rather than
exotic: nothing deletes this file when a process exits. Suppose v1.1.6
writes `stacks_armed: true, pid: 100` and then crashes, and you drag the
older bundle back — which is how this app downgrades, and the reason the
"has never reported" message below exists at all. The old build starts as
pid 200 and writes nothing. The file still says `true`. Signalling pid
200 **terminates it**, at the exact moment you were being careful not to
lose it.

`plutil` rather than `python3` or `jq` deliberately: it is part of macOS
and is present on a machine that has never had Xcode or Homebrew on it.
A troubleshooting instruction that needs a toolchain installed first is
not one a stranger can follow at the moment they need it.

🔵 This also replaces an instruction to
`grep 'kill -USR1' ~/Library/Logs/ImageView/ui.stderr.log`. That log is
append-only across every launch, so a line written by a healthy launch
last week survives a launch today that failed before arming — and the
build you are about to signal is, by definition, the one behaving
strangely.

The app makes the same two checks before it signals anything, and two
more the shell cannot easily make: it requires the pid in this file to
equal the pid in `ui.lock`, and it re-reads the holder's start time
immediately before signalling, so a pid recycled while it was thinking
cannot be mistaken for the process it looked at.

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
