# Deep Research prompt — Napster View lenticular architecture

Drafted 2026-07-16 to supplement the Q1 (interleave locus) finding in
`probe/DECISION.md` with external research. Not yet run — paste into a
Deep Research-capable tool (e.g. claude.ai) and drop results/findings back
into this repo's `findings/` directory; fold anything load-bearing into
`probe/DECISION.md`.

---

```
I'm investigating a consumer device called the "Napster View" — a 2.1-inch
circular lenticular light-field display ("glasses-free 3D"), described in its
marketing as a "5-lens engine," USB-C connected, requiring an M1+ Mac. It
shipped around October 2025. There is no public SDK and I have not been able
to find a public teardown.

What I already know from hands-on testing:
- macOS enumerates it as a standard second display (rectangular framebuffer,
  960x960 @ 60Hz reported), even though the physical panel is round.
- It also enumerates as a USB "Billboard" device (standard USB-C DisplayPort/
  HDMI Alt-Mode signaling) — two VID/PIDs seen: LONTIUM 0x2f61:0x8846, and a
  generic Billboard 0x291a:0x8355.
- EDID queries via macOS ioreg (both a generic query and targeted
  IODisplayConnect/AppleCLCD2 queries) return empty — no manufacturer/product
  ID or native-resolution data recoverable that way on this hardware/OS
  combination.
- A genuine raw local framebuffer capture (not a compressed remote-view
  screenshot) of the stock app's UI, rendering a circular avatar, shows ONE
  coherent 2D image — no repeated/tiled sub-images, no stripe or moiré
  pattern at 4x pixel zoom on flat gradient regions. This is fairly strong
  evidence the lenticular interleaving happens in device firmware, not in the
  host app, but it's based on a single UI's content and I have not inspected
  the app binary for confirming evidence.

What I want you to research:

1. Does any public information exist about the Napster View's actual display
   architecture — teardowns, FCC filings, community reverse-engineering
   threads, developer forum posts, patents, or press/review coverage that
   describes how it's driven? I'm trying to distinguish between three
   possibilities: (a) device-side interleaving, where the host just sends a
   normal 2D image and firmware in the display handles the lenticular
   conversion; (b) a Looking-Glass-style quilt, where the host must render N
   viewpoint images tiled into one frame and the device slices them; (c)
   host-side interleaving, where a proprietary app-side shader pre-interleaves
   pixels before they hit the wire.

2. What can you find out about the two USB identifiers — LONTIUM (VID
   0x2f61) PID 0x8846, and generic USB "BillBoard Device" VID 0x291a PID
   0x8355? Are these associated with any other known display products,
   reference designs, or lenticular/light-field hardware vendors? Does the
   LONTIUM PID in particular show up anywhere else (LONTIUM is a real
   Shenzhen-based display-bridge chip vendor) — e.g. any LT-series chip known
   to do on-chip lenticular interleaving or multi-view processing?

3. Is there any public documentation, marketing material, patent, or teardown
   for how Napster (the company/brand relaunch behind this device) sourced or
   licensed the display panel/optics? Any connection to existing lenticular
   display makers (Looking Glass Factory, Leia Inc/Lume Pad, Sony Spatial
   Reality Display, etc.) in supply chain, licensing, or personnel?

4. Separately from the interleaving question: is there any public information
   about the visible circular active area on this specific panel — center
   offset, radius, or overscan relative to the reported rectangular
   framebuffer? (Lower priority than #1-3 — I have a separate empirical plan
   to determine this myself with a test pattern and photograph, so only
   surface this if you find something concrete and specific to this device,
   not general lenticular-display theory.)

Please cite sources and flag confidence level — I'm aware this device is niche
enough that solid public information may simply not exist, and I'd rather
hear "I found nothing beyond marketing copy" than a speculative answer
presented as fact.
```
