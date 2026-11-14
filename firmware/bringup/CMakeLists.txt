cmake_minimum_required(VERSION 3.13)

# Bring-up firmware for the Koppar LTE modem, rev B. Runs without the EC25:
# checks the flash, the crystal, the LEDs and the PWRKEY / RESET_N gates, and
# takes single-key commands over a USB serial port.

set(PICO_BOARD none)
set(PICO_PLATFORM rp2040)
# W25Q16JVSS on the board: the SDK's default W25Q080 stage 2 (quad XIP) suits it.
include(pico_sdk_import.cmake)

project(koppar_bringup C CXX ASM)
set(CMAKE_C_STANDARD 11)

pico_sdk_init()

add_executable(bringup main.c)
target_compile_options(bringup PRIVATE -Wall -Wextra -Wno-unused-parameter)
target_compile_definitions(bringup PRIVATE
    PICO_FLASH_SIZE_BYTES=2097152
    PICO_XOSC_STARTUP_DELAY_MULTIPLIER=64
    PICO_STDIO_USB_CONNECT_WAIT_TIMEOUT_MS=0
    USBD_MANUFACTURER="Koppar"
    USBD_PRODUCT="LTE modem bring-up"
)
target_link_libraries(bringup
    pico_stdlib
    pico_unique_id
    pico_bootrom
    hardware_flash
    hardware_adc
    hardware_clocks
    hardware_sync
)

pico_enable_stdio_usb(bringup 1)
pico_enable_stdio_uart(bringup 0)
pico_add_extra_outputs(bringup)
