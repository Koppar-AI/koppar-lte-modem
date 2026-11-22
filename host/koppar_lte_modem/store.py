"""Local history for the daemon and its UI: events, texts, calls, AT traffic,
signal samples, contacts, scheduled texts and settings, in one SQLite file.

The module keeps at most a few hundred texts and forgets everything else;
this keeps all of it on the computer. KOPPAR_DATA overrides the folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path

DEFAULT_SETTINGS = {
    "popup_on_call": True,           # notification, and open the UI if no window is open
    "mac_audio": True,               # calls go to the Mac's microphone and speakers as soon as audio flows
    "archive_delete_from_module": False,
    "auto_reply": {"enabled": False, "text": "I can't talk right now; I'll get back to you.", "hours": 6},
    "forward_sms_to": "",            # a number: every text received is forwarded there
    "webhook_url": "",               # every event is POSTed there as JSON
    # Koppar Phone's own voicemail: answer unanswered calls before the operator's
    # voicemail does (it usually takes them after 20 s), play a greeting, record.
    "voicemail": {"enabled": False, "answer_after": 15, "max_seconds": 120, "greeting": "text",
                  "greeting_text": "Hello. I can't take your call right now. Please leave a message after the tone."},
    "voicemail_number": "",          # the operator's voicemail; empty: found from call forwarding
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, event TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, direction TEXT, number TEXT, text TEXT, time TEXT,
    slots TEXT, status TEXT, starred INTEGER DEFAULT 0, read INTEGER DEFAULT 0,
    UNIQUE (direction, number, time, text));
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY, direction TEXT, number TEXT, started TEXT, answered TEXT,
    ended TEXT, recording TEXT);
CREATE TABLE IF NOT EXISTS at_log (
    id INTEGER PRIMARY KEY, time TEXT, source TEXT, cmd TEXT, lines TEXT, ok INTEGER,
    error TEXT, ms INTEGER);
CREATE TABLE IF NOT EXISTS signal (
    time TEXT, tech TEXT, operator TEXT, band TEXT, rssi REAL, rsrp REAL, rsrq REAL, sinr REAL);
CREATE TABLE IF NOT EXISTS contacts (number TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS scheduled (
    id INTEGER PRIMARY KEY, number TEXT, text TEXT, due REAL, state TEXT DEFAULT 'pending',
    result TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, created REAL, seen REAL);
CREATE TABLE IF NOT EXISTS voicemails (
    id INTEGER PRIMARY KEY, number TEXT, time TEXT, seconds REAL, path TEXT, heard INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_number ON messages (number, time);
CREATE INDEX IF NOT EXISTS events_event ON events (event);
"""


def data_dir() -> Path:
    if os.environ.get("KOPPAR_DATA"):
        return Path(os.environ["KOPPAR_DATA"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "koppar-lte-modem"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "koppar-lte-modem"


def private_dir(d: Path) -> Path:
    """Create d (and nothing above it) readable by this user only: texts, calls,
    recordings and voicemail live here."""
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


PHONE_NUMBER = re.compile(r"^\+?[0-9]{3,20}$")


def _bool(v) -> bool:
    if not isinstance(v, bool):
        raise ValueError("expected true or false")
    return v


def _num(v, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"expected a number from {lo:g} to {hi:g}")
    return v


def _text(v, most: int) -> str:
    if not isinstance(v, str) or len(v) > most:
        raise ValueError(f"expected text of at most {most} characters")
    return v


def _number_or_empty(v) -> str:
    v = _text(v, 24).strip()
    if v and not PHONE_NUMBER.match(v):
        raise ValueError("expected a phone number: digits, optionally a leading +")
    return v


def _url_or_empty(v) -> str:
    v = _text(v, 500).strip()
    if v and not re.match(r"^https?://[^\s]+$", v):
        raise ValueError("expected an http:// or https:// address")
    return v


def validate_setting(key: str, v):
    """Settings come from the UI: check every value, because some reach the modem
    (numbers go into AT commands) or the network (the webhook)."""
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    try:
        if key in ("popup_on_call", "archive_delete_from_module", "mac_audio"):
            return _bool(v)
        if key in ("forward_sms_to", "voicemail_number"):
            return _number_or_empty(v)
        if key == "webhook_url":
            return _url_or_empty(v)
        if not isinstance(v, dict):
            raise ValueError("expected an object")
        if key == "auto_reply":
            return {"enabled": _bool(v.get("enabled", False)), "text": _text(v.get("text", ""), 480),
                    "hours": _num(v.get("hours", 6), 0, 720)}
        if key == "voicemail":
            greeting = v.get("greeting", "text")
            if greeting not in ("text", "recorded"):
                raise ValueError("greeting is text or recorded")
            return {"enabled": _bool(v.get("enabled", False)), "answer_after": _num(v.get("answer_after", 15), 3, 60),
                    "max_seconds": _num(v.get("max_seconds", 120), 10, 600), "greeting": greeting,
                    "greeting_text": _text(v.get("greeting_text", ""), 500)}
    except ValueError as e:
        raise ValueError(f"{key}: {e}") from None
    raise KeyError(key)


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _iso_local() -> str:
    t = time.localtime()
    off = time.strftime("%z", t)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + off[:3] + ":" + off[3:]


class Store:
    AT_LOG_KEEP = 20000
    SIGNAL_KEEP_DAYS = 30

    SESSION_DAYS = 30

    def __init__(self, path: Path | None = None):
        d = private_dir(data_dir())
        self.path = path or d / "koppar.db"
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
        for f in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if f.exists():
                os.chmod(f, 0o600)
        self._at_rows = 0

    # ---- low level -------------------------------------------------------

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---- token for the UI --------------------------------------------------

    def ui_token(self) -> str:
        """The master key, for this user's programs (CLI, app): sent as a header, never in a URL."""
        p = data_dir() / "ui-token"
        if p.exists():
            os.chmod(p, 0o600)
            return p.read_text().strip()
        tok = secrets.token_urlsafe(32)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # never readable by others
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok

    # ---- UI sessions (one per opened window; only a hash is stored) --------------

    def new_session(self) -> str:
        tok = secrets.token_urlsafe(32)
        now = time.time()
        self._x("INSERT INTO sessions VALUES (?, ?, ?)", (_hash(tok), now, now))
        self._x("DELETE FROM sessions WHERE seen < ?", (now - self.SESSION_DAYS * 86400,))
        return tok

    def session_ok(self, tok: str) -> bool:
        if not tok:
            return False
        h, now = _hash(tok), time.time()
        r = self._rows("SELECT seen FROM sessions WHERE hash = ?", (h,))
        if not r or r[0]["seen"] < now - self.SESSION_DAYS * 86400:
            return False
        if now - r[0]["seen"] > 600:
            self._x("UPDATE sessions SET seen = ? WHERE hash = ?", (now, h))
        return True

    # ---- settings ----------------------------------------------------------

    def settings(self) -> dict:
        out = json.loads(json.dumps(DEFAULT_SETTINGS))
        for r in self._rows("SELECT key, value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        return out

    def set_settings(self, changes: dict) -> dict:
        changes = {k: validate_setting(k, v) for k, v in changes.items()}
        for k, v in changes.items():
            self._x("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, json.dumps(v)))
        return self.settings()

    # ---- events ------------------------------------------------------------

    def add_event(self, ev: dict) -> int:
        data = {k: v for k, v in ev.items() if k not in ("event", "time", "id")}
        cur = self._x("INSERT INTO events (time, event, data) VALUES (?, ?, ?)",
                      (ev["time"], ev["event"], json.dumps(data)))
        return cur.lastrowid

    def events(self, after: int = 0, before: int | None = None, limit: int = 200, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events WHERE id > ?", [after]
        if before:
            q += " AND id < ?"
            args.append(before)
        if kind:
            q += " AND event = ?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self._rows(q, tuple(args))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # ---- messages ----------------------------------------------------------

    def add_message(self, direction: str, number: str, text: str, time_: str | None = None,
                    slots: list[int] | None = None, status: str = "") -> int | None:
        """Returns the new id, or None if this exact message is already stored."""
        cur = self._x("INSERT OR IGNORE INTO messages (direction, number, text, time, slots, status, read) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (direction, number, text, time_ or _iso_local(), json.dumps(slots or []), status,
                       1 if direction == "out" else 0))
        return cur.lastrowid if cur.rowcount else None

    def messages(self, number: str | None = None, starred: bool = False, query: str | None = None,
                 limit: int = 500) -> list[dict]:
        q, args = "SELECT * FROM messages WHERE 1", []
        if number is not None:
            q += " AND number = ?"
            args.append(number)
        if starred:
            q += " AND starred = 1"
        if query:
            q += " AND (text LIKE ? OR number LIKE ?)"
            args += [f"%{query}%"] * 2
        q += " ORDER BY time DESC, id DESC LIMIT ?"
        args.append(limit)
        rows = self._rows(q, tuple(args))
        for r in rows:
            r["slots"] = json.loads(r["slots"] or "[]")
        return rows[::-1]

    def threads(self) -> list[dict]:
        return self._rows("""
            SELECT m.number, m.text AS last_text, m.time AS last_time, m.direction AS last_direction,
                   (SELECT COUNT(*) FROM messages u WHERE u.number = m.number AND u.read = 0) AS unread,
                   (SELECT COUNT(*) FROM messages c WHERE c.number = m.number) AS count,
                   (SELECT name FROM contacts k WHERE k.number = m.number) AS name
            FROM messages m
            WHERE m.id = (SELECT id FROM messages x WHERE x.number = m.number ORDER BY time DESC, id DESC LIMIT 1)
            ORDER BY m.time DESC""")

    def mark_read(self, number: str) -> None:
        self._x("UPDATE messages SET read = 1 WHERE number = ?", (number,))

    def star(self, mid: int, on: bool) -> None:
        self._x("UPDATE messages SET starred = ? WHERE id = ?", (1 if on else 0, mid))

    def delete_message(self, mid: int) -> None:
        self._x("DELETE FROM messages WHERE id = ?", (mid,))

    # ---- calls ---------------------------------------------------------------

    def call_started(self, direction: str, number: str | None) -> int:
        return self._x("INSERT INTO calls (direction, number, started) VALUES (?, ?, ?)",
                       (direction, number or "", _iso_local())).lastrowid

    def call_update(self, cid: int, **fields) -> None:
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._x(f"UPDATE calls SET {cols} WHERE id = ?", (*fields.values(), cid))

    def calls(self, limit: int = 200) -> list[dict]:
        return self._rows("SELECT c.*, (SELECT name FROM contacts k WHERE k.number = c.number) AS name "
                          "FROM calls c ORDER BY id DESC LIMIT ?", (limit,))

    def call(self, cid: int) -> dict | None:
        r = self._rows("SELECT * FROM calls WHERE id = ?", (cid,))
        return r[0] if r else None

    # ---- voicemail ---------------------------------------------------------------

    def add_voicemail(self, number: str | None, seconds: float, path: str) -> int:
        return self._x("INSERT INTO voicemails (number, time, seconds, path) VALUES (?, ?, ?, ?)",
                       (number or "", _iso_local(), seconds, path)).lastrowid

    def voicemails(self) -> list[dict]:
        return self._rows("SELECT v.*, (SELECT name FROM contacts k WHERE k.number = v.number) AS name "
                          "FROM voicemails v ORDER BY id DESC LIMIT 500")

    def voicemail(self, vid: int) -> dict | None:
        r = self._rows("SELECT * FROM voicemails WHERE id = ?", (vid,))
        return r[0] if r else None

    def update_voicemail(self, vid: int, **fields) -> None:
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._x(f"UPDATE voicemails SET {cols} WHERE id = ?", (*fields.values(), vid))

    def delete_voicemail(self, vid: int) -> str | None:
        v = self.voicemail(vid)
        self._x("DELETE FROM voicemails WHERE id = ?", (vid,))
        return v["path"] if v else None

    def new_voicemails(self) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM voicemails WHERE heard = 0")[0]["n"]

    # ---- AT traffic --------------------------------------------------------

    def add_at(self, source: str, cmd: str, lines: list[str], ok: bool, error: str | None, ms: int) -> int:
        cur = self._x("INSERT INTO at_log (time, source, cmd, lines, ok, error, ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (_iso_local(), source, cmd, json.dumps(lines), 1 if ok else 0, error, ms))
        self._at_rows += 1
        if self._at_rows % 500 == 0:
            self._x("DELETE FROM at_log WHERE id < ?", (cur.lastrowid - self.AT_LOG_KEEP,))
        return cur.lastrowid

    def at_log(self, after: int = 0, limit: int = 300, hide_poll: bool = True) -> list[dict]:
        q = "SELECT * FROM at_log WHERE id > ?" + (" AND source != 'poll'" if hide_poll else "")
        rows = self._rows(q + " ORDER BY id DESC LIMIT ?", (after, limit))
        for r in rows:
            r["lines"] = json.loads(r["lines"])
        return rows[::-1]

    # ---- signal ----------------------------------------------------------------

    def add_signal(self, s: dict) -> None:
        self._x("INSERT INTO signal VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_iso_local(), s.get("tech"), s.get("operator"), s.get("band"), s.get("rssi"), s.get("rsrp"),
                 s.get("rsrq"), s.get("sinr")))

    def signal(self, hours: float = 24) -> list[dict]:
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - hours * 3600))
        return self._rows("SELECT * FROM signal WHERE time >= ? ORDER BY time", (since,))

    # ---- contacts ----------------------------------------------------------------

    def contacts(self) -> list[dict]:
        return self._rows("SELECT * FROM contacts ORDER BY name COLLATE NOCASE")

    def set_contact(self, number: str, name: str) -> None:
        if name:
            self._x("INSERT INTO contacts VALUES (?, ?) ON CONFLICT(number) DO UPDATE SET name = excluded.name",
                    (number, name))
        else:
            self._x("DELETE FROM contacts WHERE number = ?", (number,))

    def contact_name(self, number: str | None) -> str | None:
        if not number:
            return None
        r = self._rows("SELECT name FROM contacts WHERE number = ?", (number,))
        return r[0]["name"] if r else None

    # ---- scheduled texts ---------------------------------------------------------

    def schedule(self, number: str, text: str, due: float) -> int:
        return self._x("INSERT INTO scheduled (number, text, due) VALUES (?, ?, ?)", (number, text, due)).lastrowid

    def scheduled(self) -> list[dict]:
        return self._rows("SELECT * FROM scheduled ORDER BY due DESC LIMIT 200")

    def due(self) -> list[dict]:
        return self._rows("SELECT * FROM scheduled WHERE state = 'pending' AND due <= ?", (time.time(),))

    def scheduled_done(self, sid: int, state: str, result: str = "") -> None:
        self._x("UPDATE scheduled SET state = ?, result = ? WHERE id = ?", (state, result, sid))

    def cancel_scheduled(self, sid: int) -> None:
        self._x("UPDATE scheduled SET state = 'cancelled' WHERE id = ? AND state = 'pending'", (sid,))
