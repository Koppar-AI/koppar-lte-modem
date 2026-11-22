"""The daemon's UI: a local web page and its JSON API, on 127.0.0.1 only.

Every /api request needs either the master token from the data folder
(header X-Koppar-Token: this user's programs, the CLI and the app) or a UI
session (header X-Koppar-Session). Neither ever appears in a URL: a window
is opened with a one-time launch code (valid 60 s) that the page swaps for a
session it keeps in that tab only. The Host header must name this server
(no DNS rebinding). A web page from anywhere else can reach 127.0.0.1 but
can't read any of these, so it can't send texts or place calls through it.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .at import ATError

log = logging.getLogger("koppar_lte_modem.web")

UI_DIR = Path(__file__).parent / "ui"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
DEFAULT_PORT = 8025
MAX_BODY = 1 << 20                 # 1 MB: nothing the UI sends comes close
LAUNCH_SECONDS = 60
_launch: dict[str, float] = {}     # one-time launch codes -> expiry
_launch_lock = threading.Lock()


def new_launch_code() -> str:
    code = secrets.token_urlsafe(32)
    now = time.time()
    with _launch_lock:
        for c, exp in list(_launch.items()):
            if exp < now:
                del _launch[c]
        _launch[code] = now + LAUNCH_SECONDS
    return code


def take_launch_code(code: str) -> bool:
    with _launch_lock:
        exp = _launch.pop(code, None)
    return exp is not None and exp >= time.time()


class FileResponse:
    def __init__(self, path, ctype: str = "audio/wav"):
        self.path, self.ctype = path, ctype


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


ROUTES: list[tuple[str, re.Pattern, object]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def _need(body: dict, *keys: str) -> list:
    missing = [k for k in keys if body.get(k) in (None, "")]
    if missing:
        raise ApiError(400, "missing " + ", ".join(missing))
    return [body[k] for k in keys]


# ---- sessions ---------------------------------------------------------------------

@route("POST", "/api/launch")
def _new_launch(d, m, q, b):
    """Master token only (checked in _dispatch): a one-time code to open a window with."""
    return {"launch": new_launch_code(), "expires_in": LAUNCH_SECONDS}


# ---- state and history --------------------------------------------------------

@route("GET", "/api/state")
def _state(d, m, q, b):
    return d.ui_state()


@route("GET", "/api/events")
def _events(d, m, q, b):
    return d.store.events(after=int(q.get("after", 0)), before=int(q["before"]) if q.get("before") else None,
                          limit=min(int(q.get("limit", 200)), 2000), kind=q.get("kind") or None)


@route("GET", "/api/atlog")
def _atlog(d, m, q, b):
    return d.store.at_log(after=int(q.get("after", 0)), hide_poll=q.get("poll") != "1")


@route("GET", "/api/signal")
def _signal(d, m, q, b):
    return d.store.signal(hours=float(q.get("hours", 24)))


@route("GET", "/api/export/events")
def _export(d, m, q, b):
    return d.store.events(limit=1_000_000)


# ---- messages -----------------------------------------------------------------------

@route("GET", "/api/threads")
def _threads(d, m, q, b):
    return d.store.threads()


@route("GET", "/api/messages")
def _messages(d, m, q, b):
    number = q.get("number")
    msgs = d.store.messages(number=number, starred=q.get("starred") == "1", query=q.get("q") or None)
    if number and any(not m["read"] for m in msgs):
        d.store.mark_read(number)
        d.push("read", number=number)            # menu bar and other windows update their counts
    return msgs


@route("POST", "/api/messages/send")
def _send(d, m, q, b):
    number, text = _need(b, "number", "text")
    return d.ui_send_sms(number.strip(), text)


@route("POST", "/api/messages/sync")
def _sync(d, m, q, b):
    return d.ui_sync_messages(delete=bool(b.get("delete")))


@route("POST", r"/api/messages/(\d+)/star")
def _star(d, m, q, b):
    d.store.star(int(m[1]), bool(b.get("on", True)))
    return {"ok": True}


@route("DELETE", r"/api/messages/(\d+)")
def _delete_msg(d, m, q, b):
    d.store.delete_message(int(m[1]))
    return {"ok": True}


@route("GET", "/api/module-messages")
def _module_messages(d, m, q, b):
    return [s.as_dict() for s in d.modem_call(lambda mo: mo.list_sms())]


@route("GET", "/api/scheduled")
def _scheduled(d, m, q, b):
    return d.store.scheduled()


@route("POST", "/api/scheduled")
def _schedule(d, m, q, b):
    number, text, due = _need(b, "number", "text", "due")
    if float(due) < time.time() - 60:
        raise ApiError(400, "that time has passed")
    return {"id": d.store.schedule(number.strip(), text, float(due))}


@route("DELETE", r"/api/scheduled/(\d+)")
def _unschedule(d, m, q, b):
    d.store.cancel_scheduled(int(m[1]))
    return {"ok": True}


@route("GET", "/api/contacts")
def _contacts(d, m, q, b):
    return d.store.contacts()


@route("POST", "/api/contacts")
def _set_contact(d, m, q, b):
    (number,) = _need(b, "number")
    d.store.set_contact(number.strip(), (b.get("name") or "").strip())
    return {"ok": True}


# ---- calls and audio ----------------------------------------------------------------

@route("GET", "/api/calls")
def _calls(d, m, q, b):
    return d.store.calls()


@route("POST", "/api/call/dial")
def _dial(d, m, q, b):
    (number,) = _need(b, "number")
    d.ui_dial(number.strip())
    return {"ok": True}


@route("POST", "/api/call/answer")
def _answer(d, m, q, b):
    d.ui_answer(bool(b.get("audio", True)))
    return {"ok": True}


@route("POST", "/api/call/hangup")
def _hangup(d, m, q, b):
    d.ui_hangup()
    return {"ok": True}


@route("POST", "/api/call/dtmf")
def _dtmf(d, m, q, b):
    (digits,) = _need(b, "digits")
    d.modem_call(lambda mo: mo.dtmf(str(digits)))
    return {"ok": True}


@route("POST", "/api/audio/bridge")
def _bridge(d, m, q, b):
    return d.ui_bridge(bool(b.get("on")), record=bool(b.get("record")))


@route("GET", r"/api/calls/(\d+)/recording")
def _recording(d, m, q, b):
    c = d.store.call(int(m[1]))
    p = d.safe_media(c and c.get("recording"), "recordings")
    if not p:
        raise ApiError(404, "no recording for this call")
    return FileResponse(p)


# ---- voicemail ----------------------------------------------------------------------

@route("GET", "/api/voicemail")
def _voicemails(d, m, q, b):
    return d.store.voicemails()


@route("GET", r"/api/voicemail/(\d+)/audio")
def _voicemail_audio(d, m, q, b):
    v = d.store.voicemail(int(m[1]))
    p = d.safe_media(v and v.get("path"), "voicemail")
    if not p:
        raise ApiError(404, "no such voicemail")
    if not v["heard"]:
        d.store.update_voicemail(v["id"], heard=1)
        d.push("voicemail_heard", id=v["id"])
    return FileResponse(p)


@route("POST", r"/api/voicemail/(\d+)")
def _voicemail_update(d, m, q, b):
    fields = {k: int(bool(b[k])) for k in ("heard", "starred") if k in b}
    d.store.update_voicemail(int(m[1]), **fields)
    d.push("voicemail_heard", id=int(m[1]))
    return {"ok": True}


@route("DELETE", r"/api/voicemail/(\d+)")
def _voicemail_delete(d, m, q, b):
    p = d.safe_media(d.store.delete_voicemail(int(m[1])), "voicemail")
    if p:
        p.unlink()
    d.push("voicemail_heard", id=int(m[1]))
    return {"ok": True}


@route("GET", "/api/voicemail/greeting")
def _greeting(d, m, q, b):
    p = d.greeting_path()
    if not p.exists() and not d.make_greeting():
        raise ApiError(404, "no greeting yet")
    return FileResponse(p)


@route("POST", "/api/voicemail/greeting")
def _greeting_text(d, m, q, b):
    (text,) = _need(b, "text")
    vm = dict(d.store.settings()["voicemail"], greeting="text", greeting_text=text)
    d.store.set_settings({"voicemail": vm})
    if not d.make_greeting(text):
        raise ApiError(503, "no text-to-speech on this computer: record your greeting instead")
    return {"ok": True}


@route("POST", "/api/voicemail/greeting/record")
def _greeting_record(d, m, q, b):
    seconds = min(max(float(b.get("seconds", 8)), 2), 30)
    d.record_greeting(seconds)
    vm = dict(d.store.settings()["voicemail"], greeting="recorded")
    d.store.set_settings({"voicemail": vm})
    return {"ok": True}


@route("POST", "/api/voicemail/call")
def _voicemail_call(d, m, q, b):
    number = d.voicemail_number()
    if not number:
        raise ApiError(404, "the operator's voicemail number isn't known: set it under Voicemail")
    d.ui_dial(number)
    return {"ok": True, "number": number}


# ---- network, console, device ------------------------------------------------

@route("GET", "/api/cell")
def _cell(d, m, q, b):
    return d.ui_cell()


@route("POST", "/api/scan")
def _scan(d, m, q, b):
    return d.ui_scan_start()


@route("GET", "/api/scan")
def _scan_state(d, m, q, b):
    return d.scan_state


@route("POST", "/api/ussd")
def _ussd(d, m, q, b):
    (code,) = _need(b, "code")
    d.ui_ussd(code.strip())
    return {"ok": True, "note": "the answer arrives as a 'ussd' event"}


@route("POST", "/api/at")
def _at(d, m, q, b):
    (cmd,) = _need(b, "cmd")
    try:
        lines = d.at_logged(cmd.strip(), timeout=float(b.get("timeout", 10)), source="ui")
        return {"ok": True, "lines": lines}
    except ATError as e:
        return {"ok": False, "error": e.result, "lines": e.lines}


@route("POST", "/api/board")
def _board(d, m, q, b):
    (cmd,) = _need(b, "cmd")
    return {"lines": d.ui_board(cmd.strip())}


@route("POST", "/api/module/off")
def _module_off(d, m, q, b):
    return d.ui_module_off()


@route("POST", "/api/module/on")
def _module_on(d, m, q, b):
    return d.ui_module_on()


@route("GET", "/api/device")
def _device(d, m, q, b):
    return d.ui_device()


@route("GET", "/api/settings")
def _settings(d, m, q, b):
    return d.store.settings()


@route("POST", "/api/settings")
def _set_settings(d, m, q, b):
    try:
        return d.store.set_settings(b)
    except KeyError as e:
        raise ApiError(400, f"unknown setting {e}")
    except ValueError as e:
        raise ApiError(400, str(e))


# ---- server -------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "koppar-lte-modem"
    daemon = None            # set by serve()
    token = ""
    port = DEFAULT_PORT

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None):
        self._send(status, json.dumps(obj, default=str).encode(), "application/json", extra)

    def _dispatch(self, method: str):
        if not self._host_ok():
            return self._json(421, {"error": "wrong host"})
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path in STATIC and method == "GET":
            f, ctype = STATIC[url.path]
            csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                   "connect-src 'self'; media-src blob:; object-src 'none'; base-uri 'none'; "
                   "form-action 'none'; frame-ancestors 'none'")
            return self._send(200, (UI_DIR / f).read_bytes(), ctype, {"Content-Security-Policy": csp})
        if not url.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            return self._json(400, {"error": "bad Content-Length"})
        if n > MAX_BODY:
            return self._json(413, {"error": "request too large"})
        body = {}
        if method in ("POST", "PUT") and n:
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._json(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "expected a JSON object"})
        if url.path == "/api/session" and method == "POST":
            # The page swaps its one-time launch code for a session of its own.
            if not take_launch_code(str(body.get("launch", ""))):
                return self._json(401, {"error": "launch code unknown or expired"})
            return self._json(200, {"session": self.daemon.store.new_session()})
        master = secrets.compare_digest(self.headers.get("X-Koppar-Token") or "", self.token)
        if not master and not self.daemon.store.session_ok(self.headers.get("X-Koppar-Session") or ""):
            return self._json(401, {"error": "not signed in: open the UI from the app or `koppar-lte-modem ui`"})
        if url.path == "/api/launch" and not master:
            return self._json(403, {"error": "launch codes need the master token"})
        if url.path == "/api/stream" and method == "GET":
            return self._stream()
        for meth, rx, fn in ROUTES:
            mt = rx.match(url.path)
            if meth == method and mt:
                try:
                    out = fn(self.daemon, mt, q, body)
                    if isinstance(out, FileResponse):
                        return self._send(200, out.path.read_bytes(), out.ctype)
                    extra = None
                    if url.path.startswith("/api/export/"):
                        extra = {"Content-Disposition": 'attachment; filename="koppar-events.json"'}
                    return self._json(200, out, extra)
                except ApiError as e:
                    return self._json(e.status, {"error": str(e)})
                except ATError as e:
                    return self._json(502, {"error": e.result, "lines": e.lines})
                except Exception as e:                     # keep the server up; report it
                    log.exception("UI request %s %s", method, url.path)
                    return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "not found"})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        d = self.daemon
        with d.sub_lock:
            d.ui_subscribers.append(q)
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while not d.stop.is_set():
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(ev, default=str).encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with d.sub_lock:
                if q in d.ui_subscribers:
                    d.ui_subscribers.remove(q)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(daemon, port: int = DEFAULT_PORT, token: str = "") -> _Server:
    handler = type("Handler", (_Handler,), {"daemon": daemon, "token": token, "port": port})
    srv = _Server(("127.0.0.1", port), handler)
    handler.port = srv.server_address[1]          # port 0: whatever the system picked
    threading.Thread(target=srv.serve_forever, name="koppar-ui", daemon=True).start()
    return srv


def ui_url(port: int, launch: str, fragment: str = "", embedded: bool = False) -> str:
    """A URL that opens the UI once: `launch` is a one-time code (new_launch_code)."""
    return (f"http://127.0.0.1:{port}/?launch={launch}" + ("&embedded=1" if embedded else "")
            + (f"#{fragment}" if fragment else ""))
