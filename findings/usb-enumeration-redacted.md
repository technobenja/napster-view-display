# USB enumeration — what the View presents on the bus (redacted)

Replaces the four raw `system_profiler SPUSBDataType` dumps that this
repository used to carry. The raw dumps were byte-identical to each other and
contained device serial numbers, a volume UUID, and unrelated personal
peripherals. **The VID/PID analysis is the part with research value, and it
survives redaction intact** — nothing below is inferred, all of it was read off
the bus on a Mac mini (Apple silicon) with the View attached.

Serial numbers, volume UUIDs, mount points, and unrelated peripherals are
omitted deliberately. Devices that are not part of the View's connection path
are listed only where their *absence* from the analysis would be misleading.

## Devices in the View's connection path

| Device | VID | PID | Manufacturer string | Class / role | Speed |
|---|---|---|---|---|---|
| Billboard Device | `0x2f61` | `0x8846` | LONTIUM | USB Billboard | 12 Mb/s |
| USB BillBoard | `0x291a` | `0x8355` | BillBoard | USB Billboard | 12 Mb/s |

Both are **USB Billboard class** devices. That class exists for exactly one
purpose: a USB-C device uses it to describe an **Alternate Mode** it supports
(here, DisplayPort Alt Mode) so the host can report the link to the user. A
Billboard descriptor is a *declaration about the link*, not a control channel.

## What the enumeration establishes

1. **The video path is standard DisplayPort Alt Mode over USB-C.** This is
   consistent with macOS treating the View as an ordinary second display, and
   it is why no protocol reverse-engineering was ever required.

2. **`0x2f61` is LONTIUM Semiconductor** — a conventional display-bridge
   vendor. Every documented LONTIUM LT-series part is a DP/HDMI/Type-C to
   MIPI/LVDS converter. **No LONTIUM part performs lenticular or multiview
   interleaving.** So the bridge chip is not the thing doing the 3D work; the
   interleaving happens either further downstream on the device, or on the
   host. See `deep-research-results.md` for the sourcing on this.

3. **`0x291a` resolves to Anker Innovations** in the public USB-ID registries.
   Treat this as a commodity Billboard implementation, not a Napster-specific
   part.

4. **There is no vendor-specific control interface.** This is the most useful
   negative result on this page, and it is the reason it is worth publishing.
   The View exposes **no HID, no vendor-class interface, and no USB endpoint
   that could serve per-unit lens calibration**. That is a direct structural
   contrast with the Looking Glass device family, which enumerates as a
   monitor *and* serves per-device calibration (pitch, slope, center, view
   cone) over USB. Any approach that assumes calibration can be read out of
   the View over the bus is dead on arrival — calibration here has to be
   determined empirically, which is what the test-pattern-and-photograph
   method in this repository does.

## Scope and caveats

- Read on macOS only. The companion finding
  `2026-07-20-windows-does-not-enumerate.md` records that the same device does
  **not** enumerate as a display under Windows, which means "Billboard class +
  bridge chip" is not on its own sufficient to predict cross-OS behaviour.
- EDID queries (`ioreg -lw0 -r -c IODisplayConnect` / `-c AppleCLCD2`) returned
  empty on this hardware/OS combination, so no EDID identity is recorded here.
- One Apple-silicon host, one View, one macOS version. Other units and other
  hosts are unverified.
