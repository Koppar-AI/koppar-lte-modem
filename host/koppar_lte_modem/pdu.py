"""SMS PDUs (3GPP TS 23.040), as the module reports them with AT+CMGF=0.

Text mode loses things the PDU keeps: the EC25 prints an alphanumeric sender
("MyShop", "Bank") as decimal character codes, and drops the user-data header
that says which part of a long text a message is. So messages are read as
PDUs and decoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# GSM 7-bit default alphabet and its extension table (3GPP TS 23.038, 6.2.1).
GSM7 = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\", 0x3C: "[", 0x3D: "~", 0x3E: "]",
            0x40: "|", 0x65: "€"}
assert len(GSM7) == 128


class PDUError(ValueError):
    pass


@dataclass
class SmsPdu:
    kind: str                 # "deliver" (received) or "submit" (stored outgoing)
    number: str               # sender (deliver) or recipient (submit); text for alphanumeric senders
    time: str                 # ISO 8601 with the network's UTC offset; "" for submit
    text: str
    concat: tuple[int, int, int] | None = None    # (reference, total parts, this part), 1-based


def unpack_septets(data: bytes, count: int) -> list[int]:
    out, acc, bits = [], 0, 0
    for b in data:
        acc |= b << bits
        bits += 8
        while bits >= 7 and len(out) < count:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    return out[:count]


def gsm7_decode(septets: list[int]) -> str:
    out, esc = [], False
    for s in septets:
        if esc:
            out.append(GSM7_EXT.get(s, GSM7[s]))
            esc = False
        elif s == 0x1B:
            esc = True
        else:
            out.append(GSM7[s])
    return "".join(out)


def _semi_octets(data: bytes) -> str:
    digits = []
    for b in data:
        for nib in (b & 0x0F, b >> 4):
            if nib == 0x0F:
                continue
            digits.append("0123456789*#abc"[nib])
    return "".join(digits)


def _address(pdu: bytes, i: int) -> tuple[str, int]:
    """Originating or destination address: length in digits, type, value."""
    ndigits, toa = pdu[i], pdu[i + 1]
    nbytes = (ndigits + 1) // 2
    raw = pdu[i + 2:i + 2 + nbytes]
    if len(raw) != nbytes:
        raise PDUError("address runs past the end")
    ton = (toa >> 4) & 0x07
    if ton == 5:                                   # alphanumeric, GSM 7-bit
        value = gsm7_decode(unpack_septets(raw, ndigits * 4 // 7))
    else:
        value = _semi_octets(raw)[:ndigits]
        if ton == 1:
            value = "+" + value
    return value, i + 2 + nbytes


def _timestamp(b: bytes) -> str:
    if len(b) < 7:
        raise PDUError("timestamp runs past the end")
    year, month, day, hour, minute, second = ((x & 0x0F) * 10 + (x >> 4) for x in b[:6])
    tz = b[6]
    quarters = (tz & 0x07) * 10 + (tz >> 4)        # the sign is bit 3 of the first semi-octet
    if tz & 0x08:
        quarters = -quarters
    try:
        t = datetime((2000 if year < 80 else 1900) + year, month, day, hour, minute, second,
                     tzinfo=timezone(timedelta(minutes=15 * quarters)))
    except ValueError as e:
        raise PDUError(f"bad timestamp {b.hex()}") from e
    return t.isoformat()


def _alphabet(dcs: int) -> str:
    group = dcs >> 4
    if group <= 0x7:                               # general data coding (incl. auto-delete)
        return ("gsm7", "8bit", "ucs2", "gsm7")[(dcs >> 2) & 3]
    if group == 0xE:
        return "ucs2"
    if group == 0xF:
        return "8bit" if dcs & 0x04 else "gsm7"
    return "gsm7"                                  # message-waiting groups 0xC, 0xD


def _concat(udh: bytes) -> tuple[int, int, int] | None:
    i = 0
    while i + 1 < len(udh):
        iei, n = udh[i], udh[i + 1]
        v = udh[i + 2:i + 2 + n]
        if iei == 0x00 and n == 3:
            return v[0], v[1], v[2]
        if iei == 0x08 and n == 4:
            return (v[0] << 8) | v[1], v[2], v[3]
        i += 2 + n
    return None


def decode(hexpdu: str) -> SmsPdu:
    """Decode an SMS-DELIVER or SMS-SUBMIT PDU as AT+CMGR/AT+CMGL print it (with the SMSC part)."""
    try:
        pdu = bytes.fromhex(hexpdu.strip())
    except ValueError as e:
        raise PDUError("not hex") from e
    try:
        i = 1 + pdu[0]                             # skip the service-centre address
        fo = pdu[i]
        i += 1
        mti = fo & 0x03
        if mti == 0:
            kind = "deliver"
            number, i = _address(pdu, i)
            pid, dcs = pdu[i], pdu[i + 1]
            time = _timestamp(pdu[i + 2:i + 9])
            i += 9
        elif mti == 1:
            kind = "submit"
            i += 1                                 # message reference
            number, i = _address(pdu, i)
            pid, dcs = pdu[i], pdu[i + 1]
            i += 2
            vpf = (fo >> 3) & 0x03
            i += {0: 0, 2: 1}.get(vpf, 7)
            time = ""
        else:
            raise PDUError(f"unsupported message type {mti}")
        udl = pdu[i]
        ud = pdu[i + 1:]
    except IndexError as e:
        raise PDUError("PDU too short") from e

    alphabet = _alphabet(dcs)
    concat = None
    udh_len = 0
    if fo & 0x40 and ud:                           # user-data header present
        udh_len = ud[0] + 1
        concat = _concat(ud[1:udh_len])
    if alphabet == "gsm7":
        septets = unpack_septets(ud, udl)
        skip = (udh_len * 8 + 6) // 7              # header plus fill bits, in septets
        text = gsm7_decode(septets[skip:])
    else:
        body = ud[udh_len:udl]
        text = body.decode("utf-16-be", "replace") if alphabet == "ucs2" else body.decode("latin-1")
    return SmsPdu(kind, number, time, text, concat)
