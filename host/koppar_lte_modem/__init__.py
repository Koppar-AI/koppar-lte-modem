"""Computer-side software for the Koppar LTE modem (rev B).

    from koppar_lte_modem import Link
    with Link.open() as link:
        modem = link.modem()
        print(modem.signal())
        modem.send_sms("+447700900123", "hello from the board")
"""

from .at import ATChannel, ATError, ATTimeout
from .link import Link
from .modem import Modem

__all__ = ["ATChannel", "ATError", "ATTimeout", "Link", "Modem"]
__version__ = "0.1.0"
