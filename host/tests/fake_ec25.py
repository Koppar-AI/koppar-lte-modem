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


class FakeEC25:
    def __init__(self):
        self.master, slave = os.openpty()
        self.port = os.ttyname(slave)
        self._slave = slave
        self.cscs = "IRA"
        self.csmp = "17,167,0,0"
        self.qdai = "3,0,0,4,0,0,1,1"
        self.calls: list[tuple] = []          # (idx, dir, stat, number)
        self.received: list[str] = []         # every command line
        self.sms_sent: list[tuple[str, str, str, str]] = []   # (da, body, cscs, csmp)
        self.inbox = {3: ("REC UNREAD", "+447700900123", "26/11/11,09:30:00+04", "Hej! Ring mig 👋")}
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

    def _text(self, s: str) -> str:
        return u(s) if self.cscs == "UCS2" else s

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
        simple_ok = ("ATE0", "AT", "AT+CMEE=2", "AT+CMGF=1", "AT+CSDH=0", "AT+CNMI=2,1,0,0,0", "AT+CLIP=1",
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
            self.send("+CPIN: READY", "OK")
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
            self._sms_da = cmd.split("=", 1)[1].strip('"')
            os.write(self.master, b"\r\n> ")
        elif c.startswith("AT+CMGL="):
            lines = []
            for i, (st, num, ts, text) in self.inbox.items():
                lines += [f'+CMGL: {i},"{self._text(st)}","{self._text(num)}",,"{ts}"', self._text(text)]
            self.send(*lines, "OK")
        elif c.startswith("AT+CMGR="):
            i = int(c.split("=")[1])
            if i in self.inbox:
                st, num, ts, text = self.inbox[i]
                self.send(f'+CMGR: "{self._text(st)}","{self._text(num)}",,"{ts}"', self._text(text), "OK")
            else:
                self.send("+CMS ERROR: 321")
        elif c.startswith("AT+CMGD="):
            self.inbox.pop(int(c.split("=")[1].split(",")[0]), None)
            self.send("OK")
        elif c.startswith("ATD") and c.endswith(";"):
            self.calls = [(1, 0, 2, cmd[3:-1])]
            self.send("OK")
        elif c == "AT+CLCC":
            self.send(*[f'+CLCC: {i},{d},{s},0,0,"{n}",145' for (i, d, s, n) in self.calls], "OK")
        elif c == "AT+SLOW":
            # Answers late, with a URC in the middle of the response.
            time.sleep(0.2)
            self.send("+SLOW: 1")
            self.send("RING")
            self.send("+SLOW: 2", "OK")
        else:
            self.send("ERROR")
