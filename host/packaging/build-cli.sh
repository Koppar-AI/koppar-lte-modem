#!/bin/sh
# Build the standalone CLI: one folder with its own Python, nothing to install.
#   PYTHON=/path/to/python packaging/build-cli.sh   -> dist/koppar-lte-modem/koppar-lte-modem
# The Python needs pyinstaller and this package with [audio] installed.
# macOS: KOPPAR_LIBUSB_DYLIB=/path/to/libusb-1.0.0.dylib (bundled; the EC25's AT port goes through libusb).
set -eu
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
set -- --noconfirm --clean --onedir --name koppar-lte-modem \
    --distpath dist --workpath build/pyinstaller --specpath build/pyinstaller \
    --collect-data koppar_lte_modem --hidden-import usb.backend.libusb1
if [ "$(uname)" = Darwin ]; then
    : "${KOPPAR_LIBUSB_DYLIB:?set KOPPAR_LIBUSB_DYLIB to a libusb-1.0 dylib to bundle}"
    set -- "$@" --add-binary "$KOPPAR_LIBUSB_DYLIB:." --osx-bundle-identifier ai.koppar.lte-modem.cli
fi
"$PY" -m PyInstaller "$@" packaging/entry.py
./dist/koppar-lte-modem/koppar-lte-modem --help > /dev/null
echo "built dist/koppar-lte-modem"
