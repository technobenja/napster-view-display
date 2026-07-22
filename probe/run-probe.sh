#!/usr/bin/env bash
# view-lab probe — READ ONLY. No sudo, no installs, no writes outside repo.
# Characterizes the Napster View. Path 1 (standard display) is already confirmed;
# this fills in the details we cannot get by hand.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
OUT="$REPO/findings/$STAMP"
mkdir -p "$OUT/photos"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
cap() { echo "  -> $2"; eval "$1" > "$OUT/$2" 2>&1 || echo "     (command failed; see file)"; }

echo "view-lab probe  |  $STAMP"
echo "output: $OUT"

# ---------------------------------------------------------------- displays
say "Displays"
cap "system_profiler SPDisplaysDataType -json" "displays.json"
cap "system_profiler SPDisplaysDataType"       "displays.txt"

# EDID: manufacturer, product id, native timing, physical panel size.
# Often the fastest way to learn the panel's true native resolution.
say "EDID / display connect"
cap "ioreg -lw0 -r -c IODisplayConnect"        "ioreg-displayconnect.txt"
cap "ioreg -lw0 -r -c AppleCLCD2"              "ioreg-clcd.txt"

# ---------------------------------------------------------------- usb
# Q: is there a USB sidechannel alongside the display? If yes, this is the
# Looking Glass architecture (panel over video, calibration over USB) and their
# math applies directly.
say "USB"
cap "system_profiler SPUSBDataType -json"      "usb.json"
cap "system_profiler SPUSBDataType"            "usb.txt"
cap "ioreg -p IOUSB -l -w 0"                   "ioreg-usb.txt"

# ---------------------------------------------------------------- app recon
# Legitimate interop recon on software we have licensed and installed.
# Looking for: quilt geometry, calibration constants, VID/PID, a bundled driver.
say "Napster app bundle"
APP="$(ls -d /Applications/Napster*.app 2>/dev/null | head -n1)"
if [ -n "${APP:-}" ]; then
  echo "  found: $APP"
  echo "$APP" > "$OUT/app-path.txt"
  cap "ls -R '$APP'"                                  "app-tree.txt"
  cap "plutil -p '$APP/Contents/Info.plist'"          "app-info-plist.txt"
  cap "codesign -d --entitlements :- '$APP' "         "app-entitlements.txt"
  BIN="$APP/Contents/MacOS/$(basename "$APP" .app)"
  if [ -f "$BIN" ]; then
    cap "otool -L '$BIN'"                             "app-linked-libs.txt"
    # Filter strings for anything that smells like optics or transport.
    cap "strings -a '$BIN' | grep -iE 'quilt|lenticul|calibrat|pitch|slope|viewcone|subpixel|tilt|hologra|vid|pid|usb|iokit|hidapi|metal|shader' | sort -u | head -400" "app-strings-filtered.txt"
  fi
  # Shaders and calibration blobs frequently ship as loose resources.
  cap "find '$APP' -type f \\( -iname '*.metal' -o -iname '*.json' -o -iname '*.plist' -o -iname '*.txt' -o -iname '*.cfg' \\) -size -2M | head -200" "app-resource-candidates.txt"
else
  echo "  Napster app NOT found in /Applications" | tee "$OUT/app-path.txt"
fi

# System extensions / drivers — if one exists, it names the transport instantly.
say "Drivers / system extensions"
cap "systemextensionsctl list"                 "system-extensions.txt"
cap "kmutil showloaded --list-only"            "kexts.txt"

# ---------------------------------------------------------------- framebuffer
# THE decisive test. Capture every display; identify the View by its small,
# near-square dimensions. Then LOOK at the pixels:
#   stripes/moire   -> host-side interleave (app runs the shader)
#   clean 2D image  -> device-side interleave (firmware does it)
#   grid of ~5 tiles-> device consumes a quilt directly
say "Framebuffer capture (interleave locus test)"
echo "  NOTE: needs Terminal to have Screen Recording permission."
echo "        System Settings > Privacy & Security > Screen Recording"
echo "  NOTE: for a meaningful result, the Napster app should be RUNNING"
echo "        with an avatar visible on the View right now."
for i in 1 2 3 4 5; do
  f="$OUT/screen-$i.png"
  if screencapture -x -D "$i" "$f" 2>/dev/null && [ -s "$f" ]; then
    dim="$(sips -g pixelWidth -g pixelHeight "$f" 2>/dev/null \
           | awk '/pixel/ {printf "%s ", $2}')"
    echo "  display $i -> $(basename "$f")  [$dim]"
  else
    rm -f "$f"
  fi
done
cap "sips -g pixelWidth -g pixelHeight '$OUT'/screen-*.png" "capture-dimensions.txt"

# ---------------------------------------------------------------- summary
say "Done"
echo "Findings: $OUT"
echo
echo "Next: fill in probe/DECISION.md, then STOP and report."
echo "Do not begin Phase 2 without the owner's go."
