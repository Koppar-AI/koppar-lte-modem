import pytest

from fake_ec25 import deliver_pdu
from koppar_lte_modem.pdu import PDUError, decode


def test_public_vector():
    # The classic SMS-DELIVER example (dreamfabric.com's PDU format page):
    # "hellohello" from 27838890001, sent 1999-03-29 15:16:59 at UTC+2.
    d = decode("07917283010010F5040BC87238880900F10000993092516195800AE8329BFD4697D9EC37")
    assert (d.kind, d.number, d.text) == ("deliver", "27838890001", "hellohello")
    assert d.time == "1999-03-29T15:16:59+02:00"
    assert d.concat is None


def test_alphanumeric_sender():
    # Text mode prints these as decimal character codes; the PDU has the name.
    d = decode(deliver_pdu("MyBank", "Your code is 1234"))
    assert (d.number, d.text) == ("MyBank", "Your code is 1234")


def test_gsm7_accents_and_extension_table():
    text = "Grüße, smørrebrød à la carte? €5 [x] {y} ~ ^ | \\"
    d = decode(deliver_pdu("+447700900123", text))
    assert d.text == text


def test_ucs2_emoji_and_negative_offset():
    d = decode(deliver_pdu("+15551234567", "Hallå 👋", when="261122194446", tz_quarters=-20))
    assert d.text == "Hallå 👋"
    assert d.time == "2026-11-22T19:44:46-05:00"


@pytest.mark.parametrize("text, ref16", [("x" * 150, False), ("é👋" * 30, True)])
def test_concatenated_part(text, ref16):
    d = decode(deliver_pdu("Shop", text, concat=(300 if ref16 else 54, 3, 2), ref16=ref16))
    assert d.concat == (300 if ref16 else 54, 3, 2)
    assert d.text == text                        # the header and its fill bits are not in the text


def test_truncated_pdu_raises_pdu_error():
    # The daemon catches PDUError; anything else would take its event loop down.
    with pytest.raises(PDUError):
        decode("0004")


def test_voicemail_waiting():
    # In the data coding (0xC8: discard, active, voicemail) ...
    d = decode(deliver_pdu("+447700900123", "", dcs=0xC8))
    assert d.mwi == {"type": "voicemail", "active": True, "count": None, "store": False}
    # ... or as a special SMS indication in the header, with a count.
    d = decode(deliver_pdu("+447700900123", "You have 2 new messages", waiting=(0x80, 2)))
    assert d.mwi == {"type": "voicemail", "active": True, "count": 2, "store": True}
    assert d.text == "You have 2 new messages"
