# `koppar-lte-modem`: the computer side

A command line, a background daemon and a Python API for the board: AT
commands, texts, calls, call audio, events for scripts and AI agents. On
macOS, **Koppar Phone** puts all of it in the menu bar.

## Install

From the [releases](https://github.com/Koppar-AI/koppar-lte-modem/releases) page:

| You have | Download | Then |
|---|---|---|
| A Mac with Apple Silicon | `Koppar-Phone-<version>-macos-arm64.zip` | Unzip, move Koppar Phone to Applications, open it. The command line is inside: `Koppar Phone.app/Contents/Resources/cli/koppar-lte-modem` |
| A Mac with an Intel chip | `Koppar-Phone-<version>-macos-x86_64.zip` | The same |
| Linux, x86-64 or ARM64 | `koppar-lte-modem-<version>-linux-<arch>.tar.gz` | Unpack and run `koppar-lte-modem/koppar-lte-modem`. Add yourself to the `dialout` group for the module's serial ports, and install `libportaudio2` for call audio |
| Python 3.10+ anywhere | `koppar_lte_modem-<version>-py3-none-any.whl` | `pip install '<file>[audio]'` (macOS also needs `brew install libusb`) |

The standalone downloads carry their own Python: nothing else to install. If a
release isn't signed with a Developer ID, macOS asks you to confirm Koppar Phone the
first time: right-click it, Open. Windows isn't supported yet: the daemon talks
over a Unix socket, and the EC25 needs Quectel's Windows drivers.

## Koppar Phone, the menu-bar app (macOS)

- **The icon**: signal bars, and how many texts are unread. Click it for the
  network, your latest conversations, Text, Call, Open at login and Quit.
- **Open Full App**: the full interface in a window (and in the Dock while it's
  open): overview, messages, calls, events with all their data, an AT
  console with every command anyone sent, network and cell details, USSD,
  automations, and the device.
- **A call**: the icon turns into a copper phone and a small call panel floats
  over everything: answer (on the Mac's microphone and speakers), decline,
  record, keypad, hang up. No notification permission needed.
- **Voicemail**: switch it on and Koppar Phone answers the calls you don't pick
  up (before the operator's voicemail does), plays your greeting (typed, and
  spoken by the Mac, or recorded) and keeps the message. The operator's own
  voicemail still takes calls while the Mac or the board is off; its "new
  messages" signal shows up, with a button to call it.
- **Recordings**: calls you record play back from the call log.
- It starts `koppar-lte-modem daemon` itself and stops it when you quit.
  History lives in `~/Library/Application Support/koppar-lte-modem`.

Build it yourself with the Command Line Tools: `macos/build.sh` (see the top
of the script). Without the app, `koppar-lte-modem daemon` serves the same
interface on 127.0.0.1:8025 and `koppar-lte-modem ui` opens it in a browser.

## From source

```sh
cd host
python3 -m venv .venv && .venv/bin/pip install -e '.[audio]'
brew install libusb            # macOS: talks to the EC25's AT port directly
.venv/bin/koppar-lte-modem status
```

On macOS the EC25's serial ports have no driver, so `koppar-lte-modem` opens the AT
interface (USB interface 2) through libusb. On Linux it uses `/dev/ttyUSB2`
from the `option` driver. Set `KOPPAR_AT_PORT` to override.

## First run with the module

```sh
koppar-lte-modem power on                # PWRKEY; the module's USB ports appear ~10 s later
koppar-lte-modem info                    # model, firmware revision, IMEI, ICCID
koppar-lte-modem setup --autopower       # PCM audio for the board (AT+QDAI), URCs on the AT port; restarts the module
koppar-lte-modem status                  # SIM, network, operator, signal
koppar-lte-modem audio test              # sound card loopback + tone (no call needed)
```

`koppar-lte-modem setup --apn <apn>` also switches the module to ECM: a USB network
adapter macOS and Linux drive without extra software; the module routes and
hands out 192.168.225.x by DHCP.

## Commands

| | |
|---|---|
| `koppar-lte-modem status`, `info` | Board, SIM, registration, signal (RSRP/RSRQ/SINR on LTE), calls |
| `koppar-lte-modem power on\|off\|reset\|restart` | `off` is graceful (`AT+QPOWD`): run it before unplugging |
| `koppar-lte-modem sms send <number> <text>` | Long or non-GSM text (accents, emoji) handled: UCS2, numbered parts |
| `koppar-lte-modem sms list [--unread]`, `read <i>`, `delete <i\|all>` | Read as PDUs: long texts come back whole (`delete` removes every part), named senders (a shop, a bank) by name |
| `koppar-lte-modem call <number>`, `answer`, `hangup`, `calls`, `dtmf <digits>` | |
| `koppar-lte-modem audio play <wav>`, `record <wav> -s 10`, `bridge` | Into the call, from the call, or your mic and speakers on the call |
| `koppar-lte-modem at [cmd]` | One AT command, or an AT console |
| `koppar-lte-modem board <cmd>` | Raw control-port command (`status`, `led ring fast`, ...) |
| `koppar-lte-modem flash [file.uf2]` | Update the board firmware over the control port (`--bootloader`: through the ROM bootloader) |
| `koppar-lte-modem daemon`, `events` | See below |

Add `--json` for machine-readable output.

## The daemon: events for scripts and agents

```sh
koppar-lte-modem daemon --on-event ./on-event.sh
```

It powers the module on, waits for it, sets up caller ID and SMS notification,
then reports events: `ready`, `network`, `ring`, `incoming_call`,
`call_state`, `call_ended`, `sms`, `call_audio`, `module_on`, `module_off`,
`modem_disconnected`. Each `--on-event` command runs with `KOPPAR_EVENT`,
`KOPPAR_NUMBER`, `KOPPAR_TEXT` (and the whole event as `KOPPAR_JSON`) set.
While the daemon runs, every other `koppar-lte-modem` command goes through it, so a hook
can answer the call it was told about:

```sh
#!/bin/sh
# on-event.sh: answer calls from one number, auto-reply to texts
case "$KOPPAR_EVENT" in
  incoming_call) [ "$KOPPAR_NUMBER" = "+447700900123" ] && koppar-lte-modem answer ;;
  sms) koppar-lte-modem sms send "$KOPPAR_NUMBER" "Got it: $KOPPAR_TEXT" ;;
esac
```

`koppar-lte-modem events --json` streams the same events to any program. It also keeps
the LEDs meaningful: NET solid once registered, RING blinking while a call
rings.

## Python

```python
from koppar_lte_modem import Link
from koppar_lte_modem.audio import CallAudio

with Link.open() as link:              # via the daemon if it runs
    m = link.modem()
    m.send_sms("+447700900123", "hej")
    m.dial("+447700900123")
    with CallAudio() as call:          # float32 mono at call.rate (8 kHz)
        call.play_wav("greeting.wav")
        heard = call.record(5.0)       # what the other side said
    m.hangup()
```

For an agent: run the daemon, react to `incoming_call`, `koppar-lte-modem answer`, then
stream `CallAudio` to your speech-to-text and from your text-to-speech.

## Tests

`tests/fake_ec25.py` simulates the module on a pseudo-terminal:

```sh
PYTHONPATH=tests .venv/bin/python -m pytest tests
```

## Security

- **Your data stays yours.** Texts, calls, recordings and voicemail live in
  `~/Library/Application Support/koppar-lte-modem` (Linux:
  `~/.local/share/koppar-lte-modem`), readable by your user account only.
- **The UI listens on 127.0.0.1 only**, and every request needs a key: the
  master token in that folder (for the CLI and the app, sent as a header) or a
  session. A window opens with a one-time launch code that the page swaps for
  a session kept in that tab; no key ever appears in a URL, the browser
  history or `ps`. Requests must name 127.0.0.1 as their host (no DNS
  rebinding), and the page's content policy allows no third-party code.
- **Settings are checked** before they reach the modem: numbers must be
  numbers (they end up in AT commands), the webhook must be http(s).
- **Automatic texts are capped** at 30 an hour (auto-replies and forwards), so
  a flood of texts with spoofed senders can't make the line send hundreds.
- **What it can't protect:** any program running as your user can read the
  master token, like any other file of yours. On macOS, other user accounts
  on the same Mac can open the board's USB serial ports directly. And the
  board accepts a firmware update from anything that can open its control
  port (the image is checked with a CRC, not a signature); see `REV-C.md`.

## Releasing

Push a tag: `git tag v0.3.0 && git push origin v0.3.0`.
[`.github/workflows/release.yml`](../.github/workflows/release.yml) runs the
tests, then builds the standalone CLI for macOS (arm64, x86_64) and Linux
(x86_64, arm64), Koppar Phone.app with that CLI inside, and a wheel and sdist, and
publishes them as a GitHub release with `SHA256SUMS`. Run the workflow by hand
for a test build (artifacts only).

To build the same things on a Mac: `packaging/build-cli.sh` (needs PyInstaller
and `KOPPAR_LIBUSB_DYLIB`), then `KOPPAR_EMBED_CLI=../dist/koppar-lte-modem
macos/build.sh`.

To sign and notarize the macOS app, add the repository secrets listed at the
top of the workflow (a Developer ID certificate and an app-specific password).
