# The menu bar — installing, quitting, rebuilding, removing

The menu bar is `ImageView.app`. It is a real installed application in
`/Applications`, and a LaunchAgent (`dev.viewlab.imageview.ui`) starts it
at login so it survives logout, reboot, and closing the Terminal that
happened to launch it. Before this, it ran as
`display/.venv/bin/python3 ui/menubar.py` from an interactive shell and
died with the session.

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

The display agent has its own separate lock (`~/.viewlab/display.lock`).
The two never contend.

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
- `~/Library/Logs/ImageView/` — `ui.stdout.log`, `ui.stderr.log`.
- `~/Library/Caches/dev.viewlab.imageview/` — downloaded images.
  Regenerable; delete freely.

Removing the app does **not** touch the display agent. That is still the
pre-existing a pre-existing source-tree LaunchAgent label LaunchAgent running from the
source tree, and it is deliberately out of scope here.

## Where things are

| What | Where |
|---|---|
| The app | `/Applications/ImageView.app` |
| Its executable | `Contents/MacOS/ImageView` (no args = menu bar, `--display` = display agent) |
| LaunchAgent plist | `~/Library/LaunchAgents/dev.viewlab.imageview.ui.plist` |
| Logs | `~/Library/Logs/ImageView/ui.{stdout,stderr}.log` |
| Instance lock | `~/.viewlab/ui.lock` |
| Menu bar icon | `Contents/Resources/menubar-template.pdf` (inside the bundle, not the source tree) |

## Checking on it

```sh
launchctl print gui/$(id -u)/dev.viewlab.imageview.ui
```

The useful lines are `state`, `pid`, and `last exit code`. A `state = not
running` with `last exit code = 0` is the normal, healthy state after you
chose Quit — not a failure.
