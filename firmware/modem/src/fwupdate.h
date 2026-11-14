// Firmware update over the control port, without the ROM bootloader.
//
// The ROM's USB bootloader has no fix for RP2040-E5 and sometimes never
// enumerates behind the board's hub; this path avoids it:
//   fw begin <size> <crc32>   erase the staging area (upper 1 MB of flash)
//   fw w <offset> <hex>       write a chunk (sequential offsets)
//   fw commit                 check the CRC, copy staging over the running
//                             image from RAM, reboot into it
// A power cut during the copy (about 1.5 s for a 100 kB image) leaves no bootable image:
// recover with the bootloader (U6 pin 1 to pin 4 on boards without BOOT pads).
#pragma once
#include <stdbool.h>
#include <stdint.h>

const char *fwupdate_begin(uint32_t size, uint32_t crc);              // NULL on success, else the reason
const char *fwupdate_write(uint32_t offset, const char *hex);
const char *fwupdate_commit(void);   // checks the image; NULL = ready
void fwupdate_apply(void);           // after replying OK: copy and reboot (does not return)
