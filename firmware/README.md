# Firmware

Two RP2040 firmwares. Both build with Pico SDK 2.3.1 and Arm GCC. The modem firmware needs
**TinyUSB 0.21.0** in the SDK (`lib/tinyusb`): every SDK up to 2.3.1 pins 0.18.0, whose RP2040 driver
panics the second time the computer opens an audio stream.

```sh
git -C $PICO_SDK_PATH/lib/tinyusb fetch --depth 1 origin tag 0.21.0
git -C $PICO_SDK_PATH/lib/tinyusb checkout 0.21.0
```

Then, with the SDK and an Arm GNU toolchain (14.2 here) wherever you keep them:

```sh
export PICO_SDK_PATH=/path/to/pico-sdk
cmake -S firmware/modem -B firmware/modem/build \
  -DPICO_TOOLCHAIN_PATH=/path/to/arm-gnu-toolchain-arm-none-eabi
cmake --build firmware/modem/build -j8      # -> build/koppar_modem.uf2
```

**Flashing.** `koppar-lte-modem flash` updates the modem firmware over its own
control port: the image goes to the upper half of the flash, its CRC is checked,
and code running from RAM copies it over the running firmware and reboots (about
5 s in all; don't unplug during it). The RP2040's ROM USB bootloader is only the
fallback (`--bootloader`), because it has no fix for erratum RP2040-E5 and,
behind the board's hub, sometimes never shows up on the computer.

A blank RP2040 starts that ROM bootloader by itself (the `RPI-RP2` drive): copy
the `.uf2` onto it. To force it later, short **BOOT** to **GND** on the back
while plugging in; on boards without those pads, bridge U6 pin 1 (flash CS) to
pin 4 (GND). `b` in the bring-up firmware also enters it.

**USB robustness.** The SDK's RP2040-E5 workaround is on (it borrows GPIO15,
unconnected on rev B), and if the computer has not configured the board 3 s
after it attached, the firmware detaches and attaches again.

On every start the three LEDs flash for 0.3 s; with the module off, NET blips
every 3 s to show the firmware is running.

## `bringup/`: board test, no module needed

USB serial, single-key commands (`?` lists them): flash ID, crystal, LEDs, the
PWRKEY / RESET_N transistors. Results for rev B are in [`REVIEW.md`](../REVIEW.md).

## `modem/`: the firmware the board runs

One USB device with two functions:

| Function | What the computer sees | Does |
|---|---|---|
| USB sound card (UAC2) | "Koppar LTE modem", 1 in / 1 out, 16-bit, 8 kHz | Output = what the caller hears, input = what the caller says |
| Control port (CDC) | a serial port | Module power, LEDs, audio route, counters |

USB ID 1209:0001 is the [pid.codes](https://pid.codes) test PID: fine on your
own desk, get a real PID before handing boards out.

**Call audio.** The EC25 is the PCM master (AT+QDAI, set by `koppar-lte-modem setup`):
2048 kHz clock, 8 kHz short sync, 16-bit linear. A PIO state machine
(`src/pcm.pio`) is the slave: it launches each uplink bit on the rising edge and
samples the downlink bit on the falling edge, as the EC25 hardware design
(3.12) specifies. An interrupt per frame moves one sample each way. The module's
clock and the computer's USB clock drift apart by tens of ppm; on the
microphone side TinyUSB's flow control sends one sample more or less per packet,
on the speaker side the interrupt drops or repeats one sample now and then.
For 16 kHz, build with `-DKOPPAR_SAMPLE_RATE=16000` and run `koppar-lte-modem setup --rate 16000`
(the module then clocks at 4096 kHz).

With no call (no PCM clock) the microphone stream carries silence, so apps see
a steady device. `audio loopback` sends the speaker back to the microphone and
`audio tone` plays a 1 kHz tone: both test the sound card with no module.

**Module power** (`src/modem.c`), from the EC25 hardware design V2.4:

| | Sequence |
|---|---|
| On | PWRKEY low (GPIO22 high) for at least 500 ms, released once STATUS goes low (max 1 s); STATUS within 15 s |
| Off | `AT+QPOWD=1` first (the software does), else PWRKEY low for 800 ms; STATUS clears within 35 s |
| Reset | RESET_N low (GPIO23 high) for 300 ms (150-460 allowed): last resort |

STATUS (open drain, low = on) and RI come through the TXS0108E, which is
disabled while the module is off; the RP2040's pull-ups then read "off" and
"idle". `autopower on` (saved in flash) powers the module at plug-in.

**LEDs** (`led net|ring|call auto|on|off|slow|fast`). Auto: NET is off with the
module off, blinks fast while it starts and slowly once on (the daemon turns it
solid when registered); RING flashes on each RI pulse (the daemon blinks it
while a call rings); CALL is on while call audio flows.

**Control port protocol.** One command per line; reply lines then `OK` or
`ERR <reason>`; events are lines starting with `! ` (`! status on`, `! ri`,
`! pcm on`). Commands: `help`, `version`, `status`, `power on|off|reset`,
`autopower [on|off]`, `audio [pcm|loopback|tone]`, `led ...`, `fw begin|w|commit`,
`bootloader`.
