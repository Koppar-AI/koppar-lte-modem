import json
import os
import queue
import threading
import time

import pytest

from fake_ec25 import FakeEC25, u
from koppar_lte_modem.at import ATChannel, ATError
from koppar_lte_modem.modem import Modem, sms_parts, split_fields, ucs2_decode
from koppar_lte_modem.transport import SerialTransport


@pytest.fixture
def rig():
    fake = FakeEC25().start()
    urcs = queue.Queue()
    chan = ATChannel(SerialTransport(fake.port), on_urc=urcs.put)
    yield fake, chan, urcs
    chan.close()
    fake.stop()


def test_split_fields():
    assert split_fields('1,"a,b",,3') == ["1", "a,b", "", "3"]
    assert split_fields('"LTE",-60') == ["LTE", "-60"]


def test_ucs2():
    assert ucs2_decode(u("Hej 👋")) == "Hej 👋"
    assert ucs2_decode("REC UNREAD") == "REC UNREAD"


def test_sms_parts():
    assert sms_parts("hello") == (["hello"], False)
    assert sms_parts("hallå")[1] is True
    parts, ucs2 = sms_parts("x" * 400)
    assert not ucs2 and len(parts) == 3 and all(len(p) <= 160 for p in parts)
    parts, ucs2 = sms_parts("é" * 100)
    assert ucs2 and len(parts) == 2 and all(len(p) <= 70 for p in parts)


def test_command_and_error(rig):
    fake, chan, _ = rig
    assert chan.command("ATI")[-1] == "Revision: EC25EUXGAR08A03M1G"
    with pytest.raises(ATError) as e:
        chan.command("AT+NOPE")
    assert e.value.result == "ERROR"


def test_urc_inside_a_response(rig):
    fake, chan, urcs = rig
    assert chan.command("AT+SLOW") == ["+SLOW: 1", "+SLOW: 2"]
    assert urcs.get(timeout=1) == "RING"


def test_unsolicited_urc(rig):
    fake, chan, urcs = rig
    fake.urc('+CMTI: "SM",3')
    assert urcs.get(timeout=1) == '+CMTI: "SM",3'


def test_info_signal_registration(rig):
    fake, chan, _ = rig
    m = Modem(chan.command)
    info = m.info()
    assert info["imei"] == "861234567890123" and info["iccid"] == "89460123456789012345"
    assert m.sim() == "READY"
    sig = m.signal()
    assert sig["rssi_dbm"] == -73 and sig["rsrp"] == -89 and sig["rat"] == "LTE"
    reg = m.registration()
    assert reg["registered"] and reg["operator"] == "Telia" and reg["tech"] == "LTE" and reg["band"] == "LTE BAND 3"


def test_send_sms_ascii(rig):
    fake, chan, _ = rig
    refs = Modem(chan.command).send_sms("+447700900123", "hello from the board")
    assert refs == [1]
    assert fake.sms_sent == [("+447700900123", "hello from the board", "IRA", "17,167,0,0")]
    assert fake.cscs == "IRA"


def test_send_sms_unicode(rig):
    fake, chan, _ = rig
    Modem(chan.command).send_sms("+447700900123", "Hallå 👋")
    da, body, cscs, csmp = fake.sms_sent[0]
    assert cscs == "UCS2" and csmp == "17,167,0,8"
    assert ucs2_decode(da) == "+447700900123" and ucs2_decode(body) == "Hallå 👋"
    assert fake.cscs == "IRA"                      # restored


def test_list_and_read_sms(rig):
    fake, chan, _ = rig
    m = Modem(chan.command)
    msgs = m.list_sms()
    assert len(msgs) == 1
    assert (msgs[0].index, msgs[0].number, msgs[0].text) == (3, "+447700900123", "Hej! Ring mig 👋")
    assert msgs[0].status == "REC UNREAD"
    assert m.read_sms(3).text == "Hej! Ring mig 👋"
    m.delete_sms(3)
    assert m.list_sms() == []


def test_calls(rig):
    fake, chan, _ = rig
    m = Modem(chan.command)
    m.dial("+447700900123")
    calls = m.calls()
    assert calls[0].state == "dialing" and calls[0].direction == "out"
    m.hangup()
    assert m.calls() == []
    with pytest.raises(ValueError):
        m.dial("123; AT+CFUN=0")


def test_setup_sets_pcm(rig):
    fake, chan, _ = rig
    changed = Modem(chan.command).setup(rate=8000)
    assert fake.qdai == "1,0,0,4,0,0,1,1" and changed["reboot_needed"]
    assert Modem(chan.command).setup(rate=8000)["reboot_needed"] is False


def test_daemon_events_and_socket(rig, tmp_path, monkeypatch):
    fake, chan, _ = rig
    monkeypatch.chdir(tmp_path)                      # AF_UNIX paths are short: use a relative one
    monkeypatch.setenv("KOPPAR_SOCKET", "d.sock")
    from koppar_lte_modem.daemon import Daemon
    from koppar_lte_modem.link import DaemonLink

    d = Daemon(json_out=True)
    events = []
    d.emit = lambda ev, **f: events.append((ev, f))
    d.at, d.modem = chan, Modem(chan.command)
    chan.on_urc = d.urcs.put

    fake.calls = [(1, 1, 4, "+447700900123")]
    fake.urc("RING")
    fake.urc('+CLIP: "+447700900123",145,,,,0')
    fake.urc('+CMTI: "SM",3')
    for _ in range(3):
        d._handle_urc(d.urcs.get(timeout=1))
    names = [e[0] for e in events]
    assert names[:3] == ["ring", "call_state", "incoming_call"]
    assert events[2][1]["number"] == "+447700900123"
    sms = [e for e in events if e[0] == "sms"][0][1]
    assert sms["text"] == "Hej! Ring mig 👋" and sms["number"] == "+447700900123"

    # A command from another process goes through the socket.
    threading.Thread(target=d._serve, daemon=True).start()
    for _ in range(50):
        link = DaemonLink.connect()
        if link:
            break
        time.sleep(0.05)
    assert link.at("AT+CSQ") == ["+CSQ: 20,99"]
    link.modem().answer()
    assert fake.calls[0][2] == 0
    d._poll_calls()
    assert ("call_state", {"index": 1, "direction": "in", "state": "active", "number": "+447700900123", "voice": True}) in events
    fake.calls = []
    d._poll_calls()
    assert events[-1][0] == "call_ended"
    link.close()
    d.server.shutdown()
