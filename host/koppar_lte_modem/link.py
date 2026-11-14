"""One way in for every command: through `koppar-lte-modem daemon` if it is running
(it owns the AT port and the board), or straight to the hardware if not."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

from .at import ATChannel, ATError, ATTimeout
from .board import Board, BoardError
from .modem import Modem
from .transport import open_at_transport


def socket_path() -> Path:
    return Path(os.environ.get("KOPPAR_SOCKET", "~/.koppar-lte-modem/daemon.sock")).expanduser()


class DaemonError(Exception):
    pass


class Link:
    """at(): AT commands. board(): control-port commands. modem(): high-level API."""

    @staticmethod
    def open(use_daemon: bool = True) -> "Link":
        if use_daemon:
            d = DaemonLink.connect()
            if d is not None:
                return d
        return DirectLink()

    def at(self, cmd: str, timeout: float = 5.0, data: str | None = None) -> list[str]:
        raise NotImplementedError

    def board(self, cmd: str, timeout: float = 3.0) -> list[str]:
        raise NotImplementedError

    def board_status(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.board("status"):
            for kv in line.split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    out[k] = v
        return out

    def modem(self) -> Modem:
        return Modem(self.at)

    @property
    def via(self) -> str:
        return "?"

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DirectLink(Link):
    def __init__(self):
        self._at: ATChannel | None = None
        self._board: Board | None = None

    @property
    def via(self) -> str:
        return "direct"

    def at(self, cmd, timeout=5.0, data=None):
        if self._at is None or not self._at.alive:
            self._at = ATChannel(open_at_transport())
            try:
                self._at.command("ATE0")
            except ATError:
                pass
        return self._at.command(cmd, timeout=timeout, data=data)

    def board(self, cmd, timeout=3.0):
        if self._board is None:
            self._board = Board()
        return self._board.command(cmd, timeout=timeout)

    def close(self):
        if self._at:
            self._at.close()
        if self._board:
            self._board.close()


class DaemonLink(Link):
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._file = sock.makefile("rwb")
        self._lock = threading.Lock()

    @property
    def via(self) -> str:
        return "daemon"

    @classmethod
    def connect(cls) -> "DaemonLink | None":
        path = socket_path()
        if not path.exists():
            return None
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(path))
        except OSError:
            s.close()
            return None
        return cls(s)

    def request(self, req: dict, timeout: float = 10.0) -> dict:
        with self._lock:
            self._sock.settimeout(timeout + 5)
            self._file.write(json.dumps(req).encode() + b"\n")
            self._file.flush()
            raw = self._file.readline()
        if not raw:
            raise DaemonError("daemon closed the connection")
        return json.loads(raw)

    def at(self, cmd, timeout=5.0, data=None):
        r = self.request({"op": "at", "cmd": cmd, "timeout": timeout, "data": data}, timeout)
        if r.get("ok"):
            return r["lines"]
        cls = ATTimeout if r.get("kind") == "timeout" else ATError
        raise cls(cmd, r.get("error", "failed"), r.get("lines"))

    def board(self, cmd, timeout=3.0):
        r = self.request({"op": "board", "cmd": cmd, "timeout": timeout}, timeout)
        if r.get("ok"):
            return r["lines"]
        raise BoardError(r.get("error", "failed"))

    def ping(self) -> dict:
        return self.request({"op": "ping"})

    def events(self):
        """Yield events from the daemon until the connection closes."""
        self._file.write(b'{"op": "subscribe"}\n')
        self._file.flush()
        self._sock.settimeout(None)
        for raw in self._file:
            yield json.loads(raw)

    def close(self):
        try:
            self._file.close()
            self._sock.close()
        except OSError:
            pass
