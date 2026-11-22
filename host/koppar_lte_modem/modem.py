"""High-level EC25 operations on top of an AT command function.

`at(cmd, timeout=5.0, data=None) -> list[str]` can be ATChannel.command or
the daemon client's equivalent, so the same code runs in-process or through
`koppar-lte-modem daemon`.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import pdu
from .at import ATClosed, ATError

ATFunc = Callable[..., list]

# AT+QDAI: <io>=1 customer-defined PCM, <mode>=0 module is master, <fsync>=0
# primary (short sync), <clock>, <format>=0 16-bit linear, <sample>,
# <num_slots>=1, <slot_mapping>=1. Saved to NV; effective after a module reset.
QDAI_8K = "1,0,0,4,0,0,1,1"     # 2048 kHz clock, 8 kHz
QDAI_16K = "1,0,0,5,0,1,1,1"    # 4096 kHz clock, 16 kHz (the only clock Quectel allows at 16 kHz)

CLCC_STATES = {0: "active", 1: "held", 2: "dialing", 3: "alerting", 4: "incoming", 5: "waiting"}
# AT+CMGL / AT+CMGR <stat> in PDU mode.
SMS_STATES = {0: "REC UNREAD", 1: "REC READ", 2: "STO UNSENT", 3: "STO SENT"}
SMS_LIST = {"REC UNREAD": 0, "REC READ": 1, "STO UNSENT": 2, "STO SENT": 3, "ALL": 4}
REG_STATES = {0: "not-registered", 1: "home", 2: "searching", 3: "denied", 4: "unknown", 5: "roaming"}

# +CME ERROR codes AT+CPIN? can return (3GPP TS 27.007), as the module words them with AT+CMEE=2.
SIM_ERRORS = {"10": "SIM not inserted", "11": "SIM PIN required", "12": "SIM PUK required",
              "13": "SIM failure", "14": "SIM busy", "15": "SIM wrong"}

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
    limit = 70 if ucs2 else 160
    # UCS2 limits are in UTF-16 code units: an emoji takes two.
    size = (lambda s: len(s.encode("utf-16-be")) // 2) if ucs2 else len
    if size(text) <= limit:
        return [text], ucs2
    n = 2
    while True:                                    # the " (i/n)" suffix grows with n
        room = limit - len(f" ({n}/{n})")
        chunks, cur = [], ""
        for ch in text:                            # never split a character
            if size(cur + ch) > room:
                chunks.append(cur)
                cur = ""
            cur += ch
        chunks.append(cur)
        if len(chunks) <= n:
            n = len(chunks)
            return [f"{c} ({i + 1}/{n})" for i, c in enumerate(chunks)], ucs2
        n = len(chunks)


@dataclass
class SMS:
    index: int
    status: str
    number: str        # sender, or the sender's name for alphanumeric senders ("MyShop", "Bank")
    time: str          # ISO 8601 with the network's UTC offset
    text: str
    parts: list[int] = field(default_factory=list)   # storage indexes of every part, in order
    concat: tuple[int, int, int] | None = field(default=None, repr=False)   # (ref, total, part) of one part
    mwi: dict | None = field(default=None, repr=False)   # voicemail waiting etc. (see pdu.SmsPdu.mwi)

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


def _part(index: int, stat: int, hexpdu: str) -> SMS:
    status = SMS_STATES.get(stat, str(stat))
    try:
        d = pdu.decode(hexpdu)
    except pdu.PDUError as e:
        return SMS(index, status, "", "", f"[undecodable message: {e}]", [index])
    return SMS(index, status, d.number, d.time, d.text, [index], d.concat, d.mwi)


def merge_parts(parts: list[SMS]) -> list[SMS]:
    """Join the parts of long texts (same sender and reference) into one SMS each,
    in storage order of their first part. A missing part shows as "[part n missing]"."""
    slots: list[SMS | tuple] = []                  # a single SMS, or the key of a long text
    groups: dict[tuple, dict[int, SMS]] = {}
    for p in parts:
        if p.concat is None:
            slots.append(p)
            continue
        ref, total, n = p.concat
        key = (p.number, ref, total)
        if key not in groups:
            groups[key] = {}
            slots.append(key)
        groups[key][n] = p
    out = []
    for slot in slots:
        if isinstance(slot, SMS):
            out.append(slot)
            continue
        got = groups[slot]
        total = slot[2]
        first = got[min(got)]
        unread = any(p.status == "REC UNREAD" for p in got.values())
        text = "".join(got[n].text if n in got else f"[part {n} missing]" for n in range(1, total + 1))
        out.append(SMS(first.index, "REC UNREAD" if unread else first.status, first.number, first.time, text,
                       [got[n].index for n in sorted(got)]))
    return out


class Modem:
    ready_wait = 30.0       # seconds _when_ready keeps retrying, and how often
    ready_poll = 2.0

    def __init__(self, at: ATFunc):
        self.at = at

    # ---- helpers ---------------------------------------------------------

    def _value(self, cmd: str, prefix: str, timeout: float = 5.0) -> str | None:
        for line in self.at(cmd, timeout=timeout):
            if line.startswith(prefix + ":"):
                return line[len(prefix) + 1:].strip()
        return None

    def _when_ready(self, cmd: str, prefix: str) -> str | None:
        """_value, retried while the module answers ERROR: for the first seconds
        after it starts, its audio and SIM commands are not ready yet."""
        deadline = time.monotonic() + self.ready_wait
        while True:
            try:
                return self._value(cmd, prefix)
            except ATClosed:
                raise
            except ATError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(self.ready_poll)

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

    def sms_storage(self) -> tuple[str, int, int] | None:
        """Where received texts are stored: (memory, used, total), e.g. ("SM", 4, 50)."""
        v = self._value("AT+CPMS?", "+CPMS")
        if not v:
            return None
        f = split_fields(v)
        try:
            return f[6], int(f[7]), int(f[8])          # <mem3>: where new texts go
        except (IndexError, ValueError):
            return None

    def store_texts_on_sim(self) -> bool:
        """Keep received texts on the SIM, not in the module's flash: one less thing
        written to the module's file system, which a power cut can damage."""
        cur = self.sms_storage()
        if cur and cur[0] == "SM":
            return True
        try:
            self.at('AT+CPMS="SM","SM","SM"')
            return True
        except ATError:
            return False                               # a SIM without SMS storage: leave it

    def setup(self, rate: int = 8000) -> dict:
        """One-time module settings (saved in its NV). Returns what changed;
        'reboot_needed' is True if the module must restart to apply them."""
        changed = {}
        want = QDAI_16K if rate == 16000 else QDAI_8K
        cur = self._when_ready("AT+QDAI?", "+QDAI")
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
            # Numeric when AT+CMEE=2 has not been set since the module started.
            err = e.result.split(":", 1)[-1].strip()
            return SIM_ERRORS.get(err, err)

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
                if "sinr" in d:                 # 0-250 in 1/5 dB from -20 dB
                    d["sinr"] = round(d["sinr"] / 5 - 20, 1)
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
        if not re.fullmatch(r"\+?[0-9]{3,20}", number):
            raise ValueError(f"not a phone number: {number!r}")     # it goes into an AT command
        parts, ucs2 = sms_parts(text)
        refs = []
        self.at("AT+CMGF=1")                       # the module forgets it on every restart
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

    # Texts are read as PDUs (see pdu.py): text mode garbles alphanumeric
    # senders and hides which part of a long text a message is. Each SMS
    # command sets the mode it needs, because the module forgets AT+CMGF on
    # every restart.

    def _parts(self, which: str = "ALL") -> list[SMS]:
        self.at("AT+CMGF=0")
        lines = self.at(f"AT+CMGL={SMS_LIST[which]}", timeout=20)
        out: list[SMS] = []
        head = None
        for l in lines:
            if l.startswith("+CMGL:"):
                head = split_fields(l[6:])
            elif head is not None:
                out.append(_part(int(head[0]), int(head[1]), l))
                head = None
        return out

    def read_part(self, index: int) -> SMS | None:
        """One stored message as it is: a long text's parts come one by one (see .concat)."""
        self.at("AT+CMGF=0")
        lines = self.at(f"AT+CMGR={index}", timeout=10)
        for i, l in enumerate(lines):
            if l.startswith("+CMGR:") and i + 1 < len(lines):
                return _part(index, int(split_fields(l[6:])[0]), lines[i + 1])
        return None

    def list_sms(self, which: str = "ALL") -> list[SMS]:
        """which: ALL, REC UNREAD, REC READ, STO SENT, STO UNSENT. Long texts come back whole."""
        return merge_parts(self._parts(which))

    def read_sms(self, index: int) -> SMS | None:
        """The message stored at index; for one part of a long text, the whole text."""
        part = self.read_part(index)
        if part is None or part.concat is None:
            return part
        for msg in self.list_sms():
            if index in msg.parts:
                return msg
        return part

    def delete_sms(self, index: int) -> None:
        """Delete one storage slot (one part of a long text)."""
        self.at(f"AT+CMGD={index}")

    def delete_message(self, index: int) -> list[int]:
        """Delete the message at index with all of its parts; returns the slots deleted."""
        msg = self.read_sms(index)
        slots = msg.parts if msg and msg.parts else [index]
        for i in slots:
            self.delete_sms(i)
        return slots

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

    def calls(self, voice_only: bool = True) -> list[Call]:
        """Current calls. On LTE the module also lists its data connection here
        (<mode> 1, an "incoming", "active" call with no number): skipped unless
        voice_only is False."""
        out = []
        for l in self.at("AT+CLCC"):
            if l.startswith("+CLCC:"):
                f = split_fields(l[6:])
                voice = f[3] == "0"
                if voice_only and not voice:
                    continue
                out.append(Call(int(f[0]), "in" if f[1] == "1" else "out", CLCC_STATES.get(int(f[2]), f[2]),
                                f[5] if len(f) > 5 else "", voice))
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
