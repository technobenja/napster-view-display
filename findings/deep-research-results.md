# Deep Research results — Napster View reverse-engineering

Run 2026-07-16 in response to `findings/deep-research-prompt.md`. Full verbatim
output below. Key findings folded into `probe/DECISION.md` §11 and
`CLAUDE.md`'s "Prior art" section — read those first for the load-bearing
takeaways; this file is the full source material.

---

# Reverse-Engineering the Napster View: What Public Sources Actually Reveal

## TL;DR
- **The single most important finding:** The Napster View's glasses-free 3D is supplied by **3D Global GmbH (Aalen, Germany)** — a lenticular-optics firm built on IP acquired from Secco GmbH — and the unit was co-manufactured by **faytech Tech Co., Ltd (Shenzhen, China)**. 3D Global's documented architecture does view-processing on a **device-side FPGA integrated into the display housing**, which supports **architecture (a): device-side interleaving** over Looking-Glass-style quilts or host-side pixel interleaving. Confidence: medium-high on the partners (both self-published, mutually corroborating); medium on device-side interleaving (inference from the partner's general architecture, not a Napster-specific teardown).
- **The LONTIUM chip (VID 0x2f61) is a conventional display bridge, not a multiview processor.** Every documented LONTIUM LT-series part is a DP/HDMI/Type-C ↔ MIPI/LVDS converter; none perform lenticular interleaving. So the 3D interleaving must happen either in a separate processor (an FPGA) or on the host — not in the Lontium bridge. Confidence: high.
- **No teardown, no FCC ID, no hardware SDK, and no packet captures exist publicly.** This is confirmed negative space: the deepest hardware questions (panel part number, FPGA presence, active-circle geometry, definitive proof of device- vs. host-side interleaving) can only be answered by your own hardware investigation.

---

## Question 1 — Display architecture

**Verdict: The best-supported model is device-side interleaving driven by a 3D Global FPGA, with the Mac sending an ordinary 2D/stereo image over standard DisplayPort. Confidence: MEDIUM (inference from the optics partner's documented architecture; no Napster-specific teardown confirms it).**

### Documented facts (with sources)
- **3D Global GmbH is the named "3D technology partner."** In a press release dated 8 January 2026, 3D Global states: "3D Global GmbH, a leading provider of eyeglass-free 3D display technology, is the official 3D technology partner of Napster View… Through the close cooperation of Napster, faytech and 3D Global, Napster View was successfully launched." The release is accompanied by a photo captioned "Selcuk Özer, Group Chief Business Development Officer at 3D Global and Edo Segal, CTO at Napster." Özer is quoted: "Projects like Napster View show exactly what 3D Global stands for: We specialize in future topics and develop eyeglass-free 3D solutions for completely new interfaces – regardless of size, form factor or application area." (Source: 3d-global.com)
- **3D Global's signal-chain architecture** (self-published on 3d-global.com/en/3d-technology/) has a **"Processing Unit"** described as an **"FPGA platform for highest performance – Short latency time – Entire signal chain coordinated – Optimum system integration in monitor housing,"** feeding a display stage described as **"Stereo with head tracking – for one person."** Input options are listed as a 3D stereo camera, a 2D camera with AI 2D→3D conversion, or "3D-Signal input – HDMI side by side / Customized interface."
- **3D Global's lineage:** Per 3d-global.eu, the company "entered the market in December 2017" after "the acquisition of employees, IP, patents and production facilities from Secco GmbH based in the German Erzgebirge." Its "key business is the development and production of optical filter components" (lenticular/optical filters, 0.1 mm film or 0.1–3 mm glass).
- **The "SingleView" / eye-tracked 2-view family** that this lineage represents (see the closely related ZVIEW product from United Screens GmbH) uses lenticular optics but, "due to the integrated eye-tracking… only 2 instead of 5–9 perspective views… are required," with the "left and right partial image… permanently and latency-free aligned on the display via the position detection of the viewer's eyes." (Source: united-screens.tv)
- **Your own empirical result is consistent with device-side interleaving:** a raw framebuffer capture showed ONE coherent 2D image, no tiled sub-images and no stripe/moiré pattern. This matches a pipeline where the host sends a normal image and the display's own processor does the lenticular mapping.

### Important tension / unresolved point
There is a genuine conflict in the marketing language that no public source resolves:
- Napster's own materials describe a **"5-lens light-field engine"** and **"lenticular lens technology to create stereoscopic 3D"** — wording that suggests a **fixed multi-view lenticular** (à la Looking Glass), which needs no eye-tracking.
- 3D Global's core competency is **SingleView 2-view eye-tracked** autostereoscopy, which *requires* an eye-tracking camera and device-side interleaving.
- **The Napster View has no onboard camera** (see Additional angles). If it uses eye-tracking, the eye position would have to come from the **Mac's webcam** (host-side tracking fed to the display). If it uses a fixed "5-lens" multiview lenticular, no tracking is needed at all.

Both readings still put the **pixel interleaving on the device side** (either an FPGA using host-provided eye coordinates, or a fixed lenticular mapping) rather than requiring the host to pre-interleave pixels — which is why the framebuffer looks like a coherent 2D image. But whether an FPGA is physically present in the retail unit is **not confirmed by any teardown**. Confidence on "device-side interleaving": medium. Confidence on "an FPGA is inside": low (inference only).

### The three candidate architectures, assessed
- **(a) Device-side interleaving** — host sends a normal image, display firmware/FPGA does lenticular conversion: **Best supported.** Consistent with (i) 3D Global's documented FPGA-in-housing architecture, (ii) the "HDMI side by side / customized interface" input model, and (iii) your single-coherent-image framebuffer capture.
- **(b) Looking-Glass-style quilt** — host tiles N viewpoints, device slices: **Not supported.** No evidence; the framebuffer capture showed no tiling. The "5-lens" phrase could loosely imply ~5 views, but there is no quilt in the observed framebuffer.
- **(c) Host-side pixel interleaving** — proprietary shader pre-interleaves before the wire: **Weakly supported / partly contradicted.** The framebuffer capture argues against a fully pre-interleaved wire image. Note that Napster's CTO says the avatar *video* is generated on-device by the Mac's GPU ("a neural network that's running on your Mac that's creating that video," per Macworld) — but that is about avatar generation, not 3D interleaving; do not conflate the two.

### Subscription / app / developer access
- **Requires the Napster for Mac app and (after a one-month trial) a subscription.** "Napster View is available for $99 (includes one free month of Napster 26 platform access). Subscription plans available after the first month. Requires Mac app download." (Sources: GlobeNewswire; Tom's Guide; napster.com newsroom)
- **Mac-only, M1+ required.** Napster's CTO cites Apple Silicon GPU/CPU strength; a PC version is stated as "coming later." (Source: GamesBeat)
- **No View-specific hardware SDK, developer program, or third-party app support has been announced.** Napster operates a developer program, but it is the **Omniagent / Companion AI-agent API** (WebRTC/WebSocket video-agent platform) and the legacy **music-streaming API** — neither exposes the View display hardware. Confidence: high that no hardware SDK exists publicly.

---

## Question 2 — USB identifiers

**Verdict: VID 0x2f61 = LONTIUM Semiconductor (a conventional display-bridge vendor, Hefei/Shenzhen, China); VID 0x291a = Anker Innovations; the "Billboard" enumeration confirms this is a standard USB-C DisplayPort Alt-Mode link. No LONTIUM part does lenticular/multiview interleaving. Confidence: HIGH.**

### VID 0x2f61 — LONTIUM Semiconductor
- **Registry confirmation:** the-sz.com's USB-ID database lists "0x2F61 · Lontium Semiconductor Corporation."
- **Company identity/location:** Lontium is "a fabless design house established in 2006 with design centers, sales & support offices in Hefei, Shenzhen and Hongkong China" (lontiumsemi.com); HQ in Hefei, Anhui. It listed on the Shanghai Stock Exchange STAR Market in 2023. This confirms your expectation (Shenzhen/Hefei-based display-bridge vendor).
- **Product line is strictly conventional bridges/converters.** Lontium's catalog: "HDMI V1.3/1.4/2.0/2.1 chipset, Displayport 1.2/1.4 chipset, USB 2.0, USB3.1 Type C chipset, MIPI chipset, LVDS chipset, VGA and LCD/TV controller." Representative parts:
  - **LT9611 / LT9611UXC** — MIPI DSI/CSI → HDMI bridge (Linux kernel driver: `lontium,lt9611`).
  - **LT6911 / LT6911C** — HDMI/DP++ → MIPI DSI/CSI/LVDS.
  - **LT7911 / LT7911UXC** — Type-C/DP1.4 → MIPI CSI/DSI.
  - **LT7211 / LT8912 / LT9211** — various DP/Type-C/LVDS/MIPI conversions.
- **On "3D" support in LONTIUM parts:** Some parts (e.g., LT6911C, LT7211, LT7911) list "3D" support — but the datasheets make clear this is **frame-packed / side-by-side stereo routing**, NOT lenticular interleaving. LT6911C's datasheet: "for 3D video format, left side data can be sent to one panel, and right side data can be sent to another panel." This is dual-panel stereo splitting, not sub-pixel lenticular mapping. **No LONTIUM LT-series part is documented as performing on-chip lenticular interleaving, multi-view processing, or light-field/quilt-slicing.** Confidence: high.
- **PID 0x8846:** Not found in any public database tied to a named product; no other display product, dev board, or reference design surfaced for this PID. Confidence on "nothing found": high.

**Interpretation:** Because the LONTIUM bridge is purely a format converter (Type-C/DP → MIPI/LVDS to drive the raw LCD panel), the lenticular interleaving **cannot** happen inside it. It must happen either (i) upstream in a separate processor inside the device (the 3D Global FPGA hypothesis), or (ii) on the host. Combined with your coherent-2D-image framebuffer capture, the separate-processor/device-side model is the most consistent.

### VID 0x291a — "generic Billboard device"
- **VID 0x291a = Anker Innovations Limited** (the-sz.com USB-ID database, citing linux-usb.org). PID 0x8355 did not resolve to a named product. This is plausibly an off-the-shelf USB-C bridge/PD-controller silicon or module whose vendor block is registered to Anker, reused in the View's USB-C front end. Confidence: medium (VID attribution is solid; the specific product mapping is "nothing found").

### What a USB "Billboard" device is (and what it implies)
- A **USB Billboard Device (USB-IF class code 0x11)** is a standard mechanism by which a USB-C accessory advertises its **Alternate Mode** capabilities and negotiation status to the host. Per Infineon's documentation: "According to USB Type-C specification, if a device fails to successfully enter an Alternate Mode within tAMETimeout (maximum of 1000 ms), then the device will minimally expose a USB 2.0 interface (USB Billboard Device Class)… to indicate the failure of Alternate Mode entry." It requires no special driver.
- **Implication for the link:** The View's presence as a Billboard device confirms it is a **standard USB-C DisplayPort Alt-Mode sink** — i.e., the Mac drives it over native DisplayPort signaling carried on the USB-C connector, exactly as it would an external monitor. This is fully consistent with macOS enumerating it as "a standard second display (rectangular framebuffer)." The 3D is therefore an **optical + processing layer on top of an ordinary DP video stream**, not a custom bulk-transfer protocol. Confidence: high.
- **On the empty EDID:** an empty EDID over a DP Alt-Mode link is unusual but not unheard-of in cheap/custom bridge implementations; it does not itself indicate anything exotic about the 3D pipeline. (Flagged as inference; no device-specific source.)

---

## Question 3 — Supply chain / optics sourcing

**Verdict: Optics/3D technology from 3D Global GmbH (Aalen, Germany), lineage traced to Secco GmbH; hardware manufacturing by faytech (Shenzhen, China); no evidence of any tie to Looking Glass, Leia, Sony, Dimenco, Alioscopy, Proto, or Hypervsn. Confidence: HIGH on the named partners; HIGH on the absence of ties to the other named makers.**

### Documented supply-chain facts
- **3D Global GmbH — optics/3D technology partner** (Aalen, Baden-Württemberg, Germany). Self-identified as "the official 3D technology partner of Napster View." Core business: lenticular optical filter components, custom bonding, and FPGA-based 3D processing. Panel sizes "from 2.5 to 86 inches." (3d-global.com)
- **Secco GmbH — IP origin.** 3D Global acquired "employees, IP, patents and production facilities from Secco GmbH" (Erzgebirge, Germany) and entered market December 2017. (3d-global.eu)
- **faytech — manufacturing partner.** Named in 3D Global's release ("the teams at Napster and faytech"). faytech Tech Co., Ltd is headquartered in **Shenzhen, China** ("SHENZHEN, CHINA – faytech Tech Co., Ltd, a leading innovator in interactive display solutions," per faytech.com) and is "a leading global manufacturer of industrial PCs, touch PCs, and touch monitors" with plants in Huizhou and Suining, China, plus New Delhi; it specializes in optical bonding (patented "ClearBond" adhesive). (faytech.com)
- **Related product line — Napster Station:** A separate faytech collaboration (with AUO) produced "Napster Station," a **30-inch AUO Transparent Micro-LED touch display, 960×540 resolution (~0.69 mm pitch), 600 cd/m², >60% transparency, 1,000,000:1 contrast**, that embeds "2.1" Napster View" plus a "Napster VoiceField Microphone Array" and "48MP camera." AUO CTO Wei-Lung Liau: "Our Transparent Micro-LED technology is a testament to that commitment, and its application in Napster Station demonstrates its immense potential." This confirms faytech is Napster's display-hardware integrator and that the 2.1" View module is treated as a reusable component. (faytech.shop press release)

### Corporate history (documented)
- Napster's current owner is **Napster Corporation, formerly Infinite Reality**, which **acquired the Napster brand for $207 million in March 2025** and then rebranded from Infinite Reality to Napster. It separately **acquired Touchcast (Edo Segal's agentic-AI company) for $500 million in cash and stock, announced April 16, 2025, in a deal that valued Infinite Reality at $15.5 billion** ("The acquisition is Infinite Reality's largest yet, and the transaction agreement values the company at $15.5 billion," per GlobeNewswire). The Napster Companion/View software stack derives from Touchcast's "Mentorverse." CEO: **John Acunto.** CTO: **Edo Segal.** (Sources: Adweek, Rolling Stone, Fast Company, GlobeNewswire, GamesBeat)
- Napster View was first announced **25 June 2025** (as a "$199 later this summer" 2.1" device) and launched at **$99 on October 20, 2025** ("BOCA RATON, Fla., Oct. 20, 2025 (GLOBE NEWSWIRE)… Napster View is available for $99"); it was recognized by **USA TODAY and Reviewed as one of the top tech picks of CES 2026** (Reviewed's CES 2026 awards page lists it among "80 winners"). (GlobeNewswire; Tom's Guide; reviewed.com; napster.com CES recap)

### No connection found to other 3D-display makers
- **Nothing found** linking Napster/Infinite Reality/3D Global to **Looking Glass Factory, Leia Inc (Lume Pad / RED Hydrogen One), Sony Spatial Reality Display, Dimenco, Alioscopy, Proto Inc, or Hypervsn** via supply chain, licensing, acquisition, or personnel movement. The optics chain is entirely 3D Global/Secco (German lenticular lineage) + faytech (Chinese manufacturing). Confidence: high that no such ties are public.
- **No display/holographic-company acquisitions by Napster/Infinite Reality** surfaced beyond Touchcast (software) and the Napster brand itself; the View appears to be a **partnership/OEM arrangement, not an acquisition.** Confidence: medium-high.

---

## Question 4 — Circular active-area geometry, panel part number, resolution, supplier

**Verdict: NOTHING FOUND that is concrete and device-specific. Confidence: HIGH that this information is not public.**

- **No panel part number, native resolution, DPI, pixel pitch, center-offset, active-circle radius, or overscan figure** for the Napster View exists in any public source. Marketing states only "2.1-inch," "circular," and "high-resolution."
- **The panel supplier is not disclosed.** 3D Global customizes its lenticular filter to a third-party 2D LCD panel ("Customization of our 3D technology to suit your 2D panel"), so the base LCD is likely a stock small circular/round LCD sourced by faytech — but **no part number is documented.**
- **No teardown or FCC internal photos** exist to recover this (see below). This section is exactly where your own test-pattern-plus-photograph plan is the only viable path. No general lenticular theory is substituted here, per your instruction.

---

## Additional angles

- **App bundle / Metal shader / entitlements analysis:** **Nothing found.** No one has publicly published an analysis of the Napster for Mac app's frameworks, entitlements, private APIs, or Metal shaders as they relate to the View. This is a productive avenue for you: a host-side interleaving shader, if present, would be visible in the app's Metal library — its absence would further support device-side interleaving.
- **Packet captures / USB traces:** **Nothing found.** No public USB or DisplayPort traces of the device exist.
- **Firmware / homebrew / modding community:** **Nothing found.** No modding, homebrew, or reverse-engineering thread on GitHub, Reddit (r/hardware, r/lookingglass), Hacker News, or Discord (publicly indexed) discusses the Napster View's hardware. The device is too new and niche.
- **FCC / regulatory:** **No FCC ID found** for Napster View, Napster, or Infinite Reality. faytech holds FCC grantee codes 2AC8I and 2AWNG, but its most recent filing is dated 2022 and none match a 2.1" circular display. Napster's own regulatory page (napster.com/view/safety-and-regulatory) publishes only a 3D-effect health warning and a California Prop 65 statement, no FCC ID or SDoC. **Interpretation (inference):** a USB-C bus-powered display with no radio (no Wi-Fi/BT) is exempt from FCC Certification and would carry no FCC ID — it falls under the Supplier's Declaration of Conformity (SDoC) procedure, which is not filed with the FCC and does not appear in the equipment-authorization database. This means the usual "FCC internal photos" teardown shortcut is unavailable for this device. Confidence: high.
- **Onboard camera:** The View **has no built-in camera** in any published spec; reviewers note that context/vision comes from the **Mac's own webcam** (TechRadar hands-on). This is directly relevant to the eye-tracking-vs-fixed-multiview question above.
- **Developer program / SDK waitlist for View:** **Nothing found.** No public statement about future third-party app support for the View display specifically.
- **Weight (corroborating detail):** The device is "a small 2.1-inch display, surrounded in anodized aluminum that weighs just 65 grams" (Techigar hands-on; matches Napster's "sub-65g anodized aluminum").

---

## Established fact vs. inference vs. unknown

### (a) Documented fact (with named source)
- 3D Global GmbH (Aalen, Germany) is the named 3D-technology partner; faytech (Shenzhen) is the named manufacturer. (3d-global.com; faytech.com)
- 3D Global's IP derives from Secco GmbH; company entered market Dec 2017. (3d-global.eu)
- 3D Global's general architecture uses an FPGA processing unit integrated in the monitor housing and a "stereo with head tracking, for one person" display. (3d-global.com)
- LONTIUM (VID 0x2f61) is a fabless bridge-chip vendor (Hefei/Shenzhen); its LT-series parts are conventional DP/HDMI/Type-C↔MIPI/LVDS converters; none do lenticular interleaving. (lontiumsemi.com, kernel.org, lcsc.com)
- VID 0x291a = Anker Innovations. (the-sz.com / linux-usb.org)
- A USB Billboard device signals USB-C Alternate Mode status; its presence confirms standard DisplayPort Alt-Mode signaling. (Infineon)
- Corporate history: Infinite Reality acquired the Napster brand ($207M, March 2025), rebranded to Napster; acquired Touchcast ($500M cash+stock, announced 16 April 2025, $15.5B valuation); CEO John Acunto, CTO Edo Segal. (Adweek, GlobeNewswire, Rolling Stone)
- Device is Mac-only (M1+), USB-C, ~65 g anodized aluminum, $99 with subscription; launched 20 October 2025; recognized by USA TODAY/Reviewed at CES 2026. (GlobeNewswire, Tom's Guide, reviewed.com, Techigar)

### (b) Reasonable inference
- The 3D pixel interleaving happens **device-side** (in a separate processor/FPGA), not in the Lontium bridge and probably not pre-interleaved on the wire — inferred from 3D Global's architecture + the Lontium bridge's limitations + your coherent-2D-image framebuffer capture.
- The base LCD is a stock small round/circular panel sourced by faytech, with a 3D Global lenticular filter bonded on top.
- The device is exempt from FCC ID (SDoC-only) because it is a radio-less bus-powered display — hence no FCC teardown photos.

### (c) Speculation (explicitly flagged)
- Whether the View uses **eye-tracking (SingleView 2-view, needing the Mac webcam)** or a **fixed multi-view "5-lens" lenticular (no tracking)** is unresolved; the marketing "5-lens" language leans fixed-multiview, the 3D Global partnership leans eye-tracked. No source resolves it.
- Whether an FPGA is physically inside the retail unit (vs. a cheaper fixed ASIC/mapping) is unconfirmed.
- The specific roles of PID 0x8846 and PID 0x8355 are unknown.

### (d) Nothing found (no amount of web searching will close these — only hardware investigation can)
- Panel part number, native resolution, DPI, pixel pitch.
- Circular active-area center-offset, radius, overscan relative to the rectangular framebuffer.
- Whether interleaving is truly device-side vs. host-side (definitive proof).
- Presence/absence and identity of an internal FPGA or processor.
- App-side Metal shaders / entitlements bearing on the pipeline.
- USB/DP packet-level protocol details.
- Any View hardware SDK or developer access.

---

## Recommendations

**Stage 1 — Confirm the interleaving location (highest leverage).**
- Inspect the Napster for Mac app bundle: dump the Metal shader library (`.metallib`), list frameworks and entitlements, and look for a lenticular/interleaving/quilt shader. **If no interleaving shader exists in the app, that is strong positive evidence for device-side interleaving.** If one exists, host-side (c) becomes viable. This is the cheapest, most decisive test.
- Feed the device known synthetic test frames (flat gradients, single-pixel verticals, thin horizontal/vertical lines) at the native rectangular resolution and photograph the panel through a macro lens. A device-side interleaver will scramble a flat gradient into stripes on the panel while the framebuffer stays clean; a host-side interleaver will already show stripes in the framebuffer.

**Stage 2 — Recover panel geometry empirically (only path available).**
- Use your test-pattern-plus-photograph plan to map the circular active area: display a full-field rectangular grid, photograph, and measure the visible circle's center offset and radius in framebuffer pixels. There is no public datasheet to shortcut this.

**Stage 3 — Characterize the USB/DP link and open the unit.**
- Capture the USB-C CC-line PD/Alt-Mode negotiation (a USB-PD analyzer) to confirm DisplayPort Alt-Mode and read the advertised DP capabilities; capture the Billboard BOS descriptor to read the advertised SVIDs. This will confirm the link is plain DP and explain the two enumerations you observed.
- Physically open the unit (you own it) and photograph the PCB: identify the exact Lontium part number, look for a second processor/FPGA, check for a hidden camera module, and read the panel's flex-cable part markings. **This is the only way to resolve the FPGA, camera, and panel-supplier questions**, since no teardown or FCC photos exist.

**Benchmarks that would change the conclusions:**
- Finding a lenticular/quilt shader in the app → shifts toward host-side or quilt architecture.
- Finding an FPGA (e.g., Lattice/Xilinx/Anlogic) on the PCB → confirms the 3D Global device-side model.
- Finding an onboard camera → confirms eye-tracked SingleView; its absence supports fixed multiview.
- Reading a panel part number → unlocks resolution/DPI from that panel's datasheet.

---

## Caveats
- The two strongest partner claims (3D Global, faytech) are **self-published by the vendors**; they are mutually corroborating and consistent with the product, but there is no fully independent teardown confirming which 3D Global technology variant ships in the retail unit.
- 3D Global's FPGA/head-tracking architecture is its **general** capability description, not a Napster-View-specific spec sheet. Treat "device-side FPGA interleaving" as the best-supported hypothesis, not established fact.
- The "5-lens light-field engine" is **marketing language**; it has no corroborated technical definition in any datasheet, and it is in mild tension with 3D Global's 2-view SingleView approach.
- "Holographic" in all Napster materials is marketing; the technology is lenticular autostereoscopic 3D, as reviewers repeatedly note.
- Several USB PIDs (0x8846, 0x8355) and the empty-EDID behavior have **no device-specific public explanation**; interpretations here are inferences, not documented facts.
