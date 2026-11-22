"""A small simulated EC25 on a pseudo-terminal, for testing without the module.

    fake = FakeEC25(); fake.start()
    chan = ATChannel(SerialTransport(fake.port))
    fake.urc("RING")
"""

from __future__ import annotations

import os
import threading
import time


def u(s: str) -> str:
    return s.encode("utf-16-be").hex().upper()


# ---- SMS-DELIVER builder (3GPP TS 23.040), written from the spec for the tests ----

_GSM7 = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
         "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM7_EXT = {"\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F, "[": 0x3C, "~": 0x3D, "]": 0x3E,
             "|": 0x40, "€": 0x65}


def gsm7_septets(text: str) -> list[int] | None:
    out = []
    for ch in text:
        if ch in _GSM7_EXT:
            out += [0x1B, _GSM7_EXT[ch]]
        elif ch in _GSM7 and ch != "\x1b":
            out.append(_GSM7.index(ch))
        else:
            return None
    return out


def _pack_bits(bits: list[int]) -> bytes:
    bits = bits + [0] * (-len(bits) % 8)
    return bytes(sum(b << k for k, b in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8))


def _septet_bits(septets: list[int]) -> list[int]:
    return [(s >> k) & 1 for s in septets for k in range(7)]


def _swap(digits: str) -> bytes:
    digits += "F" * (len(digits) % 2)
    return bytes.fromhex("".join(digits[i + 1] + digits[i] for i in range(0, len(digits), 2)))


def deliver_pdu(sender: str, text: str, when: str = "261111093000", tz_quarters: int = 4,
                concat: tuple[int, int, int] | None = None, ref16: bool = False,
                dcs: int | None = None, waiting: tuple[int, int] | None = None) -> str:
    """when: YYMMDDhhmmss. concat: (reference, total, part). dcs: force the data coding
    (message-waiting groups 0xC0-0xEF). waiting: (type octet, count) as a special SMS
    message indication (IEI 0x01)."""
    if sender.startswith("+") or sender.isdigit():
        digits = sender.lstrip("+")
        oa = bytes([len(digits), 0x91 if sender.startswith("+") else 0x81]) + _swap(digits)
    else:                                                    # alphanumeric
        sept = gsm7_septets(sender)
        packed = _pack_bits(_septet_bits(sept))
        oa = bytes([-(-len(sept) * 7 // 4), 0xD0]) + packed
    tz = abs(tz_quarters)
    scts = _swap(when) + bytes([((tz % 10) << 4) | (tz // 10) | (0x08 if tz_quarters < 0 else 0)])
    ies = b""
    if concat:
        ref, total, part = concat
        ies += bytes([0x08, 4, ref >> 8, ref & 0xFF, total, part]) if ref16 else bytes([0x00, 3, ref, total, part])
    if waiting:
        ies += bytes([0x01, 2, waiting[0], waiting[1]])
    udh = bytes([len(ies)]) + ies if ies else b""
    septets = gsm7_septets(text)
    if septets is not None:
        dcs = 0x00 if dcs is None else dcs
        skip = -(-len(udh) * 8 // 7)
        bits = [(b >> k) & 1 for b in udh for k in range(8)]
        bits += [0] * (skip * 7 - len(bits)) + _septet_bits(septets)
        ud, udl = _pack_bits(bits), skip + len(septets)
    else:
        dcs = 0x08
        ud = udh + text.encode("utf-16-be")
        udl = len(ud)
    fo = 0x04 | (0x40 if udh else 0)
    body = bytes([fo]) + oa + bytes([0x00, dcs]) + scts + bytes([udl]) + ud
    return ("00" + body.hex()).upper()                       # "00": no service-centre address


class FakeEC25:
    def __init__(self):
        self.master, slave = os.openpty()
        self.port = os.ttyname(slave)
        self._slave = slave
        self.cscs = "IRA"
        self.csmp = "17,167,0,0"
        self.qdai = "3,0,0,4,0,0,1,1"
        self.cpin = "+CPIN: READY"            # or a final result, e.g. "+CME ERROR: 10"
        self.calls: list[tuple] = []          # (idx, dir, stat, number)
        self.received: list[str] = []         # every command line
        self.sms_sent: list[tuple[str, str, str, str]] = []   # (da, body, cscs, csmp)
        self.cmgf = 0                         # PDU mode, as the module is after every restart
        self.inbox: dict[int, list] = {       # index -> [stat, pdu]; stat 0 unread, 1 read
            3: [0, deliver_pdu("+447700900123", "Hej! Ring mig 👋")]}
        self.clcc_extra: list[str] = []       # e.g. the LTE data bearer: '+CLCC: 1,1,0,1,0,"",128'
        self.busy = 0                         # answer ERROR to this many audio/SIM queries (starting up)
        self._sms_da = None
        self._buf = b""
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop = True
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def send(self, *lines: str):
        os.write(self.master, b"".join(b"\r\n" + l.encode() + b"\r\n" for l in lines))

    def urc(self, line: str):
        self.send(line)

    def _run(self):
        import select

        while not self._stop:
            try:
                r, _, _ = select.select([self.master], [], [], 0.1)
                if not r:
                    continue
                data = os.read(self.master, 1024)
            except OSError:
                return
            self._buf += data
            if self._sms_da is not None:
                if b"\x1a" in self._buf:
                    body, self._buf = self._buf.split(b"\x1a", 1)
                    self.sms_sent.append((self._sms_da, body.decode(), self.cscs, self.csmp))
                    self._sms_da = None
                    time.sleep(0.05)
                    self.send(f"+CMGS: {len(self.sms_sent)}", "OK")
                continue
            while b"\r" in self._buf:
                line, self._buf = self._buf.split(b"\r", 1)
                cmd = line.decode().strip()
                if cmd:
                    self.received.append(cmd)
                    self._command(cmd)
                if self._sms_da is not None:
                    break

    def _command(self, cmd: str):
        c = cmd.upper()
        if c in ("AT+CMGF=0", "AT+CMGF=1"):
            self.cmgf = int(c[-1])
            self.send("OK")
            return
        if c == "AT+QDAI?" and self.busy > 0:
            self.busy -= 1
            self.send("ERROR")
            return
        simple_ok = ("ATE0", "AT", "AT+CMEE=2", "AT+CSDH=0", "AT+CNMI=2,1,0,0,0", "AT+CLIP=1",
                     "AT+CRC=0", "AT+CREG=0", "AT+CEREG=0", "AT+CHUP", "ATA", "AT+CNUM")
        if c in simple_ok:
            if c == "AT+CHUP":
                self.calls = []
            if c == "ATA":
                self.calls = [(i, d, 0, n) for (i, d, s, n) in self.calls]
            self.send("OK")
        elif c.startswith("AT+CSCS="):
            self.cscs = cmd.split("=", 1)[1].strip('"')
            self.send("OK")
        elif c.startswith("AT+CSMP="):
            self.csmp = cmd.split("=", 1)[1]
            self.send("OK")
        elif c == "ATI":
            self.send("Quectel", "EC25", "Revision: EC25EUXGAR08A03M1G", "OK")
        elif c == "AT+GSN":
            self.send("861234567890123", "OK")
        elif c == "AT+CIMI":
            self.send("240011234567890", "OK")
        elif c == "AT+QCCID":
            self.send("+QCCID: 89460123456789012345", "OK")
        elif c == "AT+CPIN?":
            if self.cpin.startswith("+CME ERROR"):
                self.send(self.cpin)
            else:
                self.send(self.cpin, "OK")
        elif c == "AT+CSQ":
            self.send("+CSQ: 20,99", "OK")
        elif c == "AT+QCSQ":
            self.send('+QCSQ: "LTE",-60,-89,170,-10', "OK")
        elif c == "AT+CEREG?":
            self.send("+CEREG: 0,1", "OK")
        elif c == "AT+CREG?":
            self.send("+CREG: 0,1", "OK")
        elif c == "AT+COPS?":
            self.send('+COPS: 0,0,"Telia",7', "OK")
        elif c == "AT+QNWINFO":
            self.send('+QNWINFO: "FDD LTE","24001","LTE BAND 3",1300', "OK")
        elif c == "AT+QDAI?":
            self.send(f"+QDAI: {self.qdai}", "OK")
        elif c.startswith("AT+QDAI="):
            self.qdai = cmd.split("=", 1)[1]
            self.send("OK")
        elif c == 'AT+QURCCFG="URCPORT"':
            self.send('+QURCCFG: "urcport","usbat"', "OK")
        elif c.startswith("AT+CMGS="):
            if self.cmgf != 1:                  # what the real module answers in PDU mode
                self.send("+CMS ERROR: 304")
                return
            self._sms_da = cmd.split("=", 1)[1].strip('"')
            os.write(self.master, b"\r\n> ")
        elif c.startswith("AT+CMGL=") and self.cmgf == 0:
            want = int(c.split("=")[1])
            lines = []
            for i, entry in sorted(self.inbox.items()):
                if want in (4, entry[0]):
                    lines += [f"+CMGL: {i},{entry[0]},,{len(entry[1]) // 2 - 1}", entry[1]]
                    entry[0] = max(entry[0], 1)    # listed = read
            self.send(*lines, "OK")
        elif c.startswith("AT+CMGR=") and self.cmgf == 0:
            i = int(c.split("=")[1])
            if i in self.inbox:
                entry = self.inbox[i]
                self.send(f"+CMGR: {entry[0]},,{len(entry[1]) // 2 - 1}", entry[1], "OK")
                entry[0] = max(entry[0], 1)
            else:
                self.send("+CMS ERROR: 321")
        elif c.startswith("AT+CMGD="):
            self.inbox.pop(int(c.split("=")[1].split(",")[0]), None)
            self.send("OK")
        elif c.startswith("ATD") and c.endswith(";"):
            self.calls = [(1, 0, 2, cmd[3:-1])]
            self.send("OK")
        elif c == "AT+CLCC":
            self.send(*self.clcc_extra, *[f'+CLCC: {i},{d},{s},0,0,"{n}",145' for (i, d, s, n) in self.calls], "OK")
        elif c == "AT+QPOWD=1":
            self.send("OK", "POWERED DOWN")
            threading.Timer(0.1, self.stop).start()  # the module leaves the bus
        elif c == "AT+SLOW":
            # Answers late, with a URC in the middle of the response.
            time.sleep(0.2)
            self.send("+SLOW: 1")
            self.send("RING")
            self.send("+SLOW: 2", "OK")
        else:
            self.send("ERROR")
