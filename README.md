# ImageView — pictures on a Napster View

[![tests](../../actions/workflows/tests.yml/badge.svg)](../../actions/workflows/tests.yml)

Shows a rotating, circular-masked slideshow on a **Napster View** — a 2.1-inch
round lenticular display — instead of the app it ships with. A small menu bar
app controls it; a background agent does the drawing.

Images suit this hardware far better than text. Lenticular displays trade
resolution for depth, because the panel is shared across viewing angles, so a
2.1-inch round panel is a poor home for stats and a good home for pictures.

---

## Unofficial and unaffiliated

**This project is not affiliated with, endorsed by, or connected to Napster,
its parent companies, or any of its hardware or optics partners.** "Napster"
and "Napster View" are the trademarks of their respective owners, and are used
here only to identify the hardware this software drives.

Everything here was worked out from the outside, on one retail unit, with no
documentation, no SDK, and no vendor contact. **No reverse-engineered binary
content is redistributed in this repository** — the research notes describe
what was concluded, and deliberately do not reproduce extracted strings or
symbols from any third-party application.

## Maintenance posture

**This is a snapshot of what worked on one device. Issues may go unanswered.**

It is published because the research underneath it is hard to redo and might
save someone else a lot of time, not because it is a supported product. There
is no roadmap, no release schedule, and no promise that a future macOS or a
future firmware will not break it. Fork it freely.

## Requirements

- A Mac with Apple silicon (M1 or newer)
- A Napster View, attached and recognised by macOS as a second display

No Homebrew and no separate Python install are needed — the `.app` bundles its
own interpreter.

## Install

Download `ImageView.dmg` from the releases page, open it, and drag
**ImageView** to Applications. Launch it once and the first-run flow will ask
where pictures should come from: a folder on this Mac, or an HTTP source.

**Verify the download before opening it.** This build has no Developer ID
signature, so the checksum is the only tamper-evidence available:

```bash
shasum -a 256 ~/Downloads/ImageView.dmg
```

```
SHA-256 (ImageView.dmg) = 20f05ae906e8d448a5d15651746524960d672ea3027396ae547d73e2e3fb582a
```

If that does not match, do not open it.

Because the build is ad-hoc signed rather than notarized, macOS blocks the
first launch. Getting past that changed in **macOS 15 (Sequoia)**: the old
Control-click → **Open** shortcut no longer overrides Gatekeeper.

**macOS 15 and later**

1. Try to open **ImageView** from Applications once, and dismiss the warning.
   The next step does not appear until you have done this.
2. Open **System Settings → Privacy & Security** and scroll to **Security**.
3. Beside the note about ImageView, click **Open Anyway** and authenticate.
4. Open the app again.

**macOS 14 and earlier**

Control-click the app, choose **Open**, then confirm.

This is what an ad-hoc signature costs: macOS can tell the binary has not been
tampered with since it was signed, but not *who* signed it. Notarizing would
remove these steps, and is deliberately not done — see **Maintenance posture**
above. The checksum is the tamper-evidence.

## Using it

The menu bar item shows the current state and offers Next, Previous, Pause and
Blank, plus windows for calibration and settings.

**If the menu bar icon is missing, relaunch ImageView from Applications —
pictures keep rotating even when the controls aren't open.** The display agent
and the menu bar are separate processes on purpose, so losing the controls
never stops the display.

### Calibration

The visible area of the View is a circle inside a rectangular framebuffer, and
neither the centre nor the radius is discoverable in software. The calibration
window draws two rings and lets you nudge centre and radius until they line up
with the physical bezel. The result is stored in `~/.viewlab/calibration.json`.

The shipped defaults were measured on one unit; yours may differ slightly.

### When something is wrong, the View says so

A wall display has no console and nobody standing in front of it, so a blank
circle and a broken one look identical. Rather than holding the last good
picture indefinitely, the View shows a readable message and distinguishes the
cases that have different fixes:

| On screen | What it means |
|---|---|
| **Can't reach the server** | the machine serving pictures is off, or this Mac is off the network |
| **Not authorised** | the server rejected the access token below |
| **No pictures** (token in use) | the server answered but sent nothing — either the token was rejected or there is genuinely nothing to show, and the server cannot tell you which |
| **No pictures yet** | reachable, nothing matched — add some pictures |

A transient network blip does not take the screen: a fault has to persist for
two polls first, unless there is nothing being displayed anyway. A rejected
credential is shown immediately, because it will not fix itself.

### HTTP sources that need a token

An HTTP image source may require an `X-OpenLab-Read` header on its listing
call. If one is present in your login keychain, ImageView sends it:

```bash
security add-generic-password -a openlab-read -s viewlab-openlab-read -U -w
```

It is sent **only on the listing request**, never when fetching image data,
and **only to a private (LAN or loopback) address** — never to a public host.
With no such keychain item, requests are anonymous and nothing changes.

> `security ... -w` prompts for the value twice. Feeding it a single line
> stores an empty secret and still exits 0, so read it back to confirm.

## The research

The most useful part of this repository for anyone else working on this
hardware is probably not the app:

| File | What it establishes |
|---|---|
| `probe/DECISION.md` | The interleave-locus determination — where the lenticular interleaving happens, settled from a real framebuffer capture rather than guessed |
| `findings/deep-research-results.md` | The supply chain behind the device, and what the partners' documented architecture implies |
| `findings/usb-enumeration-redacted.md` | What the device presents on the USB bus, and why no per-unit calibration can be read out of it |
| `findings/2026-07-20-windows-does-not-enumerate.md` | The same device does not enumerate as a display under Windows |
| `findings/2026-07-16-report.md` | The session log behind the above |

Short version: macOS treats the View as an ordinary second display over
DisplayPort Alt Mode, so there is no protocol to defeat for 2D output, and the
device does its own interleaving. You can just draw to it.

## Building from source

```bash
packaging/make_release.sh
```

This builds `ImageView.app` and `ImageView.dmg` from a clean, tracked-files-only
checkout at a neutral path. It refuses to produce a `.dmg` if any of its six
gates fail — including identity sweeps over both the finished bundle *and* the
`.dmg` itself, which read inside binary `.pyc` files, compressed zip members
and the mounted disk image, where an ordinary `grep -r` sees nothing.

**Two of those gates need maintainer-only tooling that is deliberately not
published.** The sweeps are driven by a `release_gate.py` that holds the
catalogue of strings which must never appear in a public build — publishing
that catalogue would defeat its own purpose. Building from this repository
therefore stops at the first sweep. The `.app` and `.dmg` steps themselves are
ordinary py2app and `hdiutil` and are readable in the script.

## Running the tests

One suite, discovered from the repository root:

```bash
python3 -m unittest discover -t . -s . -p "test_*.py"
```

`display/` and `ui/` are real packages, so discovery must run from the root —
running it from inside `display/` puts that directory on `sys.path` instead of
the root and every `from display import ...` fails.

There are no third-party test dependencies.

The badge at the top is that same suite, run on a clean checkout by GitHub
Actions on macOS. It is a narrow claim: it does not build the app, does not
check the release artifact, and cannot see the display. A green badge means
the unit tests passed — nothing about whether a build is fit to ship.

## Layout

| Path | What |
|---|---|
| `display/` | The display agent — drawing, rotation, sources, cache |
| `ui/` | The menu bar app, settings, calibration, first-run flow |
| `packaging/` | py2app build, icons, `make_release.sh` |
| `probe/` | Read-only diagnostic script and the decision record |
| `findings/` | Research notes |

## License

MIT — see [LICENSE](LICENSE).
