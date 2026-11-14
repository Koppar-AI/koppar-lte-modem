# `koppar-lte-modem`: the computer side

A command line, a background daemon and a Python API for the board: AT
commands, texts, calls, call audio, events for scripts and AI agents.

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
| `koppar-lte-modem sms list [--unread]`, `read <i>`, `delete <i\|all>` | |
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
