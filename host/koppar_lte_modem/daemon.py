"""`koppar-lte-modem daemon`: owns the modem, turns its URCs into events, serves commands.

- Powers the module on (unless told not to) and waits for its AT port.
- Initialises the session (caller ID, SMS notifications) after every boot.
- Events: ready, module_on, module_off, network, ring, incoming_call,
  call_state, call_ended, sms, call_audio, modem_disconnected.
  Each goes to stdout, to `--on-event` hooks (as environment variables) and
  to `koppar-lte-modem events` subscribers.
- Other `koppar-lte-modem` commands talk to the hardware through its Unix socket, so a
  hook can run `koppar-lte-modem answer` while the daemon holds the AT port.
- LEDs: NET solid when registered, RING blinking while a call is ringing.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import socketserver
import subprocess
import threading
import time
from datetime import datetime, timezone

from .at import ATChannel, ATError, ATTimeout
from .board import Board, BoardError
from .link import socket_path
from .modem import Modem, split_fields
from .transport import TransportError, open_at_transport

log = logging.getLogger("koppar_lte_modem.daemon")


class Daemon:
    def __init__(self, hooks: list[str] | None = None, json_out: bool = False, power_on: bool = True,
                 delete_sms: bool = False, pin: str | None = None):
        self.hooks = hooks or []
        self.json_out = json_out
        self.power_on = power_on
        self.delete_sms = delete_sms
        self.pin = pin
        self.board: Board | None = None
        self.at: ATChannel | None = None
        self.modem: Modem | None = None
        self.urcs: queue.Queue[str] = queue.Queue()
        self.subscribers: list[queue.Queue] = []
        self.sub_lock = threading.Lock()
        self.stop = threading.Event()
        self.calls: dict[int, dict] = {}
        self.ringing_number: str | None = None
        self.announced_incoming = False
        self.network: dict | None = None
        self.server: socketserver.ThreadingUnixStreamServer | None = None

    # ---- events ----------------------------------------------------------

    def emit(self, event: str, **fields) -> None:
        ev = {"event": event, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
        if self.json_out:
            print(json.dumps(ev), flush=True)
        else:
            detail = " ".join(f"{k}={v!r}" for k, v in fields.items())
            print(f"{ev['time']}  {event:<18} {detail}", flush=True)
        with self.sub_lock:
            for q in list(self.subscribers):
                q.put(ev)
        for hook in self.hooks:
            env = dict(os.environ)
            env["KOPPAR_EVENT"] = event
            env["KOPPAR_JSON"] = json.dumps(ev)
            for k, v in fields.items():
                env[f"KOPPAR_{k.upper()}"] = "" if v is None else str(v)
            try:
                subprocess.Popen(hook, shell=True, env=env)
            except OSError as e:
                log.error("hook %r failed: %s", hook, e)

    # ---- hardware --------------------------------------------------------

    def _board_event(self, ev: str) -> None:
        if ev == "status on":
            self.emit("module_on")
        elif ev == "status off":
            self.emit("module_off")
        elif ev in ("pcm on", "pcm off"):
            self.emit("call_audio", active=ev == "pcm on")
        elif ev.startswith("error"):
            self.emit("board_error", message=ev)

    def _led(self, led: str, mode: str) -> None:
        try:
            if self.board:
                self.board.command(f"led {led} {mode}")
        except BoardError:
            pass

    def _open_board(self) -> None:
        while not self.stop.is_set():
            try:
                self.board = Board(on_event=self._board_event)
                log.info("board on %s", self.board.port)
                return
            except BoardError as e:
                log.warning("%s; retrying", e)
                self.stop.wait(3)

    def _ensure_module_on(self) -> None:
        st = self.board.status()
        if st.get("module") == "off" and self.power_on:
            log.info("powering the module on")
            self.board.command("power on")

    def _open_at(self) -> bool:
        deadline = time.monotonic() + 60
        while not self.stop.is_set():
            try:
                t = open_at_transport()
                self.at = ATChannel(t, on_urc=self.urcs.put)
                self.modem = Modem(self.at.command)
                # The port can enumerate before the AT parser is up.
                for _ in range(20):
                    try:
                        self.at.command("AT", timeout=1)
                        break
                    except ATTimeout:
                        continue
                return True
            except TransportError as e:
                if time.monotonic() > deadline:
                    log.warning("AT port: %s (still waiting)", e)
                    deadline = time.monotonic() + 60
                if self.board:
                    self.board.poll_events(0.5)
                self.stop.wait(2)
        return False

    def _start_session(self) -> None:
        m = self.modem
        m.init_session()
        sim = m.sim()
        if sim == "SIM PIN" and self.pin:
            m.enter_pin(self.pin)
            time.sleep(3)
            sim = m.sim()
        info = {}
        try:
            info = m.info()
        except ATError:
            pass
        self.emit("ready", sim=sim, imei=info.get("imei"), revision=info.get("revision"), via=self.at.transport.name)

    # ---- URCs and polling ------------------------------------------------

    def _handle_urc(self, line: str) -> None:
        if line == "! disconnected":
            return
        if line in ("RING",) or line.startswith("+CRING:"):
            self._led("ring", "fast")
            self.emit("ring", number=self.ringing_number)
            self._poll_calls()
        elif line.startswith("+CLIP:"):
            f = split_fields(line[6:])
            self.ringing_number = f[0] or None
            if not self.announced_incoming:
                self.announced_incoming = True
                self.emit("incoming_call", number=self.ringing_number)
        elif line.startswith("+CMTI:"):
            f = split_fields(line[6:])
            idx = int(f[1])
            try:
                msg = self.modem.read_sms(idx)
            except ATError as e:
                self.emit("sms", index=idx, error=e.result)
                return
            if msg:
                self.emit("sms", index=idx, number=msg.number, sent=msg.time, text=msg.text)
                if self.delete_sms:
                    try:
                        self.modem.delete_sms(idx)
                    except ATError:
                        pass
        elif line in ("NO CARRIER", "BUSY", "NO ANSWER"):
            self._poll_calls()
        elif line == "POWERED DOWN":
            self.emit("module_powering_down")
        elif line == "RDY":
            log.info("module reports RDY")

    def _poll_calls(self) -> None:
        try:
            now = {c.index: c.as_dict() for c in self.modem.calls()}
        except ATError:
            return
        for idx, c in now.items():
            old = self.calls.get(idx)
            if old is None or old["state"] != c["state"]:
                self.emit("call_state", **c)
                if c["state"] == "active":
                    self._led("ring", "auto")
        for idx, c in self.calls.items():
            if idx not in now:
                self.emit("call_ended", index=idx, number=c["number"], direction=c["direction"])
        if not now and (self.calls or self.announced_incoming):
            self._led("ring", "auto")
            self.ringing_number = None
            self.announced_incoming = False
        self.calls = now

    def _poll_network(self) -> None:
        try:
            reg = self.modem.registration()
            sig = self.modem.signal()
        except ATError:
            return
        key = (reg.get("state"), reg.get("operator"), reg.get("tech"))
        if self.network is None or key != (self.network.get("state"), self.network.get("operator"), self.network.get("tech")):
            self.emit("network", state=reg.get("state"), operator=reg.get("operator"), tech=reg.get("tech"),
                      band=reg.get("band"), rssi_dbm=sig.get("rssi_dbm"))
            self._led("net", "on" if reg.get("registered") else "auto")
        self.network = reg

    # ---- socket server ---------------------------------------------------

    def _serve(self) -> None:
        daemon = self
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    try:
                        req = json.loads(raw)
                    except ValueError:
                        self._reply({"ok": False, "error": "bad json"})
                        continue
                    if req.get("op") == "subscribe":
                        daemon._stream(self)
                        return
                    self._reply(daemon._request(req))

            def _reply(self, obj):
                self.wfile.write(json.dumps(obj).encode() + b"\n")
                self.wfile.flush()

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(path), Handler)
        os.chmod(path, 0o600)
        self.server.serve_forever()

    def _stream(self, handler) -> None:
        q: queue.Queue = queue.Queue()
        with self.sub_lock:
            self.subscribers.append(q)
        try:
            while not self.stop.is_set():
                try:
                    ev = q.get(timeout=1)
                except queue.Empty:
                    continue
                handler.wfile.write(json.dumps(ev).encode() + b"\n")
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.sub_lock:
                self.subscribers.remove(q)

    def _request(self, req: dict) -> dict:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "at": bool(self.at and self.at.alive), "board": self.board is not None,
                    "network": self.network, "calls": list(self.calls.values())}
        if op == "at":
            if not (self.at and self.at.alive):
                return {"ok": False, "error": "modem not connected (yet)"}
            try:
                lines = self.at.command(req["cmd"], timeout=float(req.get("timeout", 5)), data=req.get("data"))
                return {"ok": True, "lines": lines}
            except ATTimeout as e:
                return {"ok": False, "kind": "timeout", "error": e.result, "lines": e.lines}
            except ATError as e:
                return {"ok": False, "error": e.result, "lines": e.lines}
        if op == "board":
            if not self.board:
                return {"ok": False, "error": "board not connected"}
            try:
                return {"ok": True, "lines": self.board.command(req["cmd"], timeout=float(req.get("timeout", 3)))}
            except BoardError as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # ---- main loop -------------------------------------------------------

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *a: self.stop.set())
        threading.Thread(target=self._serve, name="koppar-socket", daemon=True).start()
        try:
            self._open_board()
            while not self.stop.is_set():
                self._ensure_module_on()
                if not self._open_at():
                    break
                self._start_session()
                next_net = next_calls = 0.0
                while not self.stop.is_set() and self.at.alive:
                    try:
                        line = self.urcs.get(timeout=0.25)
                        self._handle_urc(line)
                    except queue.Empty:
                        pass
                    self.board.poll_events(0)
                    now = time.monotonic()
                    if now >= next_calls:
                        self._poll_calls()
                        next_calls = now + (1 if (self.calls or self.announced_incoming) else 3)
                    if now >= next_net:
                        self._poll_network()
                        next_net = now + 15
                if not self.stop.is_set():
                    self.emit("modem_disconnected")
                    self.calls, self.network = {}, None
                    self._led("net", "auto")
                    self._led("ring", "auto")
                    self.at.close()
                    self.stop.wait(2)
        except KeyboardInterrupt:
            pass
        finally:
            self._led("net", "auto")
            self._led("ring", "auto")
            if self.server:
                self.server.shutdown()
            try:
                socket_path().unlink()
            except OSError:
                pass
            if self.at:
                self.at.close()
            if self.board:
                self.board.close()
