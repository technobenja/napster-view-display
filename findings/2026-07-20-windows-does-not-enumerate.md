# The View is not recognised as a display by Windows

**Tested 2026-07-20 by the owner.** Plugged the View into a Windows
machine over USB-C. **It does not enumerate as a display at all** — not as
a wrong-resolution monitor, not as an unknown device to be driven with a
generic driver. Windows simply does not see a screen.

## Why this matters: it corrects an inference in the Deep Research

`deep-research-results.md` established, correctly, that:

- the USB VID `0x2f61` is **LONTIUM Semiconductor**, a conventional
  DP/HDMI/Type-C↔MIPI/LVDS bridge-chip vendor — not a lenticular or
  multiview processor, and
- the device advertises the **USB Billboard** class, which is what a
  standard USB-C DisplayPort Alt-Mode link uses.

From that, the reasonable inference was that the panel is an ordinary
DisplayPort monitor behind a standard bridge, and that the vendor's
"M1+ Mac only" claim describes *their application's* requirements rather
than the hardware's. On that reading it should have enumerated as a
generic display on any OS.

**That inference is now falsified for Windows.** The Billboard descriptor
and a commodity bridge chip were necessary but not sufficient: something
else in the path — Alt-Mode negotiation, the DP topology the bridge
presents, or a mode the panel only offers after a host-side handshake the
Mac driver performs — stops Windows from ever seeing a display.

This does **not** tell us the panel is exotic. It tells us that "standard
bridge chip + Billboard class" is not enough on its own to predict
cross-platform behaviour, and that any claim of the form "it should just
work as a monitor elsewhere" needs testing rather than reasoning.

## Practical consequence for anyone reading this

If you own a View and were hoping to drive it from Windows: as of this
test, you cannot, and the obstacle is below the level anything in this
repository operates at. This project is macOS-only for a hardware reason,
not merely because it was written that way.

**Linux is untested.** It may behave like macOS, like Windows, or
differently again — the test is cheap and the result would be worth
recording here. Note that even a successful enumeration would leave a
second, unrelated obstacle: under Wayland an application generally cannot
place its own window on a chosen output, which is the entire mechanism
this software depends on. X11 would be fine.
