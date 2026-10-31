# Design review

We reviewed the first version of the board the way we would review a colleague's board: part by part,
against the datasheets. This file has the numbers, and what we changed.

## What passed

| Check | Result |
|---|---|
| KiCad DRC (KiCad 10) | 0 errors, 0 unconnected |
| Connectivity | 76/76 nets (71 in the reviewed version, plus the five debug and boot nets) |
| USB 2.0 pairs | 90 Ω class, routed as pairs |
| Buck (TPS563201) to the module | 3.8 V rail with bulk capacitance next to the VBAT pins |

## What we changed

| Finding in the first version | Why it matters | Now |
|---|---|---|
| Antenna feeds ANT_MAIN and ANT_DIV are 0.33 mm wide with a 0.20 mm gap to the top-layer ground: **35 Ω** grounded coplanar on the JLC04161H-3313 stackup (0.0994 mm prepreg to the In1 ground plane) | The module and the antennas are 50 Ω; a 35 Ω line is a mismatch at the connector | **0.20 mm with a 0.20 mm gap: 49.3 Ω.** Both feeds entirely on the top layer, ground stitching vias along them |
| ANT_MAIN drops to the bottom layer through two vias for 4 mm, under a +5V track | Each via is a discontinuity, and the bottom layer has no ground reference right below it | The +5V track moved; ANT_MAIN has no vias |
| VBAT (+3.8 V) copper 1.37 mm | Quectel's EC25 hardware design guide asks for at least 2 mm to VBAT_RF: the module draws about 2 A in transmit bursts | 2.0 mm class; widened wherever the clearances allow (1.4-2.0 mm along the run) and a solid copper area from the bulk capacitors into the four VBAT pins |
| The SIM socket's card mouth faces into the board, toward the module | A card cannot go in once the board is in an enclosure | Socket turned 180°: its front end (JAE: "PCB edge = connector front end") is flush with the bottom edge |
| LED values were their functions (NET_OK, RINGING, CALL_AUDIO) | A BOM cannot order a function | Green, Amber, Blue, as the spec says |
| The BOM matched the RP2040 to a Pico board | It is a chip on this board | RP2040 (LCSC C2040) |
| The buck's switch pin reached its copper through a 0.1-0.24 mm neck (3 A path) | IPC-2221 puts a 3 A track that narrow hundreds of degrees over | See the next row: the switch node is now a 4 mm track, 1.5 mm wide, narrowing to 0.6 mm only at U3's pin |
| The buck's inductor L1 sat about 60 mm from the buck U3, so the switch node crossed most of the board as a copper pour | A switch node should be a few millimetres: it radiates and rings | U3, its input and bootstrap capacitors, the feedback divider and the switch-node test pad moved under L1: switch pin to inductor pad is 4.1 mm |
| Tracks drawn at any angle, with many short jogs | Harder to inspect and rework, and looks unfinished | Copper redrawn at 0° and 45° (1427 to 1125 segments), power nets at their class width; the four USB pairs within 0.3 mm of length difference (the upstream pair had 7 mm, now 0.1 mm) |
| 90 vias inside SMD pads (3 in pads narrower than 0.5 mm), none of them needed: no pad on this board is walled in | A via in a pad must be filled and capped (POFV) at the fab, or the paste runs down the hole and starves the joint | Every via moved beside its pad (a short track to it); the signal nets that ran through pads re-routed. No via in a pad: a standard build, no POFV |
| No test points; LEDs scattered | Bring-up needs somewhere to probe | 14 test pads (+5V, +3V3, +3V8, +1V8, DVDD, GND x2, the switch node, the three USB pairs); the LEDs in a labelled row (NET, RING, CALL) |
| No way to recover a bricked RP2040 (no BOOTSEL by spec), no SWD, and the EC25's USB_BOOT unconnected | A firmware that crashes before USB comes up cannot be reflashed from software; a failed module update needs USB_BOOT to get back into download mode | Seven pads on the back, labelled: **BOOT** (QSPI_CS through R25, 1 kΩ) next to **GND**; **SWD** CLK, DIO, GND; **USB_BOOT** next to **1V8** (through R26, 4.7 kΩ, Quectel's USB_BOOT reference circuit). The DVDD link under the RP2040 moved to free SWCLK and SWDIO |

Impedances are the grounded coplanar waveguide closed form (εr 4.4, copper
thickness neglected); worth confirming with your fab's impedance calculator before
ordering a controlled-impedance build.

## Ordering notes (checked 2026-10-30 against the live JLCPCB catalogue)

Use `board/fab/jlc/BOM-JLCPCB.csv` and `CPL-JLCPCB.csv`. Every via is 0.45/0.2 mm and none sits in a pad: at JLCPCB pick **Min via hole size 0.2mm/(0.3/0.35mm)** and **Via covering: Plugged**. No via-in-pad option, and no 4-wire Kelvin test (it applies only to vias with a ring under 0.125 mm). The files correct the automatic part matches:

- L1: SRP1245A-**4R7M** (C3221079), 4.7 uH. Only a few in stock: order them first.
- C36, C37: 220 uF / 10 V polymer in the 6.3 x 5.8 mm footprint (JBLM2221M010C058RTM, C19270700).
- Y1: X322512**MOB**4SI (C70565, CL 12 pF) and Y2: X322512**MMB**4SI (C50430, CL 10 pF). The schematic's X322512MSB4SI is a CL 20 pF part, which the 18 pF and 15 pF load capacitors do not suit.
- U8: JLCPCB has none of the EC25-EUX variants in stock. EC25EUXGA-128-SGNS is end-of-life; its replacement is **EC25EUXGR-128-SGNS** (same LGA footprint, its own firmware). Quectel's EC25 series specification V2.5 lists PCM digital audio and voice for the EC25-EUX (only the North American EC25-AFDL and EC25-AFXD are data-only); consign it. Quectel's alternative-component notices (a second memory supplier) keep form, fit and function and the ordering code, but a module built after one runs only the new firmware.
- Rotations in `CPL-JLCPCB.csv` were checked in JLCPCB's placement preview (2026-10-30): J2 (SIM socket, card mouth at the board edge) 0°, J3/J4 (U.FL, narrow signal leg on the small pad toward the module, ground legs on the side pads) 0°. The LEDs stay at the angles the EasyEDA footprints give (all three with the anode on the right). Glance at the preview anyway before confirming.

## Bring-up and recovery (pads on the back)

- **RP2040, first flash:** blank flash boots straight into the USB bootloader. Copy a `.uf2` to the `RPI-RP2` drive or run `picotool load`.
- **RP2040, a bricked firmware:** short **BOOT** to **GND** (tweezers) while plugging in USB: the RP2040 comes up in its USB bootloader. R25 (1 kΩ) keeps the short safe if the chip is driving its flash select.
- **RP2040, debugging:** **SWD** CLK / DIO / GND take a Pico Debug Probe or any CMSIS-DAP probe (OpenOCD `target/rp2040.cfg`).
- **EC25, a failed firmware update:** hold **USB_BOOT** shorted to **1V8** while plugging in, so the module powers up with it held (Quectel's note: short the test points to force download mode): it enters emergency download mode for Quectel's QFirehose / QFlash. Release once the download port appears.

## Still open

- The SIM socket's card-detect switch is wired CSW to USIM_PRESENCE and DSW to GND; JAE's table has CSW (the ground frame) to ground and DSW to the detect pin. Harmless with the EC25's SIM hot-plug detection off (`AT+QSIMDET`, off by default); swap in the next revision.
- The SL2.1A hub's unused ports 3 and 4 are pulled down but not strapped off: they may enumerate as empty ports.
- The crystal load capacitors assume a load capacitance that the chosen crystal's datasheet should confirm.
- VoLTE is optional on the EC25-EUX (Quectel's specification): where the 2G and 3G networks are switched off, calls need a firmware with VoLTE and a carrier that allows it. Ask the seller for VoLTE firmware.
