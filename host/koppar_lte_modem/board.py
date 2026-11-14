"""The RP2040's control port (CDC serial): module power, LEDs, audio mode.

Protocol (firmware/modem/src/control.h): one command per line; reply lines,
then "OK" or "ERR <reason>". Lines starting with "! " are events.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable

BOARD_VID = 0x1209
BOARD_PID = 0x0001


class BoardError(Exception):
    pass


def find_board_port() -> str | None:
    from serial.tools import list_ports

    env = os.environ.get("KOPPAR_BOARD_PORT")
    if env:
        return env
    for p in list_ports.comports():
        if p.vid == BOARD_VID and p.pid == BOARD_PID and "Koppar" in f"{p.manufacturer} {p.product}":
            return p.device
    for p in list_ports.comports():                 # product string missing on some hosts
        if p.vid == BOARD_VID and p.pid == BOARD_PID:
            return p.device
    return None


class Board:
    def __init__(self, port: str | None = None, on_event: Callable[[str], None] | None = None):
        import serial

        port = port or find_board_port()
        if not port:
            raise BoardError("Koppar board not found on USB (is the modem firmware flashed?)")
        self.port = port
        self.on_event = on_event
        self._s = serial.Serial(port, 115200, timeout=0.1)
        self._lock = threading.Lock()
        self._s.reset_input_buffer()

    def command(self, cmd: str, timeout: float = 3.0) -> list[str]:
        """Run one command; return its reply lines (without OK). Raises BoardError on ERR."""
        with self._lock:
            self._s.write((cmd.strip() + "\n").encode())
            self._s.flush()
            lines: list[str] = []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = self._s.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("! "):
                    self._event(line[2:])
                elif line == "OK":
                    return lines
                elif line.startswith("ERR"):
                    raise BoardError(f"{cmd}: {line[3:].strip()}")
                else:
                    lines.append(line)
            raise BoardError(f"{cmd}: no answer from the board")

    def poll_events(self, timeout: float = 0.0) -> None:
        """Read pending event lines (calls on_event for each)."""
        if not self._lock.acquire(timeout=0.05):
            return
        try:
            self._s.timeout = max(timeout, 0.01)
            while True:
                raw = self._s.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("! "):
                    self._event(line[2:])
                self._s.timeout = 0.01
        finally:
            self._s.timeout = 0.1
            self._lock.release()

    def status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.command("status"):
            for kv in line.split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    out[k] = v
        return out

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass

    def _event(self, ev: str) -> None:
        if self.on_event:
            self.on_event(ev)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
