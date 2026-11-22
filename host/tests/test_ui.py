import json
import time
import urllib.error
import urllib.request

import pytest

from fake_ec25 import FakeEC25, deliver_pdu
from koppar_lte_modem.at import ATChannel
from koppar_lte_modem.modem import Modem, sms_parts
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


def raw(d, path, headers=None):
    req = urllib.request.Request(d.base + path, headers={"X-Koppar-Token": d.ui_token, **(headers or {})})
    return urllib.request.urlopen(req, timeout=5)


def call(d, method, path, body=None, token=True, host=None, session=None):
    req = urllib.request.Request(d.base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-Koppar-Token", d.ui_token)
    if session:
        req.add_header("X-Koppar-Session", session)
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
    # No key is accepted from a URL (history, logs, `ps`).
    assert call(d, "GET", f"/api/state?token={d.ui_token}", token=False)[0] == 401
    # A window: one-time launch code (master token only) -> a session of its own.
    code = call(d, "POST", "/api/launch", {})[1]["launch"]
    status, r = call(d, "POST", "/api/session", {"launch": code}, token=False)
    assert status == 200
    assert call(d, "GET", "/api/state", token=False, session=r["session"])[0] == 200
    assert call(d, "POST", "/api/session", {"launch": code}, token=False)[0] == 401      # used once
    assert call(d, "POST", "/api/launch", {}, token=False, session=r["session"])[0] == 403
    # Settings reach AT commands: a number can't smuggle one in.
    status, err = call(d, "POST", "/api/settings", {"forward_sms_to": '+447700";AT+CFUN=0;"'})
    assert status == 400 and "phone number" in err["error"]


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


def test_scheduled_text_is_sent_when_due(daemon):
    d, fake = daemon
    assert call(d, "POST", "/api/scheduled", {"number": "+447700900123", "text": "later", "due": time.time()})[0] == 200
    d._run_scheduled()
    assert fake.sms_sent[0][:2] == ("+447700900123", "later")
    assert call(d, "GET", "/api/scheduled")[1][0]["state"] == "sent"


@pytest.mark.parametrize("text", ["👋" * 70, "é" * 700, "x" * 1700])
def test_sms_parts_fit(text):
    parts, ucs2 = sms_parts(text)
    for p in parts:
        size = len(p.encode("utf-16-be")) // 2 if ucs2 else len(p)
        assert size <= (70 if ucs2 else 160), (len(parts), size)
    assert "".join(p.rsplit(" (", 1)[0] for p in parts) == text


def test_call_audio_goes_to_the_mac_by_itself(daemon):
    d, fake = daemon
    started = []
    d.ui_bridge = lambda on, record=False: started.append(on)
    d.emit("call_state", index=2, direction="out", state="dialing", number="+447700900123", voice=True)
    d.emit("call_audio", active=True)                  # ringback: the module's audio starts flowing
    assert started == [True]
    d.emit("call_ended", index=2, number="+447700900123", direction="out")
    d.store.set_settings({"mac_audio": False})
    d.emit("call_audio", active=True)
    assert started == [True]                           # switched off: left alone
    d.store.set_settings({"mac_audio": True})
    fake.calls = [(1, 1, 4, "+447700900123")]
    d.ui_answer(audio=False)                           # "answer without the Mac's audio"
    d.emit("call_audio", active=True)
    assert started == [True]



class FakeCallAudio:
    """The call's sound card, without one: records three seconds of 'message'."""
    rate = 8000
    played: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def play(self, samples, wait=True):
        FakeCallAudio.played.append(len(samples))

    def record_until(self, stop, max_seconds):
        import numpy as np
        return np.full(3 * self.rate, 0.1, np.float32)


def _fast_voicemail(d, after):
    """Voicemail on, answering after `after` seconds: below what the UI allows (3 s), so
    written straight into the store."""
    vm = {**d.store.settings()["voicemail"], "enabled": True, "answer_after": after}
    d.store._x("INSERT OR REPLACE INTO settings VALUES ('voicemail', ?)", (json.dumps(vm),))


def _wait(cond, seconds=3.0):
    t = time.time()
    while time.time() - t < seconds:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_voicemail_answers_greets_and_records(daemon):
    import numpy as np
    d, fake = daemon
    FakeCallAudio.played = []
    d.call_audio_factory = FakeCallAudio
    d.greeting_samples = lambda rate: np.zeros(800, np.float32)
    _fast_voicemail(d, 0.05)
    fake.calls = [(1, 1, 4, "+447700900123")]
    d._poll_calls()                                      # ringing
    d.emit("incoming_call", number="+447700900123")
    assert _wait(lambda: d.store.voicemails())
    v = d.store.voicemails()[0]
    assert (v["number"], v["seconds"], v["heard"]) == ("+447700900123", 3.0, 0)
    assert "ATA" in fake.received                        # it picked up
    assert len(FakeCallAudio.played) == 2                # greeting, then the tone
    assert _wait(lambda: "AT+CHUP" in fake.received)     # and hung up after the message
    status, state = call(d, "GET", "/api/state")
    assert state["voicemail_new"] == 1 and not state["voicemail_busy"]
    with raw(d, f"/api/voicemail/{v['id']}/audio") as r:
        assert r.headers["Content-Type"] == "audio/wav" and r.read(4) == b"RIFF"
    assert call(d, "GET", "/api/state")[1]["voicemail_new"] == 0     # listened to


def test_voicemail_steps_aside_when_you_answer(daemon):
    d, fake = daemon
    d.call_audio_factory = FakeCallAudio
    _fast_voicemail(d, 0.2)
    fake.calls = [(1, 1, 4, "+447700900123")]
    d._poll_calls()
    d.emit("incoming_call", number="+447700900123")
    fake.calls = [(1, 1, 0, "+447700900123")]            # you picked up on the Mac
    d._poll_calls()
    time.sleep(0.4)
    assert "ATA" not in fake.received and d.store.voicemails() == []


def test_operator_voicemail_signal(daemon):
    d, fake = daemon
    fake.inbox = {4: [0, deliver_pdu("+447700900999", "", dcs=0xC8)]}
    d._handle_urc('+CMTI: "ME",4')
    assert d.operator_voicemail()["active"] is True
    assert 4 not in fake.inbox                            # a bare signal: not kept as a text
    assert d.store.threads() == []


def test_recordings_play_only_from_the_data_folder(daemon, tmp_path):
    from koppar_lte_modem.store import data_dir
    d, _ = daemon
    rec = data_dir() / "recordings" / "a.wav"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_bytes(b"RIFF....WAVE")
    cid = d.store.call_started("in", "+447700900123")
    d.store.call_update(cid, recording=str(rec))
    with raw(d, f"/api/calls/{cid}/recording") as r:
        assert r.read() == b"RIFF....WAVE"
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"no")
    d.store.call_update(cid, recording=str(outside))
    assert call(d, "GET", f"/api/calls/{cid}/recording")[0] == 404
