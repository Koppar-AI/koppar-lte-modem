import json
import time
import urllib.error
import urllib.request

import pytest

from fake_ec25 import FakeEC25, deliver_pdu
from koppar_lte_modem.at import ATChannel
from koppar_lte_modem.modem import Modem, sms_parts
from koppar_lte_modem.store import Store
from koppar_lte_modem.transport import SerialTransport
from koppar_lte_modem import web


@pytest.fixture
def daemon():
    from koppar_lte_modem.daemon import Daemon

    fake = FakeEC25().start()
    d = Daemon(json_out=True, popup=False)
    d.at = ATChannel(SerialTransport(fake.port), on_urc=d.urcs.put)
    d.modem = Modem(d._modem_at)
    srv = web.serve(d, 0, d.ui_token)
    d.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield d, fake
    srv.shutdown()
    d.at.close()
    fake.stop()


def call(d, method, path, body=None, token=True, host=None):
    req = urllib.request.Request(d.base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-Koppar-Token", d.ui_token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_api_needs_the_token_and_the_right_host(daemon):
    d, _ = daemon
    assert call(d, "GET", "/api/state", token=False)[0] == 401
    assert call(d, "POST", "/api/messages/send", {"number": "+447700900123", "text": "x"}, token=False)[0] == 401
    assert call(d, "GET", "/api/state", host="evil.example:80")[0] == 421       # DNS rebinding
    status, state = call(d, "GET", "/api/state")
    assert status == 200 and state["modem"] is True


def test_page_is_served_with_a_strict_policy(daemon):
    d, _ = daemon
    with urllib.request.urlopen(d.base + "/") as r:
        assert b"app.js" in r.read()
        assert "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_send_text_from_the_ui(daemon):
    d, fake = daemon
    status, r = call(d, "POST", "/api/messages/send", {"number": "+447700900123", "text": "hello from the UI"})
    assert status == 200 and r["references"] == [1]
    assert fake.sms_sent[0][:2] == ("+447700900123", "hello from the UI")
    thread = call(d, "GET", "/api/messages?number=%2B447700900123")[1]
    assert [(m["direction"], m["text"]) for m in thread] == [("out", "hello from the UI")]
    assert call(d, "GET", "/api/events?kind=sms_sent")[1][0]["data"]["by"] == "ui"
    log = call(d, "GET", "/api/atlog")[1]
    assert any(e["cmd"].startswith('AT+CMGS="+447700900123"') and e["source"] == "ui" for e in log)
    assert call(d, "POST", "/api/messages/send", {"number": "not a number", "text": "x"})[0] == 400


def test_incoming_text_is_archived_answered_and_forwarded(daemon):
    d, fake = daemon
    d.store.set_settings({"auto_reply": {"enabled": True, "text": "Busy, later!", "hours": 6},
                          "forward_sms_to": "+447700900999"})
    fake.inbox = {7: [0, deliver_pdu("+447700900123", "are you there?")]}
    d._handle_urc('+CMTI: "ME",7')
    msgs = d.store.messages(number="+447700900123")
    assert [(m["direction"], m["text"]) for m in msgs] == [("in", "are you there?"), ("out", "Busy, later!")]
    sent = [(n, body) for n, body, *_ in fake.sms_sent]
    assert ("+447700900999", "From +447700900123: are you there?") in sent
    assert ("+447700900123", "Busy, later!") in sent
    n = len(fake.sms_sent)
    d._handle_urc('+CMTI: "ME",7')                               # the same text reported again
    assert len(fake.sms_sent) == n                               # not forwarded or answered twice
    fake.inbox = {8: [0, deliver_pdu("+447700900123", "hello??", when="261111093100")]}
    d._handle_urc('+CMTI: "ME",8')                               # a new text, same sender, within 6 h
    assert [b for _, b, *_ in fake.sms_sent].count("Busy, later!") == 1


def test_no_auto_reply_to_named_senders(daemon):
    d, fake = daemon
    d.store.set_settings({"auto_reply": {"enabled": True, "text": "Busy", "hours": 6}})
    fake.inbox = {7: [0, deliver_pdu("MyBank", "Your code is 1234")]}
    d._handle_urc('+CMTI: "ME",7')
    assert fake.sms_sent == []
    assert d.store.messages(number="MyBank")[0]["text"] == "Your code is 1234"


def test_sync_imports_once(daemon):
    d, fake = daemon
    fake.inbox = {1: [1, deliver_pdu("+447700900123", "old one")], 2: [0, deliver_pdu("MyShop", "sale")]}
    assert call(d, "POST", "/api/messages/sync", {})[1] == {"on_module": 2, "imported": 2, "deleted": 0}
    assert call(d, "POST", "/api/messages/sync", {})[1]["imported"] == 0
    threads = call(d, "GET", "/api/threads")[1]
    assert {t["number"] for t in threads} == {"+447700900123", "MyShop"}


def test_call_log_from_events(daemon):
    d, fake = daemon
    fake.calls = [(1, 1, 4, "+447700900123")]
    d.emit("incoming_call", number="+447700900123")
    d.emit("call_state", index=1, direction="in", state="active", number="+447700900123", voice=True)
    d.emit("call_ended", index=1, number="+447700900123", direction="in")
    c = call(d, "GET", "/api/calls")[1][0]
    assert (c["direction"], c["number"]) == ("in", "+447700900123") and c["answered"] and c["ended"]


def test_scheduled_text_is_sent_when_due(daemon):
    d, fake = daemon
    assert call(d, "POST", "/api/scheduled", {"number": "+447700900123", "text": "later", "due": time.time()})[0] == 200
    d._run_scheduled()
    assert fake.sms_sent[0][:2] == ("+447700900123", "later")
    assert call(d, "GET", "/api/scheduled")[1][0]["state"] == "sent"


def test_settings_reject_unknown_keys(daemon):
    d, _ = daemon
    assert call(d, "POST", "/api/settings", {"popup_on_call": False})[1]["popup_on_call"] is False
    assert call(d, "POST", "/api/settings", {"nope": 1})[0] == 400


def test_store_dedupes_messages(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.add_message("in", "+1555", "hi", "2026-11-22T10:00:00+01:00")
    assert s.add_message("in", "+1555", "hi", "2026-11-22T10:00:00+01:00") is None
    assert s.threads()[0]["unread"] == 1


@pytest.mark.parametrize("text", ["👋" * 70, "é" * 700, "x" * 1700])
def test_sms_parts_fit(text):
    parts, ucs2 = sms_parts(text)
    for p in parts:
        size = len(p.encode("utf-16-be")) // 2 if ucs2 else len(p)
        assert size <= (70 if ucs2 else 160), (len(parts), size)
    assert "".join(p.rsplit(" (", 1)[0] for p in parts) == text


def test_opening_a_conversation_marks_it_read_everywhere(daemon):
    d, fake = daemon
    import queue
    q = queue.Queue()
    d.ui_subscribers.append(q)
    d.store.add_message("in", "+447700900123", "unread one", "2026-11-22T10:00:00+01:00")
    assert call(d, "GET", "/api/state")[1]["unread"] == 1
    call(d, "GET", "/api/messages?number=%2B447700900123")
    assert call(d, "GET", "/api/state")[1]["unread"] == 0
    assert q.get(timeout=1) == {"event": "read", "ui": True, "number": "+447700900123"}
    call(d, "GET", "/api/messages?number=%2B447700900123")      # already read: no second push
    assert q.empty()


def test_call_audio_goes_to_the_mac_by_itself(daemon):
    d, _ = daemon
    started = []
    d.ui_bridge = lambda on, record=False: started.append(on)
    d.emit("call_state", index=2, direction="out", state="dialing", number="+447700900123", voice=True)
    d.emit("call_audio", active=True)                  # ringback: the module's audio starts flowing
    assert started == [True]
    d.emit("call_ended", index=2, number="+447700900123", direction="out")
    d.store.set_settings({"mac_audio": False})
    d.emit("call_audio", active=True)
    assert started == [True]                           # switched off: left alone


def test_answer_without_mac_audio_sticks_for_the_call(daemon):
    d, fake = daemon
    started = []
    d.ui_bridge = lambda on, record=False: started.append(on)
    fake.calls = [(1, 1, 4, "+447700900123")]
    d.ui_answer(audio=False)
    d.emit("call_audio", active=True)
    assert started == []
