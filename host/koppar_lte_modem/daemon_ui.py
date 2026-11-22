"""What the daemon does for its UI: history, the call pop-up, automations,
and the actions behind the UI's buttons. Mixed into Daemon.

Everything the daemon emits is stored and pushed to open UI windows; AT
traffic is logged with where it came from (poll, daemon, cli, ui); texts
are archived on the computer; calls are logged.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .at import ATError
from .modem import split_fields, ucs2_decode
from .store import Store, data_dir
from . import web

log = logging.getLogger("koppar_lte_modem.daemon")

PHONE = re.compile(r"^\+?[0-9]{6,15}$")

# AT+QENG="servingcell" fields after the state, by radio.
SERVING_FIELDS = {
    "LTE": ["state", "rat", "duplex", "mcc", "mnc", "cell_id", "pci", "earfcn", "band", "ul_bw", "dl_bw", "tac",
            "rsrp", "rsrq", "rssi", "sinr", "srxlev"],
    "WCDMA": ["state", "rat", "mcc", "mnc", "lac", "cell_id", "uarfcn", "psc", "rac", "rscp", "ecio", "phych",
              "sf", "slot", "speech_code", "com_mod"],
    "GSM": ["state", "rat", "mcc", "mnc", "lac", "cell_id", "bsic", "arfcn", "band", "rxlev", "txp", "rla",
            "drx", "c1", "c2", "gprs", "tch", "ts", "ta", "maio", "hsn", "rxlevsub", "rxlevfull", "rxqualsub",
            "rxqualfull", "voicecodec"],
}
NEIGHBOUR_FIELDS = {"LTE": ["kind", "rat", "earfcn", "pci", "rsrq", "rsrp", "rssi", "sinr", "srxlev"],
                    "WCDMA": ["kind", "rat", "uarfcn", "cell_resel_priority", "thresh_high", "thresh_low", "psc",
                              "rscp", "ecno", "srxlev"]}
COPS_STAT = {"0": "unknown", "1": "available", "2": "current", "3": "forbidden"}
COPS_ACT = {"0": "GSM", "2": "UTRAN", "3": "GSM/EGPRS", "4": "HSDPA", "5": "HSUPA", "6": "HSPA", "7": "LTE"}


class _Recorder:
    """Both sides of a bridged call, mixed to one mono WAV when it stops."""

    def __init__(self, path: Path, rate: int):
        self.path, self.rate = path, rate
        self.up: list = []
        self.down: list = []
        self.lock = threading.Lock()

    def tap(self, direction: str, samples) -> None:
        with self.lock:
            (self.up if direction == "up" else self.down).append(samples)

    def save(self) -> str | None:
        import numpy as np
        from .audio import write_wav

        with self.lock:
            up = np.concatenate(self.up) if self.up else np.zeros(0, np.float32)
            down = np.concatenate(self.down) if self.down else np.zeros(0, np.float32)
        n = max(len(up), len(down))
        if n == 0:
            return None
        mix = np.zeros(n, np.float32)
        mix[:len(up)] += up * 0.5
        mix[:len(down)] += down * 0.5
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(str(self.path), mix, self.rate)
        return str(self.path)


class UIMixin:
    # ---- set-up ------------------------------------------------------------

    def _ui_init(self, ui_port: int | None, popup: bool, store: Store | None) -> None:
        self.store = store or Store()
        self.ui_port = ui_port
        self.popup = popup
        self.ui_subscribers: list = []
        self.ui_server = None
        self.ui_token = self.store.ui_token()
        self._tl = threading.local()
        self.sim_state: str | None = None
        self.module_info: dict = {}
        self.last_signal: dict | None = None
        self.call_id: int | None = None
        self.call_answered = False
        self._no_mac_audio = False           # "answer without the Mac's audio" for this call
        self._bridge_stop: threading.Event | None = None
        self._bridge_thread: threading.Thread | None = None
        self._recorder: _Recorder | None = None
        self._replied: dict[str, float] = {}
        self._next_scheduled = 0.0

    def _start_ui(self) -> None:
        if not self.ui_port:
            return
        try:
            self.ui_server = web.serve(self, self.ui_port, self.ui_token)
            log.info("UI on %s", web.ui_url(self.ui_port, "<token>"))
            print(f"UI on http://127.0.0.1:{self.ui_port}/  (open it with: koppar-lte-modem ui)", flush=True)
        except OSError as e:
            log.error("UI not started (port %s): %s", self.ui_port, e)

    # ---- AT with a log -----------------------------------------------------

    def at_logged(self, cmd: str, timeout: float = 5.0, data: str | None = None, source: str | None = None):
        if not (self.at and self.at.alive):
            raise ATError(cmd, "modem not connected (yet)")
        source = source or getattr(self._tl, "source", "daemon")
        t0 = time.monotonic()
        try:
            lines = self.at.command(cmd, timeout=timeout, data=data)
            self._log_at(source, cmd, lines, True, None, t0)
            return lines
        except ATError as e:
            self._log_at(source, cmd, e.lines, False, e.result, t0)
            raise

    def _log_at(self, source, cmd, lines, ok, error, t0) -> None:
        ms = int((time.monotonic() - t0) * 1000)
        try:
            rid = self.store.add_at(source, cmd, lines, ok, error, ms)
        except Exception as e:                       # never let logging break a command
            log.warning("AT log: %s", e)
            return
        if source != "poll":
            self.push("at", id=rid, source=source, cmd=cmd, lines=lines, ok=ok, error=error, ms=ms)

    def _modem_at(self, cmd, timeout=5.0, data=None):
        return self.at_logged(cmd, timeout, data)

    def _as(self, source: str, fn, *args):
        prev = getattr(self._tl, "source", None)
        self._tl.source = source
        try:
            return fn(*args)
        finally:
            self._tl.source = prev

    def modem_call(self, fn):
        if not (self.at and self.at.alive and self.modem):
            raise ATError("", "modem not connected (yet)")
        return self._as("ui", fn, self.modem)

    # ---- events to the UI, the archive and the automations -------------------

    def push(self, kind: str, **data) -> None:
        msg = {"event": kind, "ui": True, **data}
        with self.sub_lock:
            for q in list(self.ui_subscribers):
                q.put(msg)

    def _on_event(self, ev: dict) -> None:
        try:
            ev["id"] = self.store.add_event(ev)
        except Exception as e:
            log.warning("event store: %s", e)
        with self.sub_lock:
            for q in list(self.ui_subscribers):
                q.put(ev)
        settings = self.store.settings()
        if settings.get("webhook_url"):
            threading.Thread(target=self._webhook, args=(settings["webhook_url"], ev), daemon=True).start()
        kind = ev["event"]
        try:
            if kind == "sms" and ev.get("text") is not None:
                self._on_sms(ev, settings)
            elif kind == "incoming_call":
                self._call_begin("in", ev.get("number"))
                if self.popup and settings.get("popup_on_call"):
                    self._popup(ev.get("number"))
            elif kind == "call_state":
                if self.call_id is None:
                    self._call_begin(ev.get("direction", "out"), ev.get("number"))
                if ev.get("state") == "active" and not self.call_answered:
                    self.call_answered = True
                    self.store.call_update(self.call_id, answered=ev["time"])
            elif kind == "call_audio":
                # The module's call audio is flowing (from the first ringback tone on):
                # put it on the Mac's microphone and speakers, like a phone would.
                if ev.get("active") and settings.get("mac_audio") and not self._no_mac_audio:
                    self.ui_bridge(True)
            elif kind == "call_ended":
                self._call_end(ev["time"])
            elif kind in ("modem_disconnected", "module_off"):
                self._call_end(ev["time"])
        except Exception as e:
            log.exception("UI handling of %s: %s", kind, e)

    def _webhook(self, url: str, ev: dict) -> None:
        try:
            req = urllib.request.Request(url, data=json.dumps(ev, default=str).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "koppar-lte-modem"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            log.warning("webhook %s: %s", url, e)

    # ---- texts -----------------------------------------------------------------

    def _on_sms(self, ev: dict, settings: dict) -> None:
        number, text = ev.get("number") or "", ev["text"]
        mid = self.store.add_message("in", number, text, ev.get("sent"), ev.get("parts"), "REC UNREAD")
        if mid is None:                              # reported before: already archived and handled
            return
        self.push("message", id=mid, number=number, name=self.store.contact_name(number), text=text)
        if settings.get("archive_delete_from_module") and not self.delete_sms:
            for i in ev.get("parts") or []:
                try:
                    self.modem.delete_sms(i)
                except ATError:
                    pass
        fwd = (settings.get("forward_sms_to") or "").strip()
        if fwd and number != fwd:
            who = self.store.contact_name(number) or number
            self._send_auto(fwd, f"From {who}: {text}", "forward")
        ar = settings.get("auto_reply") or {}
        if ar.get("enabled") and ar.get("text") and PHONE.match(number) and number != fwd:
            last = self._replied.get(number, 0)
            if time.time() - last >= float(ar.get("hours", 6)) * 3600:
                self._replied[number] = time.time()
                self._send_auto(number, ar["text"], "auto-reply")

    def _send_auto(self, number: str, text: str, why: str) -> None:
        try:
            refs = self._as("daemon", self.modem.send_sms, number, text)
            self.store.add_message("out", number, text, status=why)
            self.emit("sms_sent", number=number, text=text, parts=len(refs), by=why)
        except ATError as e:
            self.emit("sms_failed", number=number, error=e.result, by=why)

    def ui_send_sms(self, number: str, text: str) -> dict:
        if not PHONE.match(number):
            raise web.ApiError(400, f"not a phone number: {number}")
        refs = self.modem_call(lambda m: m.send_sms(number, text))
        mid = self.store.add_message("out", number, text, status="sent")
        self.emit("sms_sent", number=number, text=text, parts=len(refs), by="ui")
        return {"id": mid, "references": refs}

    def ui_sync_messages(self, delete: bool = False) -> dict:
        msgs = self.modem_call(lambda m: m.list_sms())
        imported = deleted = 0
        for s in msgs:
            direction = "in" if s.status.startswith("REC") else "out"
            if self.store.add_message(direction, s.number, s.text, s.time or None, s.parts, s.status):
                imported += 1
            if delete:
                for i in s.parts:
                    self.modem_call(lambda m, i=i: m.delete_sms(i))
                    deleted += 1
        return {"on_module": len(msgs), "imported": imported, "deleted": deleted}

    def _run_scheduled(self) -> None:
        if time.monotonic() < self._next_scheduled or not (self.at and self.at.alive):
            return
        self._next_scheduled = time.monotonic() + 5
        for s in self.store.due():
            try:
                refs = self._as("daemon", self.modem.send_sms, s["number"], s["text"])
                self.store.scheduled_done(s["id"], "sent", f"{len(refs)} part(s)")
                self.store.add_message("out", s["number"], s["text"], status="scheduled")
                self.emit("sms_sent", number=s["number"], text=s["text"], parts=len(refs), by="schedule")
            except ATError as e:
                self.store.scheduled_done(s["id"], "failed", e.result)
                self.emit("sms_failed", number=s["number"], error=e.result, by="schedule")

    # ---- calls -----------------------------------------------------------------

    def _call_begin(self, direction: str, number: str | None) -> None:
        if self.call_id is None:
            self.call_id = self.store.call_started(direction, number)
            self.call_answered = False
        elif number:
            self.store.call_update(self.call_id, number=number)

    def _call_end(self, when: str) -> None:
        rec = self._stop_bridge()
        if self.call_id is not None:
            fields = {"ended": when}
            if rec:
                fields["recording"] = rec
            self.store.call_update(self.call_id, **fields)
            self.call_id = None
            self.call_answered = False
        self._no_mac_audio = False

    def ui_dial(self, number: str) -> None:
        if not re.fullmatch(r"\+?[0-9*#]{2,20}", number):
            raise web.ApiError(400, f"not a number to dial: {number}")
        self.modem_call(lambda m: m.dial(number))
        self._call_begin("out", number)

    def ui_answer(self, audio: bool = True) -> None:
        self._no_mac_audio = not audio
        self.modem_call(lambda m: m.answer())
        if audio:
            self.ui_bridge(True)

    def ui_hangup(self) -> None:
        self.modem_call(lambda m: m.hangup())
        self._stop_bridge()

    def ui_bridge(self, on: bool, record: bool = False) -> dict:
        if not on:
            rec = self._stop_bridge()
            if rec and self.call_id is not None:
                self.store.call_update(self.call_id, recording=rec)
            return {"bridge": False, "recording": rec}
        if self._bridge_thread and self._bridge_thread.is_alive():
            if record and self._recorder is None:
                self._recorder = self._new_recorder()
            return {"bridge": True, "recording": self._recorder is not None}
        from . import audio
        try:
            _, _, rate = audio.find_device()
        except Exception as e:
            raise web.ApiError(503, f"call audio device not found: {e}")
        self._recorder = self._new_recorder(rate) if record else None
        self._bridge_stop = threading.Event()

        def run():
            try:
                audio.bridge(self._bridge_stop, tap=lambda d, x: self._recorder and self._recorder.tap(d, x))
            except Exception as e:
                log.error("audio bridge: %s", e)
                self.push("bridge", on=False, error=str(e))
        self._bridge_thread = threading.Thread(target=run, name="koppar-bridge", daemon=True)
        self._bridge_thread.start()
        self.push("bridge", on=True, recording=record)
        return {"bridge": True, "recording": record}

    def _new_recorder(self, rate: int | None = None) -> _Recorder:
        if rate is None:
            from . import audio
            rate = audio.find_device()[2]
        name = time.strftime("%Y%m%d-%H%M%S") + (f"-call{self.call_id}" if self.call_id else "") + ".wav"
        return _Recorder(data_dir() / "recordings" / name, rate)

    def _stop_bridge(self) -> str | None:
        rec = None
        if self._bridge_stop:
            self._bridge_stop.set()
            if self._bridge_thread:
                self._bridge_thread.join(timeout=3)
            self._bridge_stop = self._bridge_thread = None
            self.push("bridge", on=False)
        if self._recorder:
            try:
                rec = self._recorder.save()
            except Exception as e:
                log.error("saving recording: %s", e)
            self._recorder = None
        return rec

    # ---- the pop-up --------------------------------------------------------------

    def _popup(self, number: str | None) -> None:
        who = self.store.contact_name(number) or number or "unknown number"
        notify(f"Incoming call", who)
        with self.sub_lock:
            open_windows = len(self.ui_subscribers)
        if not open_windows and self.ui_server:
            open_ui(web.ui_url(self.ui_port, self.ui_token, "call"), small=True)

    # ---- network ----------------------------------------------------------------

    def _record_signal(self, reg: dict, sig: dict) -> None:
        s = {"tech": reg.get("tech"), "operator": reg.get("operator"), "band": reg.get("band"),
             "state": reg.get("state"), "rssi": sig.get("rssi_dbm"), "rsrp": sig.get("rsrp"),
             "rsrq": sig.get("rsrq"), "sinr": sig.get("sinr"), "bars": sig.get("bars", 0)}
        self.last_signal = s
        try:
            self.store.add_signal(s)
        except Exception as e:
            log.warning("signal store: %s", e)
        self.push("signal", **s)

    def ui_cell(self) -> dict:
        lines = self.modem_call(lambda m: m.at('AT+QENG="servingcell"', timeout=5))
        try:
            lines += self.modem_call(lambda m: m.at('AT+QENG="neighbourcell"', timeout=5))
        except ATError:
            pass
        serving, neighbours = None, []
        for l in lines:
            if not l.startswith("+QENG:"):
                continue
            f = split_fields(l[6:])
            if f[0] == "servingcell":
                rat = f[2] if len(f) > 2 else ""
                serving = dict(zip(SERVING_FIELDS.get(rat, []), f[1:])) or {"raw": f[1:]}
            elif f[0].startswith("neighbourcell"):
                rat = f[1] if len(f) > 1 else ""
                names = NEIGHBOUR_FIELDS.get(rat)
                neighbours.append(dict(zip(names, f)) if names else {"kind": f[0], "raw": f[1:]})
        return {"serving": serving, "neighbours": neighbours, "raw": lines}

    def ui_scan(self) -> list[dict]:
        lines = self.modem_call(lambda m: m.at("AT+COPS=?", timeout=200))
        out = []
        for l in lines:
            if l.startswith("+COPS:"):
                for t in re.findall(r"\(([^()]*)\)", l):
                    f = split_fields(t)
                    if len(f) >= 5 and f[0] in COPS_STAT:
                        out.append({"status": COPS_STAT[f[0]], "name": f[1], "short": f[2], "plmn": f[3],
                                    "tech": COPS_ACT.get(f[4], f[4])})
        return out

    def ui_ussd(self, code: str) -> None:
        if not re.fullmatch(r"[0-9*#]{2,30}", code):
            raise web.ApiError(400, "a USSD code is digits, * and #, like *#100#")
        self.modem_call(lambda m: m.at(f'AT+CUSD=1,"{code}",15', timeout=15))
        self.emit("ussd_sent", code=code)

    def _on_cusd(self, line: str) -> None:
        f = split_fields(line[6:])
        text = f[1] if len(f) > 1 else ""
        dcs = int(f[2]) if len(f) > 2 and f[2].isdigit() else 15
        if dcs in (72, 0x48) or (dcs & 0x0C) == 0x08:
            text = ucs2_decode(text)
        self.emit("ussd", status={"0": "done", "1": "reply needed", "2": "ended by network"}.get(f[0], f[0]),
                  text=text, dcs=dcs)

    # ---- device and state -------------------------------------------------------

    def ui_board(self, cmd: str) -> list[str]:
        if not self.board:
            raise web.ApiError(503, "board not connected")
        return self.board.command(cmd)

    def ui_device(self) -> dict:
        out: dict = {"info": self.module_info, "sim": self.sim_state}
        if self.board:
            try:
                out["board"] = self.board.status()
            except Exception as e:
                out["board_error"] = str(e)
        if self.at and self.at.alive:
            for key, cmd, prefix in (("temperature", "AT+QTEMP", "+QTEMP"), ("supply", "AT+CBC", "+CBC"),
                                     ("network_info", "AT+QNWINFO", "+QNWINFO")):
                try:
                    v = self.modem_call(lambda m, c=cmd, p=prefix: m._value(c, p))
                    out[key] = v
                except ATError:
                    pass
            if out.get("supply"):
                try:
                    out["supply_v"] = int(split_fields(out["supply"])[2]) / 1000
                except (IndexError, ValueError):
                    pass
        return out

    def ui_state(self) -> dict:
        with self.sub_lock:
            windows = len(self.ui_subscribers)
        bridged = bool(self._bridge_thread and self._bridge_thread.is_alive())
        return {
            "modem": bool(self.at and self.at.alive),
            "board": self.board is not None,
            "sim": self.sim_state,
            "info": self.module_info,
            "network": self.network,
            "signal": self.last_signal,
            "calls": [dict(c, name=self.store.contact_name(c.get("number"))) for c in self.calls.values()],
            "unread": sum(t["unread"] for t in self.store.threads()),
            "ringing": self.announced_incoming,
            "ringing_number": self.ringing_number,
            "ringing_name": self.store.contact_name(self.ringing_number),
            "call_id": self.call_id,
            "bridge": bridged,
            "recording": self._recorder is not None,
            "windows": windows,
            "settings": self.store.settings(),
        }


# ---- desktop helpers ---------------------------------------------------------------

def notify(title: str, body: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e", "on run argv", "-e",
                              "display notification (item 2 of argv) with title (item 1 of argv) sound name \"Glass\"",
                              "-e", "end run", title, body])
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, body])
    except OSError as e:
        log.warning("notification: %s", e)


def open_ui(url: str, small: bool = False) -> None:
    """Open the UI; as a small app window when Chrome is there (the call pop-up)."""
    try:
        chrome = Path("/Applications/Google Chrome.app")
        if sys.platform == "darwin" and chrome.exists():
            args = ["open", "-na", str(chrome), "--args", f"--app={url}"]
            if small:
                args.append("--window-size=440,720")
            subprocess.Popen(args)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url])
    except OSError as e:
        log.warning("opening the UI: %s", e)
