"""AT command channel: one command at a time, URCs delivered to a callback.

A reader thread splits the module's output into lines. While a command is
pending, its response lines are collected until a final result code; lines
that cannot belong to it (RING, +CLIP:, +CMTI:, ...) are passed to the URC
callback, whenever they arrive.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .transport import Transport, TransportError

log = logging.getLogger("koppar_lte_modem.at")


class ATError(Exception):
    def __init__(self, command: str, result: str, lines: list[str] | None = None):
        super().__init__(f"{command}: {result}")
        self.command = command
        self.result = result
        self.lines = lines or []


class ATTimeout(ATError):
    pass


class ATClosed(ATError):
    pass


# Final result codes for any command.
_ERROR_PREFIXES = ("+CME ERROR:", "+CMS ERROR:")
# Extra final result codes for dial / answer.
_CALL_FINALS = {"NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE", "CONNECT"}
# Lines that are always URCs, unless the pending command asked for them.
_URC_PREFIXES = (
    "+CRING:", "+CLIP:", "+CMTI:", "+CMT:", "+CDSI:", "+CDS:", "+QIND:", "+CREG:", "+CEREG:",
    "+CGREG:", "+CCWA:", "+CUSD:", "+QUSIM:", "+CPIN:", "+CFUN:", "+QSTAT:", "+QTONEDET:",
    "+QAUDRD:", "+CTZV:", "+QNITZ:", "+CTZE:",
)
_URC_EXACT = {"RING", "RDY", "POWERED DOWN", "NO CARRIER", "BUSY", "NO ANSWER"}


def _owns(command: str, line: str) -> bool:
    """True if `line` is the information response of `command` (AT+CREG? owns +CREG: ...)."""
    if not line.startswith("+") or ":" not in line:
        return False
    name = line.split(":", 1)[0].upper()
    cmd = command.upper()
    return cmd.startswith("AT" + name)


class ATChannel:
    def __init__(self, transport: Transport, on_urc: Callable[[str], None] | None = None):
        self.transport = transport
        self.on_urc = on_urc
        self._lock = threading.Lock()          # one command at a time
        self._cv = threading.Condition()
        self._pending: str | None = None
        self._lines: list[str] = []
        self._final: str | None = None
        self._prompt = False
        self._alive = True
        self._expect_disconnect = False        # set by AT+QPOWD: the module is about to leave the bus
        self._buf = b""
        self._reader = threading.Thread(target=self._read_loop, name="koppar-at-reader", daemon=True)
        self._reader.start()

    # ---- public ----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive

    def command(self, cmd: str, timeout: float = 5.0, data: str | None = None) -> list[str]:
        """Send `cmd`; return its response lines (without the final OK).

        With `data`, wait for the "> " prompt, then send the data and Ctrl-Z
        (AT+CMGS and friends). Raises ATError on ERROR / +CME / +CMS errors
        and on call results like NO CARRIER; ATTimeout if nothing final arrives.
        """
        with self._lock:
            if not self._alive:
                raise ATClosed(cmd, "AT port closed")
            with self._cv:
                self._pending, self._lines, self._final, self._prompt = cmd, [], None, False
            log.debug("> %s", cmd)
            if cmd.upper().startswith("AT+QPOWD"):
                self._expect_disconnect = True
            try:
                self.transport.write(cmd.encode("ascii", "replace") + b"\r")
                deadline = time.monotonic() + timeout
                if data is not None:
                    with self._cv:
                        while not self._prompt and self._final is None and self._alive:
                            if not self._cv.wait(max(0.0, deadline - time.monotonic())):
                                break
                        got_prompt = self._prompt
                    if got_prompt:
                        log.debug("> [data %d bytes]", len(data))
                        self.transport.write(data.encode("ascii", "replace") + b"\x1a")
                    elif self._final is None:
                        self.transport.write(b"\x1b")           # cancel the prompt
                        raise ATTimeout(cmd, "no '>' prompt")
                with self._cv:
                    while self._final is None and self._alive:
                        left = deadline - time.monotonic()
                        if left <= 0 or not self._cv.wait(left):
                            if self._final is None:
                                raise ATTimeout(cmd, f"no answer within {timeout:g} s", list(self._lines))
                    if self._final is None:
                        raise ATClosed(cmd, "AT port closed", list(self._lines))
                    final, lines = self._final, list(self._lines)
            except TransportError as e:
                self._alive = False
                raise ATClosed(cmd, str(e)) from e
            finally:
                with self._cv:
                    self._pending = None
            if final == "OK":
                return lines
            raise ATError(cmd, final, lines)

    def query(self, cmd: str, prefix: str | None = None, timeout: float = 5.0) -> str | None:
        """First response line's value: query("AT+CSQ") -> "17,99"."""
        for line in self.command(cmd, timeout):
            if prefix is None and not line.startswith("+"):
                return line
            p = prefix or line.split(":", 1)[0]
            if line.startswith(p + ":"):
                return line[len(p) + 1:].strip()
        return None

    def close(self) -> None:
        self._alive = False
        with self._cv:
            self._cv.notify_all()
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- reader ----------------------------------------------------------

    def _read_loop(self) -> None:
        while self._alive:
            try:
                data = self.transport.read(0.2)
            except TransportError as e:
                if not self._alive:             # close() released the port under the read
                    return
                if self._expect_disconnect:
                    log.debug("AT port gone after power-down: %s", e)
                else:
                    log.warning("AT port lost: %s", e)
                self._alive = False
                with self._cv:
                    self._cv.notify_all()
                self._urc("! disconnected")
                return
            except Exception:                   # transport closed under us
                if not self._alive:
                    return
                raise
            if data:
                self._feed(data)

    def _feed(self, data: bytes) -> None:
        self._buf += data
        while True:
            # The SMS prompt has no line ending.
            stripped = self._buf.lstrip(b"\r\n")
            if stripped.startswith(b"> ") or stripped == b">":
                with self._cv:
                    if self._pending is not None:
                        self._prompt = True
                        self._cv.notify_all()
                self._buf = stripped[2:] if stripped.startswith(b"> ") else b""
                continue
            idx = min((i for i in (self._buf.find(b"\r"), self._buf.find(b"\n")) if i >= 0), default=-1)
            if idx < 0:
                return
            raw, self._buf = self._buf[:idx], self._buf[idx + 1:]
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._line(line)

    def _line(self, line: str) -> None:
        log.debug("< %s", line)
        with self._cv:
            cmd = self._pending
            if cmd is not None:
                if line == cmd.strip():                    # echo
                    return
                up = cmd.upper()
                is_call = up.startswith("ATD") or up.startswith("ATA")
                if line == "OK" or line == "ERROR" or line.startswith(_ERROR_PREFIXES) or (is_call and line in _CALL_FINALS):
                    self._final = line
                    self._cv.notify_all()
                    return
                if not (line in _URC_EXACT or line.startswith(_URC_PREFIXES)) or _owns(cmd, line):
                    self._lines.append(line)
                    return
        self._urc(line)

    def _urc(self, line: str) -> None:
        if self.on_urc:
            try:
                self.on_urc(line)
            except Exception:
                log.exception("URC handler failed on %r", line)
