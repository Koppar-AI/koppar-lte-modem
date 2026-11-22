# Rev C: what to change, from bringing up rev B

A running list. Each item says what happened on rev B and what rev C should do. Items still open
from the design review ([`REVIEW.md`](REVIEW.md#still-open)) are folded in.

## Recovery and debugging

- **Real BOOT and RESET buttons.** Rev B has only pads (BOOT + GND, SWD, USB_BOOT + 1V8), and the
  first-order boards were built without them, so forcing the RP2040 bootloader meant bridging U6
  pin 1 to pin 4 with tweezers while plugging in. Rev C: a tactile BOOT button (QSPI_CS through
  R25, as now) and a RESET button on RUN, both reachable from the top and from an enclosure.
- **SWD on a header** (or a Tag-Connect footprint), not bare pads.
- **USB_BOOT for the EC25** on a jumper or a small button (through R26, as now), so a failed module
  update can be recovered without soldering.
- **The EC25's main UART to the RP2040.** On board 2, the module started (STATUS low, RI pulsing) but
  its USB had an open joint, and with USB the only way in there was no AT access at all. The
  TXS0108E (U7) has two unused channels (A7/B7, A8/B8): route the EC25's MAIN_TXD and MAIN_RXD
  through them to an RP2040 UART, and the firmware can bridge AT commands to the control port. A
  second way in, and AT access while the module's USB is still booting.

## Security

- **Firmware updates should need more than a serial port.** Rev B accepts a
  new firmware from any program that can open the control port (`fw begin`,
  `fw w`, `fw commit`, or `bootloader` for the ROM's USB drive); the image is
  checked with a CRC only. A malicious firmware could present itself as a USB
  keyboard. Rev C: sign release images (Ed25519, key off the board, public key
  in the firmware) and verify before copying; or allow updates only while the
  BOOT button is held, so they need someone at the board.

## SIM

- **Draw the card on the silkscreen:** outline, cut corner, "contacts down" and an arrow at the
  slot (see [`images/sim-insertion.svg`](images/sim-insertion.svg)). On rev B the only cue is the
  socket itself. The drawing puts the cut corner first, on the left seen from above. That comes
  from J2's contact layout and the card, not yet from an actual insertion: confirm it the first
  time a card clicks in.
- **Swap the card-detect wiring:** rev B has CSW to USIM_PRESENCE and DSW to GND; JAE's table has
  CSW (ground frame) to GND and DSW to the detect pin. Then `AT+QSIMDET` hot-plug detection can be
  turned on.

## The module (U8) and its power

- **Placing the EC25 by hand is the weak point.** Both rev B boards needed a second soldering pass:
  board 1's module didn't start at all (VBAT, PWRKEY or ground pads), and board 2's started but
  never showed up on USB (pins 69 to 71). Rev C: have the assembler place it (consign the module)
  or ship a stencil for paste and a hot plate. Either way, make the pads extend past the module
  edge enough to inspect and touch up with an iron.
- **Test pads next to the module** for VBAT (at the module, not at the buck), PWRKEY, STATUS and
  VDD_EXT. Rev B's 14 pads cover the rails and the USB pairs but not the module's control pins,
  which were the ones in doubt.
- **A "module on" LED** on VDD_EXT (1.8 V, a few hundred µA). With rev B there was no way to see
  whether the module had started without the firmware and a computer.
- **Measure the current.** A current-sense amplifier on +3V8 into an RP2040 ADC pin: it would have
  told "no power reaching the module" from "no key press" without a multimeter, and the host app
  can show power use and transmit bursts.
- **Supply margin.** +3V8 read 3.90 V unloaded and 3.81 V under the module (its own `AT+CBC`): fine.
  The board still needs a 3 A USB-C port (5.1 kΩ CC resistors negotiate nothing); consider more
  bulk capacitance on +3V8, or USB PD, so a 1.5 A port survives a 2G burst.

## USB

- **Strap off the hub's unused ports 3 and 4** (SL2.1A): they are pulled down but may enumerate as
  empty ports.
- **RP2040 behind the hub:** the ROM bootloader sometimes doesn't enumerate after a warm reset
  (erratum RP2040-E5). Firmware 0.2 works around it; the BOOT button above is the hardware fallback.
- **A real USB PID.** 1209:0001 is the pid.codes test PID: fine on a desk, not for boards that
  leave it.

## Parts to confirm

- **Crystal load capacitors:** confirm against the chosen crystal's datasheet (Y1 CL 12 pF, Y2 CL
  10 pF).
- **VoLTE:** optional on the EC25-EUX. Where 2G and 3G are switched off, calls need a module firmware
  with VoLTE and a carrier that allows it. Check before ordering modules.

## Enclosure (optional, later)

- **Openings on two sides:** USB-C on the left edge (J1), SIM slot on the bottom edge (J2, right of
  centre). The SIM pushes in about 0.75 mm and springs out 3.9 mm: leave room for a fingertip or a
  pin.
- **Antennas:** a plastic case lets the two flexible antennas (e.g. Taoglas FXUB63) stick inside the
  lid, at least a few centimetres apart and away from the module. A metal case needs two SMA
  bulkheads with U.FL pigtails to J3 and J4 at the top edge.
- **Light pipes** for the NET, RING and CALL LEDs (in a row near the bottom edge, about 37 to 48 mm
  from the left).
- **Buttons:** access to BOOT and RESET (above), recessed so they aren't pressed by accident.
- **Mounting:** the four holes (H101 to H104). Some airflow over the EC25, which runs warm in long
  calls and data sessions.
- 3D-print the first one; make the model from `board/koppar-lte-modem.kicad_pcb` (KiCad's STEP
  export, with the module's model in `board/models`).

## Host app

The command line and daemon work; a visual app on top of the daemon (its events stream and its
command path) would cover:

- **Status:** board and module power, SIM, network, operator, signal bars with RSRP/RSRQ/SINR over
  time, temperature and supply voltage.
- **Controls:** power on/off/restart, setup, autopower, LEDs, audio route.
- **Texts:** inbox, conversations, send (long and non-GSM text already handled).
- **Calls:** dial, answer, hang up, DTMF, call log; audio devices, bridge to headset, record.
- **Maintenance:** firmware update over the control port, module info, an AT console, the event log.
