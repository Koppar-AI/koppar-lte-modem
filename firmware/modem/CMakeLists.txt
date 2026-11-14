cmake_minimum_required(VERSION 3.13)

# Koppar LTE modem, rev B: the RP2040 firmware.
#
# One USB device behind the board's hub with two functions:
#   - a USB sound card (UAC2, mono, 16-bit) that carries call audio to and
#     from the EC25's PCM interface, through a PIO state machine;
#   - a CDC serial port for control: the module's PWRKEY and RESET_N, the
#     status LEDs, audio mode and counters.

set(PICO_BOARD none)
set(PICO_PLATFORM rp2040)
include(pico_sdk_import.cmake)

project(koppar_modem C CXX ASM)
set(CMAKE_C_STANDARD 11)

pico_sdk_init()

# Must match the module's AT+QDAI <sample> field (0 = 8 kHz, 1 = 16 kHz).
set(KOPPAR_SAMPLE_RATE 8000 CACHE STRING "Call audio sample rate, 8000 or 16000")
set(KOPPAR_FW_VERSION "0.2.1")

add_executable(koppar_modem
    src/main.c
    src/usb_descriptors.c
    src/audio.c
    src/pcm.c
    src/modem.c
    src/control.c
    src/settings.c
    src/fwupdate.c
)
pico_generate_pio_header(koppar_modem ${CMAKE_CURRENT_LIST_DIR}/src/pcm.pio)

target_include_directories(koppar_modem PRIVATE src)
target_compile_options(koppar_modem PRIVATE -Wall -Wextra -Wno-unused-parameter)
target_compile_definitions(koppar_modem PRIVATE
    PICO_FLASH_SIZE_BYTES=2097152
    PICO_STACK_SIZE=0x1000
    # RP2040-E5: the USB controller can miss the bus reset after a (re)connect and
    # never enumerate. The SDK's workaround borrows GPIO15 (unconnected on rev B).
    PICO_RP2040_USB_DEVICE_ENUMERATION_FIX=1
    PICO_XOSC_STARTUP_DELAY_MULTIPLIER=64
    KOPPAR_SAMPLE_RATE=${KOPPAR_SAMPLE_RATE}
    KOPPAR_FW_VERSION="${KOPPAR_FW_VERSION}"
)
target_link_libraries(koppar_modem
    pico_stdlib
    pico_unique_id
    pico_bootrom
    tinyusb_device
    hardware_pio
    hardware_flash
    hardware_sync
    hardware_watchdog
)

pico_enable_stdio_usb(koppar_modem 0)
pico_enable_stdio_uart(koppar_modem 0)
pico_add_extra_outputs(koppar_modem)
