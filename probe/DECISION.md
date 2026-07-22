# Probe findings — fill this in, then STOP

Date run: 2026-07-16 (three passes: ~11:07, ~12:07, ~12:10-12:15)
Findings dir: findings/2026-07-16/

---

## 1. The View as a display

| Field | Value |
|---|---|
| Appears in SPDisplaysDataType | yes |
| Reported framebuffer resolution | 960 x 960 @ 60Hz, non-mirrored |
| Native resolution (from EDID) | not determined this session — see note below |
| EDID manufacturer / product ID | not determined this session — see note below |
| EDID physical size | not determined |
| macOS display index (for `-D`) | 2 (D1 = main DELL P1425, D2 = Napster View) |
| Other resolutions offered | not checked |

Note: EDID was attempted with a generic `ioreg -lw0 \| grep IODisplayEDID` and came
back empty. `probe/run-probe.sh` in this repo actually queries the right IOKit
classes (`ioreg -lw0 -r -c IODisplayConnect`, `-c AppleCLCD2`) — that targeted query
was not run this session and should be, next pass.

## 2. USB sidechannel

| Field | Value |
|---|---|
| Separate USB device present | yes |
| VID / PID | `Billboard Device`: LONTIUM, VID 0x2f61, PID 0x8846. Separate `USB BillBoard`: VID 0x291a, PID 0x8355 |
| Device class | USB Billboard class — standard USB-C DisplayPort/HDMI Alt-Mode descriptor |
| Bundled driver / dext | no separate HID/calibration companion device found |

**Per CLAUDE.md, this question is already closed by the owner** ("we own the pixels
already... do not propose USB packet capture, driver reverse engineering, or protocol
work"). Recorded here for completeness only — not treated as an open question, and no
packet capture or protocol work was attempted (guardrail #4). The Billboard device is
generic USB-C signaling, not evidence of Looking Glass-specific calibration hardware,
but per the owner's prior confirmation this doesn't matter for the pixel-ownership
question either way.

## 3. Interleave locus test  <- the important one

**Blocked by a tooling failure this session, not resolved.**

Three local `screencapture -D2` calls across 1+ hour (11:07, 12:07, 12:13) all
returned **byte-identical** frames (matching SHA1) showing plain macOS desktop, no
Napster content, despite `ps` confirming Napster running and consuming real CPU
throughout. This matches the exact failure mode `KICKOFF.md`/`CLAUDE.md` already
warned about: ungranted Screen Recording permission causes bad captures that look
like a device problem but aren't. Screen Recording permission for this session's
terminal needs to be (re-)granted and the terminal restarted before local capture can
be trusted — not done this session.

- [ ] Fine stripes / moiré / interlaced garbage → host-side interleave
- [x] Clean, normal 2D image → device-side interleave — **tentative, see caveat**
- [ ] Grid of ~5 near-identical tiles → device consumes a quilt
- [x] Local capture unreliable / stale — Screen Recording permission needs re-check

Attach or reference the capture file: local captures in `findings/2026-07-16/` are
all known-bad (stale). The evidence actually used came from a **user-provided Screen
Sharing screenshot** (`findings/2026-07-16/NapsterViewScreenCap_original.png`,
gitignored — see report) taken while the avatar was confirmed live on the View.

Notes on what you see (be specific — stripe angle, period, tile count): the
screenshot shows **one coherent portrait**, not a tiled grid — no repeated/near-
identical sub-images anywhere in the frame at any zoom level tried. A clean flat
background patch, scanned pixel-by-pixel, shows a smooth gradient with no strong
periodic banding at a regular pitch. Some fine vertical color fringing is visible in
flat regions (wall, forehead) but is at least as well explained by the Screen Sharing
session's own video compression (chroma subsampling / block quantization) as by
genuine subpixel-level interleave — **not confidently attributable either way** from
a capture that passed through a lossy remote-view codec rather than a raw local
framebuffer read.

## 4. App recon

Not yet done. Deprioritized this session while chasing the capture-tooling problem.
`/Applications/Napster.app` bundle has not been inspected for quilt geometry, view
count, or calibration constants.

## 5. Classification

Given the above, the 3D path is:

- [ ] Trivial — draw normally, firmware handles depth
- [ ] Easy — render an N-view quilt, no shader needed
- [ ] Medium — reimplement the interleave shader, derive calibration empirically
- [x] **Unclear** — leans toward "clean 2D / device-side" based on the one
      screenshot available, but not confirmed. A clean local framebuffer capture,
      taken after fixing Screen Recording permission, would very likely settle this
      outright (rules out host-side stripes/moiré definitively either way).

## 6. What you could NOT determine

- **Interleave locus, definitively** — blocked by broken local screencapture this
  session (see above). Best current evidence is a compressed remote-view screenshot,
  not a raw framebuffer read.
- **EDID identity** — wrong ioreg query used; the repo's own probe script queries the
  right IOKit classes and wasn't run.
- **Q2, the visible circular region** (center, radius, overscan) — not investigated
  at all this session. Needs a rendered test pattern plus an owner photograph of the
  physical device; cannot be determined from software alone.
- **App bundle recon** (quilt geometry / view count / calibration constants) — not
  inspected.
- **Whether the 3D actually resolves as depth** — inherent blind spot of all
  framebuffer-based tooling, local or remote. Only the owner's eye on the physical
  device can answer this, regardless of how clean a capture gets.

## 7. Recommended next step

Grant Screen Recording permission to whichever terminal hosts Claude Code (System
Settings → Privacy & Security → Screen Recording) and **restart that terminal** —
this is the one item KICKOFF.md's pre-flight checklist already flagged and it wasn't
done before this session's probe. Then re-run `probe/run-probe.sh` (using its own
targeted EDID query, not a generic grep) while the Napster avatar is confirmed
visible on the View, to get a real local framebuffer capture and settle the
interleave-locus classification outright. In parallel, render a circular test
pattern and ask the owner to photograph the physical device — that resolves Q2
(visible region) and is the only way to confirm the 3D effect optically, independent
of how the interleave question resolves.

## 8. Session 2 addendum (~13:02, same day) — real root cause found

Owner granted Screen Recording in System Settings and confirmed it. Re-ran the full
probe (`probe/run-probe.sh`) from this Claude Code session. Result: **still a
wallpaper-only placeholder frame, byte-different but content-identical to the prior
"stale" captures** — plain desktop background, `Activity Monitor` in the menu bar
(not the actual frontmost app), no window content, no Napster avatar. See
`findings/2026-07-16_130210/screen-2.png` (gitignored).

**This is not a stale-cache problem, and re-granting the permission in Settings will
not fix it.** This Claude Code session runs over **SSH**
(`sshd-session → zsh → claude → zsh`, confirmed via `ps -o pid,ppid,comm` walking the
parent chain from this shell). macOS's Screen Recording TCC grant attaches to a
*windowed* app's identity (Terminal.app, iTerm2, etc.) connected to the window
server. An SSH-attached shell has no such identity in its process tree, so
`screencapture` invoked from here structurally cannot receive live framebuffer
content — macOS silently substitutes the desktop-picture-only placeholder instead of
erroring, which is what produced the "stale," then "still-wallpaper" results across
both sessions.

**Corrected next step:** the probe's framebuffer capture step needs to run from a
Terminal window opened **locally at the mini's own console/GUI login**, not through
a remote Claude Code / SSH session. Options, cheapest first:
1. Owner opens Terminal.app directly on the mini (physically or via Screen Sharing
   with "control", not just view), grants **that specific Terminal.app** Screen
   Recording, restarts it, and runs `./probe/run-probe.sh` by hand there.
2. Owner pastes the resulting `screen-2.png` (or just describes/photographs what it
   shows) back for this session to analyze — this session can still do all the
   *analysis*, just not the *capture*.

The rest of this run's probe (displays, USB, Napster app bundle recon, drivers) ran
fine over SSH — none of that needs a windowed TCC grant. EDID queries
(`ioreg -lw0 -r -c IODisplayConnect` / `-c AppleCLCD2`) came back **empty** again,
this time from the repo's own targeted query, not a generic one — so EDID identity
looks structurally unavailable via `ioreg` on this hardware/OS combination, not a
wrong-query problem as previously assumed. App bundle recon indicated that the
app's rendering path is a conventional Metal/WebRTC video pipeline — real-time
camera capture, H.264 encode/decode, and GPU-backed view rendering — with
**nothing naming quilts, lenticular geometry, view counts, or calibration
constants** under the filtered keyword set. That absence is the finding: had the
host been responsible for interleaving, calibration constants would have to live
somewhere in the app, and they do not appear.

(Symbol names are deliberately not reproduced here. The conclusion is what
transfers; the extracted strings are third-party binary content and are not
redistributed in this repository.)

## 9. Session 3 (2026-07-16, ~15:09) — real local capture, Q1 resolved

Owner opened a genuine local Terminal at the mini's own console (confirmed via
`who`: an active `console` session, distinct from the `ttys000`/SSH session this
Claude Code instance runs in) and ran `./probe/run-probe.sh` from there with
Napster confirmed running. Result: **a real, live local framebuffer capture** —
`findings/2026-07-16_150935/screen-2.png`, SHA1 `ba684d5...`, distinct from every
prior capture's stale-placeholder hash (`455e65d...`). The image proves liveness
independent of the hash too: it shows a clock-face UI reading `15:09`, matching
the capture timestamp (`2026-07-16_150935`) to the minute, with a live circular
avatar insert — not the desktop-wallpaper placeholder every prior SSH-based
attempt produced.

**Interleave locus test, finally decisive:** the capture is **one coherent
image** — a clock face (date, time, tick-mark ring, circular avatar) — not a
grid of near-identical tiles. Zoomed 4x crops of flat gradient regions
(`findings/2026-07-16_150935/flat_topleft_zoom4x.png`, `flat_right_zoom4x.png`)
show smooth gradients with crisp anti-aliased tick-mark edges — **no
stripe/moiré/interlacing pattern at any pitch**, unlike the ambiguous
compressed remote-view screenshot from session 1 that this session supersedes.

- [ ] Fine stripes / moiré / interlaced garbage → host-side interleave
- [x] **Clean, normal 2D image → device-side interleaving** — confirmed from a
      genuine raw local framebuffer read, not a lossy remote-view screenshot
- [ ] Grid of ~5 near-identical tiles → device consumes a quilt

Per `CLAUDE.md`'s classification table, device-side interleaving means the 3D
path (Phase 3, if ever pursued) is **Trivial** — draw normally, firmware
handles depth. This session's evidence is materially stronger than session 1's
(raw local capture vs. compressed Screen-Sharing screenshot), though still
software-only: per `GUARDRAILS.md` #6, only the owner's eye on the physical
device can confirm the 3D effect actually resolves as depth — that remains
unverified and out of scope for Phase 1.

**Other findings, unchanged from prior sessions:** EDID still structurally
unavailable via `ioreg` on this hardware/OS (both targeted queries return
empty). USB Billboard devices unchanged (LONTIUM `2f61:8846`, generic
BillBoard `291a:8355`) — standard USB-C DP/HDMI Alt-Mode signaling, not
Looking-Glass-specific calibration hardware, consistent with the owner's prior
confirmation that this isn't a live question. App-bundle string recon found no
quilt/lenticular/calibration constants this run either.

**Still open:** Q2 (the visible circular region — center, radius, overscan)
has not been investigated at all — needs a rendered test pattern and an owner
photograph of the physical device, per `CLAUDE.md`. That work is scoped as
part of Phase 2 planning (already drafted, on hold pending owner go-ahead) —
not resumed here.

## 10. Classification (revised)

- [ ] Trivial — draw normally, firmware handles depth
- [ ] Easy — render an N-view quilt, no shader needed
- [ ] Medium — reimplement the interleave shader, derive calibration empirically
- [x] **Resolved (software-only): device-side / clean 2D**, classifying the 3D
      path as **Trivial** per `CLAUDE.md`'s table — based on a genuine local
      framebuffer capture, not a guess. Marked separately from the checkboxes
      above because `GUARDRAILS.md` #6 means no agent can confirm this
      resolves as actual depth on the physical device; that's the owner's call
      alone, and it's irrelevant to Phase 2 (2D-only) regardless.

## 11. External Deep Research results (2026-07-16) — supports Q1, opens new leads

Full source: `findings/deep-research-results.md` (run against
`findings/deep-research-prompt.md`). This is external web research, not a new
local probe — it corroborates rather than replaces the session 3 framebuffer
finding above.

**Named optics/manufacturing partners found (medium-high confidence, both
self-published but mutually corroborating):**
- **3D Global GmbH** (Aalen, Germany) — named "official 3D technology
  partner of Napster View" in a Jan 2026 press release. Lineage: acquired
  IP/patents/production from Secco GmbH (Erzgebirge, Germany) in Dec 2017.
  Core business is lenticular optical filter components + FPGA-based 3D
  processing.
- **faytech Tech Co., Ltd** (Shenzhen, China) — named manufacturing partner;
  makes industrial/touch displays, patented "ClearBond" optical bonding. Also
  builds the related "Napster Station" product which embeds a 2.1" View
  module, confirming the View is treated as a reusable component.

**Architecture inference (medium confidence — general partner architecture,
not a Napster-specific teardown):** 3D Global's documented signal chain has a
**device-side FPGA "Processing Unit" integrated into the monitor housing**,
feeding a "stereo with head-tracking, for one person" display stage. This
supports **architecture (a) device-side interleaving** — same conclusion as
the session 3 local framebuffer capture, from an independent source. Genuine
open tension: this eye-tracked 2-view model conflicts with Napster's own
"5-lens light-field engine" marketing language (fixed multiview, no tracking
needed); no source resolves which one actually ships. The View has no
onboard camera, so if eye-tracking is real, tracking would have to come from
the Mac's webcam.

**USB VID/PID clarified (high confidence):**
- `0x2f61` (LONTIUM) = real fabless bridge-chip vendor, Hefei/Shenzhen. Their
  entire LT-series product line is conventional DP/HDMI/Type-C↔MIPI/LVDS
  format conversion — **no LONTIUM part does lenticular interleaving**. Some
  parts advertise "3D" support but it's dual-panel stereo frame-packing, not
  sub-pixel lenticular mapping. **This rules out the Lontium chip as the
  interleaving processor** — it must be a separate chip (the FPGA hypothesis
  above) or host-side.
- `0x291a` = Anker Innovations (VID confirmed via registry; PID `0x8355`
  itself unresolved to a specific product).
- USB Billboard Device class confirms this is a standard USB-C DisplayPort
  Alt-Mode link (per USB-IF spec, Billboard is just the Alt-Mode
  negotiation-status fallback) — no custom/proprietary transport, consistent
  with macOS seeing it as an ordinary second display.

**Confirmed negative space (nothing found, won't be found by more web
research):** no teardown, no FCC ID (device is radio-less/bus-powered, so
SDoC-exempt from the FCC database — the usual "FCC internal photos" shortcut
is unavailable), no hardware SDK, no panel part number/native
resolution/DPI, no packet captures, no modding/reverse-engineering community
presence (device too new/niche). Q2 (circular active-area geometry) remains
entirely unanswered by external research, as expected — it was explicitly
scoped as lower-priority/device-specific-only in the research prompt.

**New productive lead surfaced by this research, not previously considered:**
inspecting the Napster-for-Mac app bundle's Metal shader library
(`.metallib`) for a lenticular/interleave/quilt shader. Its **absence** would
be strong positive evidence for device-side interleaving (architecture a);
its **presence** would flip the read toward host-side (architecture c). This
is static binary analysis, does not require any device probing, and doesn't
touch the USB/protocol guardrail (#4) since it's inspecting the app we
already run, not the device. Not yet attempted.

**Effect on classification:** No change to the Trivial/clean-2D
classification in §10 above — this corroborates it from an independent
source rather than overturning it. Confidence in "device-side" moved from
"tentative, software-only" to "medium, cross-corroborated by an independent
external source," but "an FPGA is physically inside this specific retail
unit" remains an unconfirmed inference either way.
