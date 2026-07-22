# display/ — Napster View 2D dashboard

Circular image-rotation dashboard for the Napster View. Pulls from Image
Studio's starred pool (a self-hosted image service), crossfades between images on a
timer, runs unattended as a LaunchAgent (a pre-existing source-tree LaunchAgent label).
This is Phase 2 of view-lab — the proof-of-concept deliverable, no 3D.

Full design rationale and build history:
`docs/plans/phase-2-2d-dashboard.md`. That document's "Build Progress"
section at the top has the real, dated story of what was built, what
broke, and how it was fixed — read it before changing anything here, not
just this file.

## Two things you need to know before touching this

**1. This is a window, not wallpaper.** It would be reasonable to expect
`NSWorkspace.setDesktopImageURL` (i.e. "just set it as the desktop
picture") to be simpler. It was tried in design and rejected:
there's no reliable forced-redraw hook, and macOS's own wallpaper
caching/cross-fade behavior fights a deterministic rotation timer — you'd
be fighting the OS's redraw scheduling on every tick instead of owning
it. Instead `window.py` draws a borderless, always-on-top `NSWindow` on
the View's own `NSScreen`, with a custom `drawRect_` doing the circular
clip. Full control, no fighting anything.

**2. Anything that draws or captures needs the physical console — never
SSH.** Confirmed the hard way in Phase 1 (`probe/DECISION.md` §8): an
SSH-attached shell has no window-server identity. The failure mode isn't
an error — a script can run to completion, exit 0, and have drawn or
captured nothing real (Phase 1 lost a day to `screencapture` silently
returning a placeholder wallpaper image under SSH). This applies to
`app.py`, `pattern.py`, `smoke_test.py`, `window.py`, `demo_transition.py`
— anything that opens a window or calls `screencapture`. Run these only
from a Terminal at the mini's own physical console, or a Screen Sharing
session with control (not just observe). This is also why this project's
own Claude Code sessions can't verify optics or window-server behavior
themselves — see `GUARDRAILS.md`.

## Current running configuration

Two separate config files, two separate purposes — don't confuse them:

- **`display/config/settings.json`** — timing/behavior knobs: rotation
  interval, poll interval, crossfade duration, which image pool to draw
  from, cache size ceiling. Current live values (read from the file
  itself — this file is the source of truth if they've since changed):

  ```json
  {
    "rotation_interval_s": 900,   // rotate every 15 min
    "poll_interval_s": 1800,      // poll Image Server every 30 min
    "fade_duration_s": 2.0,       // 2s crossfade
    "pool": "starred",            // only starred images
    "cache_max": 300
  }
  ```

- **`display/config/calibration.json`** — where the physical circle
  actually is on the 960x960 framebuffer (`center_x`, `center_y`,
  `radius_px`, plus a `safety_margin_pct` shrink applied on top). This
  doesn't change unless the hardware itself changes — see
  Recalibration below.

## Install / start / stop / update / uninstall

Full command reference: `display/launchd/README.md` — read that before
doing any of this for real. The gist: it's a LaunchAgent
(a pre-existing source-tree LaunchAgent label), managed with the usual
`launchctl bootstrap` (install + start), `launchctl kickstart -k`
(force-restart to pick up an update), and `launchctl bootout`
(uninstall) — all from the physical console, same rule as above.
Installing/uninstalling is its own approval gate, separate from having
the code — don't run those commands without the owner's go-ahead.

## Recalibration

Redo this if the physical device is ever moved, replaced, or the visible
image looks off-center / clipped against the bezel.

1. At the console (not SSH), run `./.venv/bin/python3 pattern.py` to draw
   the labeled test pattern, then photograph the device per the workflow
   in `display/STEP1_INSTRUCTIONS.md` (straight-on, steady, watch for
   glare — the lenticular surface is more glare-prone than a flat panel).
2. Read the four bezel-crossing offsets off the photo and compute
   `center_x`, `center_y`, `radius` from the four offsets (each axis is
   the midpoint of its two bezel crossings; the radius is half the span).
3. Validate with `./.venv/bin/python3 pattern.py --validate <x> <y> <r>`
   — confirm the green ring tracks the bezel on the live device before
   trusting the numbers.
4. Update `display/config/calibration.json`'s `circle` block
   (`center_x`, `center_y`, `radius_px`) with the new numbers. Leave
   `safety_margin_pct` alone unless you have a specific reason to change
   it.
5. Restart the service so it picks up the change — calibration is
   load-at-startup only, no hot-reload (`launchctl kickstart -k`, see
   `display/launchd/README.md`).

## Troubleshooting

- **Logs:** `display/logs/display.stdout.log` and
  `display/logs/display.stderr.log`. Rotated (rename + truncate) past
  10MB, checked once at each `RunAtLoad` — not continuously, so a log can
  still grow past 10MB mid-run before the next restart catches it.
  stderr is where an exception traceback will show up if something's
  crash-looping.
- **Status file:** `display/state/status.json` — small JSON blob updated
  live: last poll time/success/count, last image shown and when. Quick
  way to tell if polling is actually succeeding without digging through
  logs.
- **Is it actually running:**
  `launchctl print gui/$(id -u)/the pre-existing source-tree agent label` — look for
  `state = running`. Or just look at the physical device; a frozen image
  reads as "hasn't rotated in a while," not "broken" (by design — every failure degrades toward showing the last good frame, never
  blank).

## File layout

```
display/
├── config/
│   ├── settings.json       # rotation/poll/fade timing, pool choice, cache size
│   └── calibration.json    # circle center/radius on the 960x960 framebuffer
├── calibration.py          # loads + validates calibration.json, safe fallback
├── display_target.py       # resolution-match: finds the View's NSScreen
├── image_pool.py           # Image Server client — GET /api/images only
├── cache.py                # local image cache: manifest, download, prune
├── rotation.py             # pure state machine — shuffled walk order, no AppKit
├── window.py               # borderless NSWindow + circular-clip drawing
├── app.py                  # full event loop — wires everything above together,
│                            #   this is what the LaunchAgent runs
├── settings.py             # loads + validates settings.json, safe fallback
├── atomic_io.py            # shared atomic write-then-rename helper
├── log_rotation.py         # size-bounded log rotation, checked at startup
├── pattern.py               # Step 1 calibration test pattern + validation mode
├── smoke_test.py             # Step 0/0b throwaway window smoke test
├── demo_transition.py        # one-off crossfade demo, not part of the real app
├── launchd/                  # plist templates + their own README
├── cache/                    # gitignored — downloaded images + manifest.json
├── state/                    # gitignored — rotation_state.json, status.json
└── logs/                     # gitignored — LaunchAgent stdout/stderr
```
