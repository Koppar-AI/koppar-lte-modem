"""High-level EC25 operations on top of an AT command function.

`at(cmd, timeout=5.0, data=None) -> list[str]` can be ATChannel.command or
the daemon client's equivalent, so the same code runs in-process or through
`koppar-lte-modem daemon`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Callable

from .at import ATError

ATFunc = Callable[..., list]

# AT+QDAI: <io>=1 customer-defined PCM, <mode>=0 module is master, <fsync>=0
# primary (short sync), <clock>, <format>=0 16-bit linear, <sample>,
# <num_slots>=1, <slot_mapping>=1. Saved to NV; effective after a module reset.
QDAI_8K = "1,0,0,4,0,0,1,1"     # 2048 kHz clock, 8 kHz
QDAI_16K = "1,0,0,5,0,1,1,1"    # 4096 kHz clock, 16 kHz (the only clock Quectel allows at 16 kHz)

CLCC_STATES = {0: "active", 1: "held", 2: "dialing", 3: "alerting", 4: "incoming", 5: "waiting"}
REG_STATES = {0: "not-registered", 1: "home", 2: "searching", 3: "denied", 4: "unknown", 5: "roaming"}

# Characters that survive CSCS="IRA" -> GSM 7-bit unchanged. Anything else
# (accents, emoji, GSM extension characters like [ ] { } ~ ^ \ |) goes as UCS2.
_GSM_SAFE = set("@$\n\r !\"#%&'()*+,-./0123456789:;<=>?ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz")


def split_fields(s: str) -> list[str]:
    """Split an AT response value on commas, honouring quotes: 1,"a,b",,3 -> ['1','a,b','','3']."""
    out, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
        elif ch == "," and not q:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return out


def ucs2_decode(s: str) -> str:
    s = s.strip()
    if s and len(s) % 4 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", s):
        try:
            return bytes.fromhex(s).decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return s


def ucs2_encode(s: str) -> str:
    return s.encode("utf-16-be").hex().upper()


def sms_parts(text: str) -> tuple[list[str], bool]:
    """Split text into single-SMS parts; returns (parts, needs_ucs2)."""
    ucs2 = any(ch not in _GSM_SAFE for ch in text)
    limit, part_limit = (70, 64) if ucs2 else (160, 152)   # leave room for " (1/3)"
    if len(text) <= limit:
        return [text], ucs2
    chunks = [text[i:i + part_limit] for i in range(0, len(text), part_limit)]
    n = len(chunks)
    return [f"{c} ({i + 1}/{n})" for i, c in enumerate(chunks)], ucs2


@dataclass
class SMS:
    index: int
    status: str
    number: str
    time: str
    text: str

    def as_dict(self):
        return asdict(self)


@dataclass
class Call:
    index: int
    direction: str     # "out" / "in"
    state: str         # active, held, dialing, alerting, incoming, waiting
    number: str
    voice: bool

    def as_dict(self):
        return asdict(self)


class Modem:
    def __init__(self, at: ATFunc):
        self.at = at

    # ---- helpers ---------------------------------------------------------

    def _value(self, cmd: str, prefix: str, timeout: float = 5.0) -> str | None:
        for line in self.at(cmd, timeout=timeout):
            if line.startswith(prefix + ":"):
                return line[len(prefix) + 1:].strip()
        return None

    def _first_plain(self, cmd: str) -> str | None:
        for line in self.at(cmd):
            if not line.startswith("+"):
                return line.strip()
        return None

    # ---- session ---------------------------------------------------------

    def init_session(self) -> None:
        """Settings that do not survive a module restart: run after every boot."""
        for cmd in (
            "ATE0",                      # no echo
            "AT+CMEE=2",                 # verbose errors
            'AT+CSCS="IRA"',
            "AT+CMGF=1",                 # SMS text mode
            "AT+CSDH=0",
            "AT+CNMI=2,1,0,0,0",         # new SMS -> +CMTI: "SM",<index>
            "AT+CLIP=1",                 # caller ID after RING
            "AT+CRC=0",                  # plain RING
            "AT+CREG=0",                 # registration is polled, not reported
            "AT+CEREG=0",
        ):
            try:
                self.at(cmd)
            except ATError:
                pass

    def setup(self, rate: int = 8000) -> dict:
        """One-time module settings (saved in its NV). Returns what changed;
        'reboot_needed' is True if the module must restart to apply them."""
        changed = {}
        want = QDAI_16K if rate == 16000 else QDAI_8K
        cur = self._value("AT+QDAI?", "+QDAI")
        if (cur or "").replace(" ", "") != want:
            self.at(f"AT+QDAI={want}")
            changed["qdai"] = f"{cur} -> {want}"
        try:
            urc = self._value('AT+QURCCFG="urcport"', "+QURCCFG")
            if urc and '"usbat"' not in urc:
                self.at('AT+QURCCFG="urcport","usbat"')
                changed["urcport"] = f"{urc} -> usbat"
        except ATError:
            pass
        self.init_session()
        changed["reboot_needed"] = "qdai" in changed
        return changed

    def restart(self) -> None:
        """Full functionality reset: the module reboots and re-enumerates on USB."""
        try:
            self.at("AT+CFUN=1,1", timeout=15)
        except ATError:
            pass

    def power_down(self) -> None:
        """Normal power-down (AT+QPOWD=1). Wait for STATUS to clear before cutting power."""
        self.at("AT+QPOWD=1", timeout=15)

    # ---- information -----------------------------------------------------

    def info(self) -> dict:
        d: dict = {}
        lines = self.at("ATI")
        d["model"] = " ".join(l for l in lines if not l.startswith("Revision")).strip()
        d["revision"] = next((l.split(":", 1)[1].strip() for l in lines if l.startswith("Revision")), "")
        for key, cmd, prefix in (
            ("imei", "AT+GSN", None),
            ("imsi", "AT+CIMI", None),
            ("iccid", "AT+QCCID", "+QCCID"),
        ):
            try:
                d[key] = self._value(cmd, prefix) if prefix else self._first_plain(cmd)
            except ATError as e:
                d[key] = None if "SIM" in e.result.upper() else f"error: {e.result}"
        try:
            num = self._value("AT+CNUM", "+CNUM")
            d["number"] = split_fields(num)[1] if num else None
        except ATError:
            d["number"] = None
        return d

    def sim(self) -> str:
        try:
            return self._value("AT+CPIN?", "+CPIN") or "unknown"
        except ATError as e:
            return e.result.split(":", 1)[-1].strip()

    def enter_pin(self, pin: str) -> None:
        self.at(f'AT+CPIN="{pin}"', timeout=10)

    def signal(self) -> dict:
        d: dict = {}
        csq = self._value("AT+CSQ", "+CSQ")
        if csq:
            rssi = int(split_fields(csq)[0])
            d["csq"] = rssi
            d["rssi_dbm"] = None if rssi == 99 else -113 + 2 * rssi
            d["bars"] = 0 if rssi == 99 or rssi < 2 else 1 if rssi < 10 else 2 if rssi < 15 else 3 if rssi < 20 else 4
        try:
            q = self._value("AT+QCSQ", "+QCSQ")
            if q:
                f = split_fields(q)
                d["rat"] = f[0]
                names = {"LTE": ("rssi", "rsrp", "sinr", "rsrq"), "WCDMA": ("rssi", "rscp", "ecio"), "GSM": ("rssi",)}
                for name, val in zip(names.get(f[0], ()), f[1:]):
                    try:
                        d[name] = int(val)
                    except ValueError:
                        pass
        except ATError:
            pass
        return d

    def registration(self) -> dict:
        d = {"state": "unknown", "tech": None, "operator": None}
        best = None
        for cmd, prefix in (("AT+CEREG?", "+CEREG"), ("AT+CREG?", "+CREG")):
            try:
                v = self._value(cmd, prefix)
            except ATError:
                continue
            if v:
                stat = int(split_fields(v)[1])
                if stat in (1, 5) and best is None:
                    best = stat
                if best is None and d["state"] == "unknown":
                    d["state"] = REG_STATES.get(stat, str(stat))
        if best is not None:
            d["state"] = REG_STATES[best]
        try:
            cops = self._value("AT+COPS?", "+COPS")
            if cops:
                f = split_fields(cops)
                if len(f) >= 3:
                    d["operator"] = f[2]
                if len(f) >= 4:
                    d["tech"] = {"0": "GSM", "2": "UTRAN", "3": "GSM/EGPRS", "4": "HSDPA", "5": "HSUPA",
                                 "6": "HSPA", "7": "LTE"}.get(f[3], f[3])
        except ATError:
            pass
        try:
            nw = self._value("AT+QNWINFO", "+QNWINFO")
            if nw:
                f = split_fields(nw)
                if len(f) >= 3:
                    d["band"] = f[2]
        except ATError:
            pass
        d["registered"] = d["state"] in ("home", "roaming")
        return d

    # ---- SMS -------------------------------------------------------------

    def send_sms(self, number: str, text: str) -> list[int]:
        """Send a text; long texts go as numbered parts. Returns message references."""
        parts, ucs2 = sms_parts(text)
        refs = []
        try:
            if ucs2:
                self.at('AT+CSCS="UCS2"')
                self.at("AT+CSMP=17,167,0,8")
                da = ucs2_encode(number)
            else:
                self.at('AT+CSCS="IRA"')
                self.at("AT+CSMP=17,167,0,0")
                da = number
            for part in parts:
                body = ucs2_encode(part) if ucs2 else part
                lines = self.at(f'AT+CMGS="{da}"', timeout=60, data=body)
                for l in lines:
                    if l.startswith("+CMGS:"):
                        refs.append(int(l.split(":", 1)[1]))
        finally:
            self.at('AT+CSCS="IRA"')
            self.at("AT+CSMP=17,167,0,0")
        return refs

    def _with_ucs2(self, fn):
        self.at('AT+CSCS="UCS2"')
        try:
            return fn()
        finally:
            self.at('AT+CSCS="IRA"')

    def list_sms(self, which: str = "ALL") -> list[SMS]:
        """which: ALL, REC UNREAD, REC READ, STO SENT, STO UNSENT."""
        def run():
            # With CSCS="UCS2", 27.005 says string parameters are UCS2 too;
            # some firmware wants the plain word anyway.
            try:
                lines = self.at(f'AT+CMGL="{ucs2_encode(which)}"', timeout=20)
            except ATError:
                lines = self.at(f'AT+CMGL="{which}"', timeout=20)
            out: list[SMS] = []
            cur = None
            for l in lines:
                if l.startswith("+CMGL:"):
                    f = split_fields(l[6:])
                    cur = SMS(int(f[0]), ucs2_decode(f[1]), ucs2_decode(f[2]), f[4] if len(f) > 4 else "", "")
                    out.append(cur)
                elif cur is not None:
                    cur.text += ("\n" if cur.text else "") + ucs2_decode(l)
            return out
        return self._with_ucs2(run)

    def read_sms(self, index: int) -> SMS | None:
        def run():
            lines = self.at(f"AT+CMGR={index}", timeout=10)
            msg = None
            for l in lines:
                if l.startswith("+CMGR:"):
                    f = split_fields(l[6:])
                    msg = SMS(index, ucs2_decode(f[0]), ucs2_decode(f[1]), f[3] if len(f) > 3 else "", "")
                elif msg is not None:
                    msg.text += ("\n" if msg.text else "") + ucs2_decode(l)
            return msg
        return self._with_ucs2(run)

    def delete_sms(self, index: int) -> None:
        self.at(f"AT+CMGD={index}")

    def delete_all_sms(self) -> None:
        self.at("AT+CMGD=1,4", timeout=25)

    # ---- calls -----------------------------------------------------------

    def dial(self, number: str) -> None:
        if not re.fullmatch(r"\+?[0-9*#]+", number):
            raise ValueError(f"not a phone number: {number!r}")
        self.at(f"ATD{number};", timeout=30)

    def answer(self) -> None:
        self.at("ATA", timeout=15)

    def hangup(self) -> None:
        try:
            self.at("AT+CHUP", timeout=10)
        except ATError:
            self.at("ATH", timeout=10)

    def calls(self) -> list[Call]:
        out = []
        for l in self.at("AT+CLCC"):
            if l.startswith("+CLCC:"):
                f = split_fields(l[6:])
                out.append(Call(int(f[0]), "in" if f[1] == "1" else "out", CLCC_STATES.get(int(f[2]), f[2]),
                                f[5] if len(f) > 5 else "", f[3] == "0"))
        return out

    def dtmf(self, digits: str) -> None:
        for d in digits:
            if d not in "0123456789*#ABCD":
                raise ValueError(f"not a DTMF digit: {d!r}")
            self.at(f'AT+VTS="{d}"', timeout=5)

    # ---- data (ECM) ------------------------------------------------------

    def data_setup(self, apn: str, pdp: str = "IPV4V6") -> dict:
        """Switch the module to ECM (a class-compliant USB network adapter,
        which macOS and Linux drive without extra software) and set the APN.
        The module NATs and serves DHCP (192.168.225.x). Needs a restart."""
        changed = {}
        cur = self._value('AT+QCFG="usbnet"', "+QCFG")
        if not cur or split_fields(cur)[-1] != "1":
            self.at('AT+QCFG="usbnet",1')
            changed["usbnet"] = f"{cur} -> ecm"
        self.at(f'AT+CGDCONT=1,"{pdp}","{apn}"')
        changed["apn"] = apn
        changed["reboot_needed"] = "usbnet" in changed
        return changed

    def data_status(self) -> dict:
        d: dict = {}
        try:
            v = self._value('AT+QCFG="usbnet"', "+QCFG")
            mode = split_fields(v)[-1] if v else "?"
            d["usbnet"] = {"0": "rmnet/qmi", "1": "ecm", "2": "mbim", "3": "rndis"}.get(mode, mode)
        except ATError:
            pass
        try:
            d["contexts"] = [split_fields(l[9:]) for l in self.at("AT+CGDCONT?") if l.startswith("+CGDCONT:")]
            d["active"] = [split_fields(l[8:]) for l in self.at("AT+CGACT?") if l.startswith("+CGACT:")]
            d["addresses"] = [split_fields(l[9:]) for l in self.at("AT+CGPADDR") if l.startswith("+CGPADDR:")]
        except ATError:
            pass
        return d
