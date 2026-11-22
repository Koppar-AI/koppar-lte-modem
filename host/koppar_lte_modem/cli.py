"""`koppar-lte-modem`: the command line for the Koppar LTE modem.

Every command goes through `koppar-lte-modem daemon` when it runs, else straight to the
hardware. Add --json to print machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .at import ATError
from .board import BoardError
from .link import DaemonLink, Link
from .transport import TransportError


def _print(args, obj, human=None):
    if args.json:
        print(json.dumps(obj, indent=2, default=lambda o: o.as_dict() if hasattr(o, "as_dict") else str(o)))
    elif human:
        human(obj)
    elif isinstance(obj, dict):
        width = max((len(k) for k in obj), default=0)
        for k, v in obj.items():
            print(f"{k:<{width}}  {v}")
    else:
        print(obj)


def _wait_module(link: Link, want: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if link.board_status().get("module") == want:
            return True
        time.sleep(0.5)
    return False


# ---- commands -------------------------------------------------------------

def cmd_status(args, link: Link):
    out = {"via": link.via}
    try:
        b = link.board_status()
        out.update({"firmware": b.get("fw"), "module": b.get("module"), "call_audio": b.get("pcm"),
                    "audio_mode": b.get("audio"), "autopower": b.get("autopower")})
    except (BoardError, OSError) as e:
        out["board"] = f"not found ({e})"
    if out.get("module") in (None, "on"):
        try:
            m = link.modem()
            out["sim"] = m.sim()
            reg = m.registration()
            out["network"] = reg["state"]
            out["operator"] = reg.get("operator")
            out["tech"] = reg.get("tech")
            out["band"] = reg.get("band")
            sig = m.signal()
            out["signal"] = f"{sig.get('rssi_dbm')} dBm ({sig.get('bars', 0)}/4)" if sig.get("rssi_dbm") else "none"
            if "rsrp" in sig:
                out["lte"] = f"RSRP {sig['rsrp']} dBm, RSRQ {sig.get('rsrq')} dB, SINR {sig.get('sinr')} dB"
            calls = m.calls()
            out["calls"] = ", ".join(f"{c.direction} {c.number} {c.state}" for c in calls) or "none"
        except (ATError, TransportError) as e:
            out["modem"] = f"not reachable ({e})"
    _print(args, out)


def cmd_info(args, link):
    _print(args, link.modem().info())


def cmd_power(args, link):
    if args.action == "on":
        link.board("power on")
        if args.wait:
            ok = _wait_module(link, "on", 20)
            print("module on; its USB ports appear about 10 s later" if ok else "STATUS never went low: check +3V8 and the module")
    elif args.action == "off":
        try:
            link.modem().power_down()                 # graceful: AT+QPOWD=1
            print("AT+QPOWD sent")
        except (ATError, TransportError) as e:
            print(f"AT power-down failed ({e}); using PWRKEY")
            link.board("power off")
        ok = _wait_module(link, "off", 40)
        print("module off: safe to unplug" if ok else "module still on after 40 s")
    elif args.action == "reset":
        link.board("power reset")
        print("RESET_N pulsed")
    elif args.action == "restart":
        link.modem().restart()
        print("AT+CFUN=1,1 sent: the module re-enumerates in ~15 s")


def cmd_setup(args, link):
    m = link.modem()
    changed = m.setup(rate=args.rate)
    if args.apn:
        d = m.data_setup(args.apn)
        changed["data"] = d
        changed["reboot_needed"] = changed["reboot_needed"] or d["reboot_needed"]
    if args.autopower is not None:
        changed["autopower"] = link.board(f"autopower {'on' if args.autopower else 'off'}")[0]
    fw_rate = link.board_status().get("rate")
    if fw_rate and int(fw_rate) != args.rate:
        changed["warning"] = f"the board firmware runs at {fw_rate} Hz; rebuild it with -DKOPPAR_SAMPLE_RATE={args.rate}"
    if changed.get("reboot_needed") and not args.no_restart:
        m.restart()
        changed["restarted"] = True
    _print(args, changed)


def cmd_at(args, link):
    if args.command:
        cmd = " ".join(args.command)
        if not cmd.upper().startswith("AT"):
            cmd = "AT" + cmd
        try:
            for line in link.at(cmd, timeout=args.timeout):
                print(line)
            print("OK")
        except ATError as e:
            for line in e.lines:
                print(line)
            print(e.result)
            sys.exit(1)
        return
    print(f"AT console ({link.via}). Ctrl-D to quit.")
    while True:
        try:
            cmd = input("AT> ").strip()
        except EOFError:
            print()
            return
        if not cmd:
            continue
        if not cmd.upper().startswith("AT"):
            cmd = "AT" + cmd
        try:
            for line in link.at(cmd, timeout=args.timeout):
                print(line)
            print("OK")
        except ATError as e:
            for line in e.lines:
                print(line)
            print(e.result)


def cmd_sms(args, link):
    m = link.modem()
    if args.action == "send":
        refs = m.send_sms(args.number, " ".join(args.text))
        _print(args, {"sent": len(refs), "references": refs})
    elif args.action == "list":
        msgs = m.list_sms("REC UNREAD" if args.unread else "ALL")

        def human(ms):
            for s in ms:
                slots = f"[{s.index}]" if len(s.parts) <= 1 else f"[{'+'.join(map(str, s.parts))}]"
                print(f"{slots} {s.status:<10} {s.number:<16} {s.time}")
                print(f"    {s.text}")
            if not ms:
                print("no messages")
        _print(args, msgs, human)
    elif args.action == "read":
        msg = m.read_sms(args.index)
        _print(args, msg, lambda s: print(f"{s.number} {s.time}\n{s.text}") if s else print("no such message"))
    elif args.action == "delete":
        if args.index == "all":
            m.delete_all_sms()
            print("deleted all")
        else:
            slots = m.delete_message(int(args.index))
            print(f"deleted {'slot' if len(slots) == 1 else 'slots'} {', '.join(map(str, slots))}")


def cmd_call(args, link):
    link.modem().dial(args.number)
    print(f"dialling {args.number}; `koppar-lte-modem hangup` to end")


def cmd_answer(args, link):
    link.modem().answer()
    print("answered")


def cmd_hangup(args, link):
    link.modem().hangup()
    print("hung up")


def cmd_calls(args, link):
    calls = link.modem().calls()
    _print(args, calls, lambda cs: [print(f"[{c.index}] {c.direction:<3} {c.state:<9} {c.number}") for c in cs] or print("no calls"))


def cmd_dtmf(args, link):
    link.modem().dtmf(args.digits)


def cmd_data(args, link):
    m = link.modem()
    if args.action == "setup":
        d = m.data_setup(args.apn)
        if d["reboot_needed"]:
            m.restart()
            d["restarted"] = True
        _print(args, d)
    else:
        _print(args, m.data_status())


def cmd_board(args, link):
    for line in link.board(" ".join(args.command)):
        print(line)


def _uf2_drive():
    import getpass
    import os

    user = getpass.getuser()
    for d in ("/Volumes/RPI-RP2", f"/media/{user}/RPI-RP2", f"/run/media/{user}/RPI-RP2"):
        if os.path.isdir(d):
            return d
    return None


def _uf2_image(path: str) -> bytes:
    """Flatten an RP2040 .uf2 into the flash image it describes (from 0x10000000)."""
    import struct

    data = open(path, "rb").read()
    chunks = {}
    for i in range(0, len(data) - 511, 512):
        m0, m1, flags, addr, size, _, _, family = struct.unpack_from("<8I", data, i)
        if m0 != 0x0A324655 or m1 != 0x9E5D5157 or flags & 1:
            continue
        chunks[addr] = data[i + 32: i + 32 + size]
    if not chunks or min(chunks) != 0x10000000:
        sys.exit(f"{path}: not an RP2040 flash image")
    end = max(a + len(d) for a, d in chunks.items())
    img = bytearray(b"\xff" * (end - 0x10000000))
    for a, d in chunks.items():
        img[a - 0x10000000: a - 0x10000000 + len(d)] = d
    return bytes(img)


def _wait_board(timeout: float = 20) -> str | None:
    from .board import Board, find_board_port

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_board_port():
            time.sleep(0.5)
            try:
                with Board() as b:
                    return b.command("version")[0]
            except (BoardError, OSError):
                pass
        time.sleep(0.25)
    return None


def _flash_via_port(link, image: bytes) -> bool:
    """Update through the firmware's own `fw` commands: no ROM bootloader involved."""
    import zlib

    try:
        if "fwupdate=1" not in link.board("version")[0]:
            return False
    except (BoardError, OSError):
        return False
    crc = zlib.crc32(image) & 0xFFFFFFFF
    link.board(f"fw begin {len(image)} {crc:08x}", timeout=20)
    step = 128
    for off in range(0, len(image), step):
        link.board(f"fw w {off:x} {image[off:off + step].hex()}", timeout=5)
        if off % (step * 64) == 0:
            print(f"\r  {100 * off // len(image):3d} %", end="", flush=True)
    print("\r  100 %")
    link.board("fw commit", timeout=10)
    link.close()
    return True


def cmd_flash(args, link):
    """Update the board firmware: over the control port when the running
    firmware supports it, else through the RP2040's ROM USB bootloader."""
    import os
    import shutil

    if not os.path.isfile(args.file):
        sys.exit(f"no such file: {args.file}")
    image = _uf2_image(args.file)
    if not args.bootloader and _uf2_drive() is None and _flash_via_port(link, image):
        print(f"sent {len(image)} bytes over the control port; the board is rebooting")
    else:
        if _uf2_drive() is None:
            try:
                link.board("bootloader")
            except (BoardError, OSError):
                pass                              # it may drop off USB before answering
            link.close()
        deadline = time.monotonic() + 20
        while (drive := _uf2_drive()) is None:
            if time.monotonic() > deadline:
                sys.exit("no RPI-RP2 drive: replug the board (if it stays away, bridge flash U6 pin 1 to pin 4 while plugging in)")
            time.sleep(0.25)
        for _ in range(20):                       # the drive mounts read-only for a moment
            try:
                shutil.copyfile(args.file, os.path.join(drive, os.path.basename(args.file)))
                break
            except OSError:
                time.sleep(0.5)
        else:
            sys.exit(f"could not write to {drive}")
        print(f"copied {args.file} to {drive}")
    version = _wait_board()
    if version is None:
        sys.exit("the board did not come back with the control port")
    print(version)


def cmd_daemon(args, _link):
    from .daemon import Daemon

    Daemon(hooks=args.on_event, json_out=args.json, power_on=not args.no_power_on,
           delete_sms=args.delete_sms, pin=args.pin, ui_port=None if args.no_ui else args.ui_port,
           popup=not args.no_popup).run()


def cmd_ui(args, _link):
    from .daemon_ui import open_ui
    from .store import Store
    from .web import ui_url

    if DaemonLink.connect() is None:
        sys.exit("koppar-lte-modem daemon is not running: start it first (koppar-lte-modem daemon)")
    url = ui_url(args.port, Store().ui_token())
    if args.print:
        print(url)
    else:
        open_ui(url)
        print(f"opened http://127.0.0.1:{args.port}/")


def cmd_events(args, _link):
    d = DaemonLink.connect()
    if d is None:
        sys.exit("koppar-lte-modem daemon is not running")
    try:
        for ev in d.events():
            if args.json:
                print(json.dumps(ev), flush=True)
            else:
                rest = {k: v for k, v in ev.items() if k not in ("event", "time")}
                print(f"{ev['time']}  {ev['event']:<18} {rest}", flush=True)
    except KeyboardInterrupt:
        pass


def cmd_audio(args, link):
    from . import audio

    if args.action == "test":
        r = audio.loopback_test(link.board)
        _print(args, r)
        r2 = audio.tone_test(link.board)
        _print(args, {"tone_" + k: v for k, v in r2.items()})
        if not (r["pass"] and r2["pass"]):
            sys.exit(1)
    elif args.action == "devices":
        inp, out, rate = audio.find_device()
        _print(args, {"input": inp, "output": out, "rate": rate})
    elif args.action == "play":
        audio.play_file(args.file)
    elif args.action == "record":
        audio.record_file(args.file, args.seconds)
        print(f"wrote {args.file}")
    elif args.action == "bridge":
        print("headset mode: your mic -> call, call -> your speakers. Ctrl-C to stop.")
        audio.bridge()
    elif args.action == "mode":
        print(link.board(f"audio {args.mode}" if args.mode else "audio")[0])


# ---- parser ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="koppar-lte-modem", description="Koppar LTE modem: a phone line over USB-C")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--direct", action="store_true", help="don't go through a running daemon")
    p.add_argument("-v", "--verbose", action="count", default=0, help="log AT traffic (-vv)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="board, SIM, network, signal, calls").set_defaults(fn=cmd_status)
    sub.add_parser("info", help="model, firmware, IMEI, IMSI, ICCID, own number").set_defaults(fn=cmd_info)

    s = sub.add_parser("power", help="module power: on | off (graceful) | reset | restart")
    s.add_argument("action", choices=["on", "off", "reset", "restart"])
    s.add_argument("--no-wait", dest="wait", action="store_false")
    s.set_defaults(fn=cmd_power)

    s = sub.add_parser("setup", help="one-time module settings: PCM audio for the board, URC port, optional data")
    s.add_argument("--rate", type=int, choices=[8000, 16000], default=8000, help="call audio rate (match the firmware)")
    s.add_argument("--apn", help="also set up data (ECM network adapter) with this APN")
    s.add_argument("--autopower", dest="autopower", action="store_true", default=None, help="power the module on at plug-in")
    s.add_argument("--no-autopower", dest="autopower", action="store_false")
    s.add_argument("--no-restart", action="store_true", help="don't restart the module to apply")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("at", help="send an AT command, or open an AT console with no argument")
    s.add_argument("command", nargs="*")
    s.add_argument("-t", "--timeout", type=float, default=10.0)
    s.set_defaults(fn=cmd_at)

    s = sub.add_parser("sms", help="send, list, read, delete text messages")
    ss = s.add_subparsers(dest="action", required=True)
    x = ss.add_parser("send")
    x.add_argument("number")
    x.add_argument("text", nargs="+")
    x = ss.add_parser("list")
    x.add_argument("--unread", action="store_true")
    x = ss.add_parser("read")
    x.add_argument("index", type=int)
    x = ss.add_parser("delete")
    x.add_argument("index", help="message index, or 'all'")
    s.set_defaults(fn=cmd_sms)

    s = sub.add_parser("call", help="dial a number")
    s.add_argument("number")
    s.set_defaults(fn=cmd_call)
    sub.add_parser("answer", help="answer the ringing call").set_defaults(fn=cmd_answer)
    sub.add_parser("hangup", help="end the call").set_defaults(fn=cmd_hangup)
    sub.add_parser("calls", help="list current calls").set_defaults(fn=cmd_calls)
    s = sub.add_parser("dtmf", help="send DTMF digits during a call")
    s.add_argument("digits")
    s.set_defaults(fn=cmd_dtmf)

    s = sub.add_parser("data", help="LTE data as a USB network adapter (ECM)")
    ss = s.add_subparsers(dest="action", required=True)
    x = ss.add_parser("setup")
    x.add_argument("--apn", required=True)
    ss.add_parser("status")
    s.set_defaults(fn=cmd_data)

    s = sub.add_parser("audio", help="call audio: test | devices | play | record | bridge | mode")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("test", help="loopback and tone test of the sound card (no call needed)")
    ss.add_parser("devices")
    x = ss.add_parser("play", help="play a WAV into the call")
    x.add_argument("file")
    x = ss.add_parser("record", help="record the call to a WAV")
    x.add_argument("file")
    x.add_argument("-s", "--seconds", type=float, default=10.0)
    ss.add_parser("bridge", help="headset mode: your mic and speakers on the call")
    x = ss.add_parser("mode", help="firmware audio route: pcm | loopback | tone")
    x.add_argument("mode", nargs="?", choices=["pcm", "loopback", "tone"])
    s.set_defaults(fn=cmd_audio)

    s = sub.add_parser("flash", help="update the board firmware (.uf2) through its USB bootloader")
    s.add_argument("file", nargs="?", default="firmware/modem/build/koppar_modem.uf2")
    s.add_argument("--bootloader", action="store_true", help="use the ROM USB bootloader instead of the control port")
    s.set_defaults(fn=cmd_flash)

    s = sub.add_parser("board", help="raw control-port command, e.g. `koppar-lte-modem board status`")
    s.add_argument("command", nargs="+")
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("daemon", help="run the event daemon (owns the modem; other commands go through it)")
    s.add_argument("--on-event", action="append", metavar="CMD",
                   help="run CMD for each event, with KOPPAR_EVENT, KOPPAR_NUMBER, KOPPAR_TEXT, KOPPAR_JSON set")
    s.add_argument("--no-power-on", action="store_true", help="don't switch the module on")
    s.add_argument("--delete-sms", action="store_true", help="delete each incoming SMS after reporting it")
    s.add_argument("--pin", help="SIM PIN to enter if the SIM asks for one")
    s.add_argument("--ui-port", type=int, default=8025, help="port for the UI on 127.0.0.1 (default 8025)")
    s.add_argument("--no-ui", action="store_true", help="don't serve the UI")
    s.add_argument("--no-popup", action="store_true", help="no notification or window on incoming calls")
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("ui", help="open the UI of the running daemon in a browser")
    s.add_argument("--port", type=int, default=8025)
    s.add_argument("--print", action="store_true", help="print the URL (with its token) instead of opening it")
    s.set_defaults(fn=cmd_ui)

    sub.add_parser("events", help="print the daemon's events as they happen").set_defaults(fn=cmd_events)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * args.verbose, format="%(asctime)s %(name)s %(message)s")
    if args.fn in (cmd_daemon, cmd_events):
        return args.fn(args, None)
    link = Link.open(use_daemon=not args.direct)
    try:
        args.fn(args, link)
    except (ATError, BoardError, TransportError, ValueError) as e:
        sys.exit(f"error: {e}")
    finally:
        link.close()


if __name__ == "__main__":
    main()
