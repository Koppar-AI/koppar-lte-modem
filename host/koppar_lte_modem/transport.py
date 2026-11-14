"""Byte transports to the EC25's AT interface.

macOS has no driver for the EC25's vendor-specific serial interfaces, so on a
Mac the AT interface is opened directly with libusb (pyusb). On Linux the
`option` driver exposes it as /dev/ttyUSB2 and pyserial is used.

EC25 USB interfaces (VID 0x2C7C, PID 0x0125): 0 DM (diagnostics), 1 NMEA,
2 AT, 3 modem (PPP, also takes AT), 4+ network (QMI / ECM).
"""

from __future__ import annotations

import ctypes.util
import os
import sys

EC25_VID = 0x2C7C
EC25_PID = 0x0125
AT_INTERFACE = 2


class TransportError(Exception):
    pass


class Transport:
    name = "?"

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, timeout: float) -> bytes:
        """Return whatever arrived within `timeout` seconds (possibly b"")."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialTransport(Transport):
    def __init__(self, path: str, baudrate: int = 115200):
        import serial

        self.name = path
        try:
            self._s = serial.Serial(path, baudrate, timeout=0.05)
        except serial.SerialException as e:
            raise TransportError(str(e)) from e

    def write(self, data: bytes) -> None:
        import serial

        try:
            self._s.write(data)
            self._s.flush()
        except (serial.SerialException, OSError) as e:
            raise TransportError(str(e)) from e

    def read(self, timeout: float) -> bytes:
        import serial

        try:
            self._s.timeout = timeout
            first = self._s.read(1)
            if not first:
                return b""
            return first + self._s.read(self._s.in_waiting)
        except (serial.SerialException, OSError, TypeError) as e:
            raise TransportError(str(e)) from e

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass


def _libusb_backend():
    import usb.backend.libusb1

    def find(name):
        found = ctypes.util.find_library(name)
        if found:
            return found
        # Homebrew on Apple Silicon is not on the default dyld search path.
        for p in ("/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"):
            if os.path.exists(p):
                return p
        return None

    backend = usb.backend.libusb1.get_backend(find_library=find)
    if backend is None:
        raise TransportError("libusb not found: install it (macOS: brew install libusb)")
    return backend


def ec25_present() -> bool:
    try:
        import usb.core

        return usb.core.find(idVendor=EC25_VID, idProduct=EC25_PID, backend=_libusb_backend()) is not None
    except Exception:
        return False


class UsbTransport(Transport):
    """Bulk endpoints of one EC25 interface, through libusb."""

    def __init__(self, interface: int = AT_INTERFACE):
        import usb.core
        import usb.util

        self._usb = usb
        dev = usb.core.find(idVendor=EC25_VID, idProduct=EC25_PID, backend=_libusb_backend())
        if dev is None:
            raise TransportError("EC25 not found on USB (is the module powered on? try: koppar-lte-modem power on)")
        self.name = f"usb:{dev.bus}-{dev.address}:if{interface}"
        try:
            cfg = dev.get_active_configuration()
            intf = cfg[(interface, 0)]
        except Exception as e:
            raise TransportError(f"EC25 has no interface {interface}: {e}") from e
        try:
            if dev.is_kernel_driver_active(interface):
                dev.detach_kernel_driver(interface)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(dev, interface)
        except usb.core.USBError as e:
            raise TransportError(f"cannot claim EC25 interface {interface} (in use by another program?): {e}") from e

        def ep(direction):
            return usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == direction
                and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
            )

        self._in = ep(usb.util.ENDPOINT_IN)
        self._out = ep(usb.util.ENDPOINT_OUT)
        if self._in is None or self._out is None:
            raise TransportError(f"EC25 interface {interface} has no bulk endpoints")
        self._dev, self._intf = dev, interface
        # Assert DTR, as the Linux driver does; some Qualcomm ports stay quiet without it.
        try:
            dev.ctrl_transfer(0x21, 0x22, 0x0001, interface, None, timeout=500)
        except usb.core.USBError:
            pass

    def write(self, data: bytes) -> None:
        try:
            self._out.write(data, timeout=2000)
        except self._usb.core.USBError as e:
            raise TransportError(str(e)) from e

    def read(self, timeout: float) -> bytes:
        try:
            return bytes(self._in.read(self._in.wMaxPacketSize * 4, timeout=max(1, int(timeout * 1000))))
        except self._usb.core.USBTimeoutError:
            return b""
        except self._usb.core.USBError as e:
            if getattr(e, "errno", None) in (60, 110) or "timed out" in str(e).lower():
                return b""
            raise TransportError(str(e)) from e

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self._dev, self._intf)
            self._usb.util.dispose_resources(self._dev)
        except Exception:
            pass


def find_linux_at_port() -> str | None:
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == EC25_VID and p.pid == EC25_PID and p.location and p.location.endswith(f".{AT_INTERFACE}"):
            return p.device
    return None


def open_at_transport(path: str | None = None) -> Transport:
    """Open the AT interface: an explicit path, $KOPPAR_AT_PORT, a Linux tty, or libusb."""
    path = path or os.environ.get("KOPPAR_AT_PORT")
    if path:
        return SerialTransport(path)
    if sys.platform.startswith("linux"):
        tty = find_linux_at_port()
        if tty:
            return SerialTransport(tty)
    return UsbTransport(AT_INTERFACE)
