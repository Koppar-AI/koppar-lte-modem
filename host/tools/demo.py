"""Koppar Phone with made-up data: for screenshots and UI work, no board needed.

    cd host && .venv/bin/python tools/demo.py [--port 8026]

Runs the real daemon and UI against the simulated modem from tests/, in a
throwaway data folder, filled with fictional contacts, texts, calls,
voicemail and signal history (UK drama numbers, +44 7700 900xxx). Prints a
one-time URL to open. Ctrl-C to stop; nothing is kept.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tests"))
os.environ["KOPPAR_DATA"] = tempfile.mkdtemp(prefix="koppar-demo-")

from fake_ec25 import FakeEC25, deliver_pdu                     # noqa: E402
from koppar_lte_modem import web                                 # noqa: E402
from koppar_lte_modem.at import ATChannel                        # noqa: E402
from koppar_lte_modem.daemon import Daemon                       # noqa: E402
from koppar_lte_modem.modem import Modem                         # noqa: E402
from koppar_lte_modem.store import data_dir, private_dir         # noqa: E402
from koppar_lte_modem.transport import SerialTransport           # noqa: E402

CONTACTS = {
    "+447700900101": "Maja Lind",
    "+447700900102": "Tom Becker",
    "+447700900103": "Studio Nord",
    "+447700900104": "Lucia Ferrari",
}


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def seed(d: Daemon) -> None:
    s = d.store
    now = datetime.now()
    for number, name in CONTACTS.items():
        s.set_contact(number, name)

    texts = [   # (minutes ago, direction, number, text)
        (300, "in", "+447700900102", "Are we still on for Thursday?"),
        (296, "out", "+447700900102", "Yes, 7 pm at the usual place."),
        (190, "in", "MyBank", "Your code is 482 911. Never share it with anyone."),
        (95, "in", "+447700900103", "Your prints are ready: pick them up any time before 6."),
        (42, "in", "+447700900101", "Landed! Taking the train now 🚆"),
        (41, "out", "+447700900101", "Welcome back. Call me when you're home."),
        (12, "in", "+447700900101", "Home. Calling you in 5."),
        (3, "in", "+447700900104", "Did you get my voicemail?"),
    ]
    for ago, direction, number, text in texts:
        s.add_message(direction, number, text, iso(now - timedelta(minutes=ago)), status="sent" if direction == "out" else "")
    s.mark_read("+447700900102")
    s.mark_read("MyBank")
    s.mark_read("+447700900103")
    s.star(4, True)                      # the prints are ready: worth keeping

    def call(ago, direction, number, seconds, answered=True, recording=None):
        cid = s.call_started(direction, number)
        start = now - timedelta(minutes=ago)
        fields = {"started": iso(start), "ended": iso(start + timedelta(seconds=seconds + 8))}
        if answered:
            fields["answered"] = iso(start + timedelta(seconds=8))
        if recording:
            fields["recording"] = recording
        s.call_update(cid, **fields)

    rec = make_wav(private_dir(data_dir() / "recordings") / "demo-call.wav", 6)
    call(260, "out", "+447700900102", 184)
    call(130, "in", "+447700900103", 52, recording=rec)
    call(48, "in", "+447700900104", 0, answered=False)
    call(8, "in", "+447700900101", 421)

    vm = private_dir(data_dir() / "voicemail")
    old = s.add_voicemail("+447700900102", 9.8, make_wav(vm / "demo-1.wav", 10))
    s.update_voicemail(old, heard=1, time=iso(now - timedelta(hours=26)))
    new = s.add_voicemail("+447700900104", 23.4, make_wav(vm / "demo-2.wav", 23))
    s.update_voicemail(new, time=iso(now - timedelta(minutes=47)))
    d.store.set_settings({"voicemail": {**s.settings()["voicemail"], "enabled": True}})

    # Six hours of signal, one sample a minute.
    for i in range(360, 0, -1):
        t = now - timedelta(minutes=i)
        rsrp = -101 + 6 * math.sin(i / 37) + 3 * math.sin(i / 7)
        s._x("INSERT INTO signal VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
             (iso(t), "LTE", "Telia", "LTE BAND 3", rsrp + 32, round(rsrp), round(-10 + 2 * math.sin(i / 11)),
              round(12 + 5 * math.sin(i / 23), 1)))

    d.sim_state = "READY"
    d.module_info = {"model": "Quectel EC25", "revision": "EC25EUXGAR08A08M1G", "imei": "350000000000001",
                     "iccid": "89000000000000000001"}
    d.network = {"state": "home", "registered": True, "operator": "Telia", "tech": "LTE", "band": "LTE BAND 3"}
    d.last_signal = {"tech": "LTE", "operator": "Telia", "band": "LTE BAND 3", "state": "home",
                     "rssi": -71, "rsrp": -99, "rsrq": -9, "sinr": 14.2, "bars": 3}
    d.voicemail_number_found = "+447700900900"
    for ev in [("ready", {"sim": "READY", "imei": "350000000000001", "revision": "EC25EUXGAR08A08M1G"}),
               ("network", {"state": "home", "operator": "Telia", "tech": "LTE", "band": "LTE BAND 3", "rssi_dbm": -71}),
               ("sms", {"index": 4, "parts": [4], "number": "+447700900101", "sent": iso(now), "text": "Home. Calling you in 5."}),
               ("incoming_call", {"number": "+447700900101"}),
               ("call_state", {"index": 1, "direction": "in", "state": "active", "number": "+447700900101", "voice": True}),
               ("call_audio", {"active": True}),
               ("call_ended", {"index": 1, "number": "+447700900101", "direction": "in"}),
               ("voicemail", {"id": 2, "number": "+447700900104", "seconds": 23.4}),
               ("sms", {"index": 5, "parts": [5], "number": "+447700900104", "sent": iso(now), "text": "Did you get my voicemail?"})]:
        s.add_event({"event": ev[0], "time": datetime.now().astimezone().isoformat(timespec="seconds"), **ev[1]})


def make_wav(path: Path, seconds: float) -> str:
    """A soft two-tone stand-in for a recorded voice."""
    import numpy as np
    from koppar_lte_modem.audio import write_wav

    rate = 8000
    t = np.arange(int(seconds * rate)) / rate
    x = 0.08 * np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t))
    write_wav(str(path), x.astype(np.float32), rate)
    return str(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8026)
    args = ap.parse_args()

    fake = FakeEC25().start()
    fake.inbox = {}
    d = Daemon(json_out=True, popup=False, ui_port=args.port)
    d.at = ATChannel(SerialTransport(fake.port), on_urc=d.urcs.put)
    d.modem = Modem(d._modem_at)
    seed(d)
    srv = web.serve(d, args.port, d.ui_token)
    print(f"demo UI (one-time link, 60 s): {web.ui_url(args.port, web.new_launch_code())}", flush=True)
    print(f"data folder (deleted on exit): {data_dir()}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        fake.stop()
        import shutil
        shutil.rmtree(data_dir(), ignore_errors=True)


if __name__ == "__main__":
    main()
