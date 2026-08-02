#!/bin/bash
#
# make_release.sh — build a shippable ImageView.app and ImageView.dmg
# (plan §6.4 / §6.5). This is THE release path. `ui/README.md`'s in-place
# recipe is the fast local loop for testing a code change and must not be
# used for anything a stranger will run.
#
#   packaging/make_release.sh [BUILD_ROOT]      default: /tmp/ivbuild
#
# Everything happens under BUILD_ROOT. The repo working tree is never
# read through `git ls-files`, and is never written to.
#
# Why a clean checkout at a neutral path is a hard requirement, not advice
# --------------------------------------------------------------------
# Since the §6.4 package conversion `display/` is a real package, so
# py2app filesystem-copies the *whole directory* into the bundle. An
# in-place build therefore ships `display/.venv`, `display/logs/`,
# `display/cache/`, `display/state/` and every stale `__pycache__/*.pyc`
# — each `.pyc` carrying `co_filename = $HOME/dev/view-lab/...`.
# Measured: in-place leaks `$HOME` into 1114+ files; the same build
# from a clean tracked-files-only checkout leaks 0.
#
# The build venv also lives under BUILD_ROOT, and that is not incidental:
# py2app writes the build interpreter's absolute path into the bundle's
# `Info.plist`. A venv at `$HOME/dev/view-lab/.venv-build` puts the
# username in the one file every user can read from Finder.
#
# The gates in VERIFY below are gates, not warnings. A build that leaks
# exits non-zero and produces no .dmg.

set -euo pipefail

BUILD_ROOT="${1:-/tmp/ivbuild}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SRC="$BUILD_ROOT/src"
VENV="$BUILD_ROOT/venv"
STAGE="$BUILD_ROOT/stage"
OUT="$BUILD_ROOT/out"
APP="$SRC/packaging/dist/ImageView.app"
DMG="$OUT/ImageView.dmg"

say() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------
# 1. Clean checkout at a neutral path
# ---------------------------------------------------------------------
say "clean checkout -> $SRC"
rm -rf "$BUILD_ROOT"
mkdir -p "$SRC" "$OUT"

# Tracked files only — that is the whole mechanism. .venv, logs/,
# cache/, state/, __pycache__/ and .claude/ are all untracked or
# ignored, so they cannot come along by accident.
#
# `git ls-files` on the working tree rather than `git archive HEAD`:
# both give exactly the tracked set, but ls-files ships what you are
# looking at. `archive HEAD` silently builds the last commit instead,
# which turns "I changed setup.py and rebuilt" into a confusing no-op.
# The cost is that a dirty tree can be released, so say so, loudly.
git -C "$REPO" ls-files -z | tar --null -T - -cf - -C "$REPO" | tar -x -C "$SRC"

COMMIT="$(git -C "$REPO" rev-parse --short HEAD)"
DIRTY="$(git -C "$REPO" status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  printf '\n  *** WORKING TREE IS DIRTY — this build is not reproducible from %s\n' "$COMMIT"
  printf '  *** commit before cutting a release the SHA-256 will be published for\n'
  printf '%s\n' "$DIRTY" | sed 's/^/      /'
  printf '\n'
fi
# Reproducibility: hdiutil and py2app both stamp mtimes otherwise.
SOURCE_DATE_EPOCH="$(git -C "$REPO" log -1 --format=%ct)"
export SOURCE_DATE_EPOCH
echo "commit $COMMIT  SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"

# ---------------------------------------------------------------------
# 2. Build venv, at a neutral path (see header)
# ---------------------------------------------------------------------
# BEFORE the prune, and that ordering is load-bearing: gate 0 below runs
# the test suite, and the prune deletes every test file. See gate 0.
say "build venv -> $VENV"
# PINNED, not `/usr/bin/env python3`. The interpreter is part of the
# shipped artifact — py2app embeds a whole Python framework in the
# bundle — so letting it float means the release silently changes what
# users run whenever Homebrew moves `python3`. That already happened
# once: 3.13 -> 3.14 between v1.0.1 and this build, noticed only because
# an unrelated gate had the old version hardcoded and failed.
#
# Overridable for a deliberate, tested interpreter bump; the point is
# that bumping it is a decision someone makes, not weather.
VIEWLAB_PYTHON="${VIEWLAB_PYTHON:-/opt/homebrew/opt/python@3.13/bin/python3.13}"
[ -x "$VIEWLAB_PYTHON" ] || fail "no interpreter at $VIEWLAB_PYTHON (set VIEWLAB_PYTHON to override)"
echo "  interpreter: $VIEWLAB_PYTHON ($("$VIEWLAB_PYTHON" --version 2>&1))"
"$VIEWLAB_PYTHON" -m venv "$VENV"
# Deliberately NOT the `pyobjc` umbrella: modulegraph would chase ~200
# framework wrappers (~70MB) into the bundle (§6.5).
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet py2app pyobjc-core pyobjc-framework-Cocoa httpx
"$VENV/bin/python" -c "import py2app; print('py2app', py2app.__version__)"

# ---------------------------------------------------------------------
# 3. GATE 0 — the test suite, against the tree that is about to ship
# ---------------------------------------------------------------------
# make_release.sh would otherwise happily produce a signed, swept,
# shippable .dmg from a tree whose tests are red. Not hypothetical: a red
# commit shipped once because the suite was run after the push.
#
# 🔴 It runs HERE, not with gates 1-6, and the position is the whole
# point. The prune below deletes `display/test_*.py` and `ui/test_*.py`,
# so the same command a hundred lines later would discover ZERO tests —
# a check that measures nothing, this project's most repeated failure
# shape. The printed count is the tripwire: if it is not a few hundred,
# this gate has quietly stopped working.
#
# 🔴 Against $SRC, not $REPO. The working tree is not what gets packaged,
# and a gate that tests something other than the artifact is the same
# class of bug as sweeping the .app while shipping the .dmg.
#
# PYTHONDONTWRITEBYTECODE is not tidiness. Running the suite inside $SRC
# writes `__pycache__` next to the sources, and the prune removes
# `display/test_*.py` but not `display/__pycache__/test_*.pyc` — so
# py2app's directory copy shipped the COMPILED test suite into the
# bundle. Caught by gate 2 the first time this gate ran:
# `test_read_token.cpython-313.pyc` carried a 64-hex fixture and tripped
# the secret-shape term. Belt here, braces in the prune below.
#
# One assertion in test_release_gate skips here: $SRC has no `.git`, so
# manifest tracked-ness cannot be checked (it is asserted in the dev
# repo). Everything else runs, so the manifest, HARD-term and
# SOFT-exception checks all run against the exact tree being packaged.
say "gate 0/7: test suite (against \$SRC, before the prune removes it)"
TESTLOG="$BUILD_ROOT/tests.log"
if ! (cd "$SRC" && PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" \
        -m unittest discover -t . -s . -p "test_*.py") \
     >"$TESTLOG" 2>&1; then
  tail -40 "$TESTLOG"
  fail "test suite failed — refusing to build (full log: $TESTLOG)"
fi
TESTCOUNT="$(grep -oE '^Ran [0-9]+ test' "$TESTLOG" | grep -oE '[0-9]+' | head -1)"
echo "  $(tail -1 "$TESTLOG")"
[ "${TESTCOUNT:-0}" -ge 100 ] \
  || fail "gate 0 discovered only ${TESTCOUNT:-0} tests — it is not running the suite"
echo "  $TESTCOUNT tests passed"

# ---------------------------------------------------------------------
# 4. Prune first-party dev artifacts that have no business in a shipped app
# ---------------------------------------------------------------------
# These are tracked files, so the checkout brings them, and py2app's
# directory copy of `display/` would ship them verbatim. Each is
# verified present inside the currently installed bundle.
#
#   display/launchd/      — the two plists hardcode $HOME in
#                           ProgramArguments/WorkingDirectory/Std*Path
#                           AND carry personal-handle labels, which §6.6
#                           puts on the §11 HARD grep list. The README
#                           beside them documents the old source-tree
#                           install and points at "how Helm services on
#                           this machine are already updated" (§7c), so
#                           the whole directory goes, not just the two
#                           plists.
#   STEP{0,1}_INSTRUCTIONS.md — dev docs; contain owner-specific notes ("at the
#                           physical console"), which the §11 SOFT
#                           term list covers.
#   display/test_*.py     — the test suite is not shipped software. It
#                           also carries private hostname and LAN-IP
#                           fixtures (§7a) into a user-readable file.
#   display/demo_transition.py — a hand-run dev scratch script.
#   display/README.md, ui/README.md — developer docs. Both ship: the
#                           first through the package copy, the second
#                           *inside* python3NN.zip. `ui/README.md`
#                           documents this repo's own paths and carried
#                           `$HOME` into the bundle; both carry
#                           a personal-handle label and a private hostname.
#                           A shipped app does not need rebuild notes.
#
# Pruning here rather than in setup.py is deliberate: setup.py's
# `includes:` list already controls what is *imported*, but py2app's
# package copy ignores it, so the only reliable filter is the build
# tree's contents.
say "prune dev artifacts from the build tree"
rm -rf "$SRC/display/launchd"
rm -f "$SRC/display/STEP0_INSTRUCTIONS.md" "$SRC/display/STEP1_INSTRUCTIONS.md"
rm -f "$SRC"/display/test_*.py "$SRC"/ui/test_*.py
rm -f "$SRC/display/demo_transition.py"
rm -f "$SRC/display/README.md" "$SRC/ui/README.md"
# Any __pycache__ in the build tree, whatever produced it. py2app copies
# `display/` as a directory and would ship these verbatim — including
# compiled copies of the very test files pruned above, whose source is
# gone but whose .pyc is not. See gate 0.
find "$SRC" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true


# ---------------------------------------------------------------------
# 5. Build
# ---------------------------------------------------------------------
say "py2app build"
(cd "$SRC/packaging" && "$VENV/bin/python" setup.py py2app >"$BUILD_ROOT/build.log" 2>&1) \
  || { tail -40 "$BUILD_ROOT/build.log"; fail "py2app build failed (see $BUILD_ROOT/build.log)"; }
[ -d "$APP" ] || fail "no bundle at $APP"
echo "built $(du -sh "$APP" | cut -f1)"

# ---------------------------------------------------------------------
# 6. Ad-hoc sign — AFTER the last file is written
# ---------------------------------------------------------------------
# Mandatory on Apple Silicon: the kernel kills an unsigned arm64 binary
# at exec. NO `--options runtime`: hardened runtime without a real
# identity blocks the bundle from loading its own dylibs.
say "ad-hoc sign"
codesign --force --deep --sign - "$APP"

# ---------------------------------------------------------------------
# 7. VERIFY — §6.5 gates. These fail the build; they do not warn.
# ---------------------------------------------------------------------
say "gate 1/7: no /opt/homebrew linkage in lib-dynload"
# If macholib misses one (most often _ssl, _sqlite3, _hashlib) it fails
# only on a machine without Homebrew — i.e. every recipient — and it
# fails on HTTPS, which is the whole point of the URL sources.
BREW_COUNT="$(otool -L "$APP"/Contents/Resources/lib/python*/lib-dynload/*.so 2>/dev/null \
  | grep -c /opt/homebrew || true)"
echo "  /opt/homebrew references: $BREW_COUNT"
[ "$BREW_COUNT" -eq 0 ] || fail "$BREW_COUNT Homebrew dylib references in lib-dynload"

say "gate 2/7: whole-bundle binary-aware identity sweep (§11 assertion 4)"
# Delegated to `release_gate.py` rather than reimplemented here. This
# script used to carry its own narrower grep, and the two checks drifted
# — which is exactly how §11's original wording ended up certifying a
# leaking bundle. One list, one scanner, two callers.
#
# What the shared scanner gets right, each learned the hard way:
#   - it reads binary files instead of skipping them. `grep -rl`
#     silently skips binary .pyc and reported 1 file where there were
#     130.
#   - it extracts and sweeps zip members. py2app byte-compiles `ui/`
#     and `display.sources` into `python3NN.zip`, and DEFLATE hides
#     plaintext from grep exactly the way a .pyc hides it from
#     `grep -r`. `ui/README.md` rode inside that zip carrying a home
#     directory path while the sweep reported 0.
#   - it checks the FULL HARD term list, not just the home directory.
"$VENV/bin/python" "$REPO/release_gate.py" --sweep-bundle "$APP" \
  || fail "first-party identity leak in the bundle"

say "gate 3/7: every first-party module is actually in the bundle"
# setup.py names modules explicitly (see its docstring). A hand-kept
# list silently rots: modulegraph does not follow a lazy import, so a
# module that moves behind one — as `ui.calibrate_window` already has —
# vanishes from the bundle and fails only when a user opens that window.
# This compares the pruned build tree against what shipped.
# Listed once into a file, not piped into `grep -q` per module: under
# `set -o pipefail` an early-exiting `grep -q` SIGPIPEs `unzip`, the
# pipeline reports failure, and every module looks missing.
ZIPLIST="$BUILD_ROOT/ziplist.txt"
unzip -l "$APP"/Contents/Resources/lib/python3*.zip >"$ZIPLIST" 2>/dev/null || true

# Derived, never hardcoded. This line used to read `python3.13`, and when
# Homebrew's `python3` moved to 3.14 the path stopped existing — so every
# module looked absent and the gate failed the build with a message
# ("present in the build tree but absent from the bundle") that pointed
# at the bundle rather than at itself. A version pinned in one gate and
# floating everywhere else is a tripwire for the toolchain, not for the
# bundle.
PYLIB="$(ls -d "$APP"/Contents/Resources/lib/python3.* 2>/dev/null | head -1)"
[ -n "$PYLIB" ] || fail "no lib/python3.* directory in the bundle"
echo "  bundle stdlib: $(basename "$PYLIB")"

MISSING=""
for f in "$SRC"/display/*.py "$SRC"/display/sources/*.py "$SRC"/ui/*.py; do
  [ -e "$f" ] || continue
  rel="${f#"$SRC"/}"
  stem="$(basename "$rel" .py)"
  pkgdir="$(dirname "$rel")"
  # Two shapes: `display/` is filesystem-copied as source, `ui/` and
  # `display.sources` are byte-compiled into python3NN.zip.
  [ -e "$PYLIB/$rel" ] && continue
  grep -q " $pkgdir/$stem\.pyc\$" "$ZIPLIST" && continue
  MISSING="$MISSING $rel"
done
if [ -n "$MISSING" ]; then
  for m in $MISSING; do echo "    $m"; done
  fail "modules present in the build tree but absent from the bundle"
fi
echo "  all first-party modules present"

say "gate 4/7: --selftest from inside the bundle"
"$APP/Contents/MacOS/ImageView" --selftest || fail "selftest failed"

say "gate 5/7: signature"
codesign --verify --deep --strict --verbose=2 "$APP"

# ---------------------------------------------------------------------
# 8. .dmg with a drag-to-Applications layout
# ---------------------------------------------------------------------
say "dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/ImageView.app"
ln -s /Applications "$STAGE/Applications"
# The .DS_Store implementing the drag layout is created BY the layout
# step, so it must be deleted after staging and before hdiutil — not
# earlier (§6.5).
find "$STAGE" -name .DS_Store -delete
rm -f "$DMG"
hdiutil create -volname "ImageView" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"

say "gate 6/7: sweep the .dmg itself (the thing that actually ships)"
# This gate runs LAST because it is the only one that can: every earlier
# check ran against an *input* to the artifact, and the artifact did not
# exist until the line above. The .app was swept, then copied, staged,
# symlinked and compressed — and nothing re-checked the result.
#
# Scanning $DMG's own bytes would prove nothing: UDZO is compressed, so
# its contents hide from a byte scan exactly the way a DEFLATE zip member
# and a .pyc already did twice in this project. release_gate mounts it.
#
# The .dmg is deleted on failure. A leaking artifact left on disk is one
# `gh release create` away from being published, and the whole point of a
# gate is that the bad output does not survive it.
"$VENV/bin/python" "$REPO/release_gate.py" --sweep-dmg "$DMG" || {
  rm -f "$DMG"
  fail "identity or secret leak in the .dmg (artifact deleted)"
}

# Hashed only after the sweep passes, and never rebuilt afterwards —
# py2app is not byte-reproducible, so a rebuild would invalidate this.
SHA="$(shasum -a 256 "$DMG" | cut -d' ' -f1)"

say "done"
cat <<EOF
  commit        $COMMIT
  app           $APP
  dmg           $DMG  ($(du -h "$DMG" | cut -f1))
  sha256        $SHA
  homebrew refs $BREW_COUNT
  identity      clean (release_gate.py, all HARD terms, zips included)

Install:
  # quit any running menu bar first — replacing a bundle under a live
  # process loads new code into old interpreter state (§6.6)
  rm -rf /Applications/ImageView.app
  cp -R "$APP" /Applications/ImageView.app
  launchctl kickstart -k gui/\$(id -u)/dev.viewlab.imageview.ui
EOF
