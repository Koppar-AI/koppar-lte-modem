#include "fwupdate.h"

#include <string.h>

#include "hardware/flash.h"
#include "hardware/structs/psm.h"
#include "hardware/structs/watchdog.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"
#include "tusb.h"

#define STAGING_OFFSET  (1024u * 1024u)                                 // upper half of the 2 MB flash
#define MAX_IMAGE       (PICO_FLASH_SIZE_BYTES - STAGING_OFFSET - FLASH_SECTOR_SIZE)   // keep the settings sector

static uint32_t img_size, img_crc, written;
static bool active;
static uint8_t page[FLASH_PAGE_SIZE];
static uint32_t page_base = UINT32_MAX;
static uint8_t sector_buf[FLASH_SECTOR_SIZE];

static uint32_t crc32_update(uint32_t crc, const uint8_t *p, uint32_t n) {
    crc = ~crc;
    while (n--) {
        crc ^= *p++;
        for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

static void flush_page(void) {
    if (page_base == UINT32_MAX) return;
    uint32_t irq = save_and_disable_interrupts();
    flash_range_program(STAGING_OFFSET + page_base, page, FLASH_PAGE_SIZE);
    restore_interrupts(irq);
    page_base = UINT32_MAX;
}

const char *fwupdate_begin(uint32_t size, uint32_t crc) {
    if (size == 0 || size > MAX_IMAGE) return "bad size";
    img_size = size;
    img_crc = crc;
    written = 0;
    page_base = UINT32_MAX;
    uint32_t span = (size + FLASH_SECTOR_SIZE - 1) & ~(FLASH_SECTOR_SIZE - 1);
    for (uint32_t off = 0; off < span; off += FLASH_SECTOR_SIZE) {
        uint32_t irq = save_and_disable_interrupts();
        flash_range_erase(STAGING_OFFSET + off, FLASH_SECTOR_SIZE);   // ~45 ms each, USB NAKs meanwhile
        restore_interrupts(irq);
        tud_task();
    }
    active = true;
    return NULL;
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

const char *fwupdate_write(uint32_t offset, const char *hex) {
    if (!active) return "no update in progress";
    if (offset != written) return "out of order";
    size_t n = strlen(hex);
    if (n == 0 || n % 2) return "bad hex";
    if (offset + n / 2 > img_size) return "past the end";
    for (size_t i = 0; i < n; i += 2) {
        int hi = hexval(hex[i]), lo = hexval(hex[i + 1]);
        if (hi < 0 || lo < 0) return "bad hex";
        uint32_t at = offset + (uint32_t)(i / 2);
        uint32_t base = at & ~(FLASH_PAGE_SIZE - 1u);
        if (base != page_base) {
            flush_page();
            memset(page, 0xff, sizeof page);
            page_base = base;
        }
        page[at - base] = (uint8_t)(hi << 4 | lo);
    }
    written += (uint32_t)(n / 2);
    return NULL;
}

// Runs from RAM: it overwrites the flash the rest of the firmware lives in.
// Nothing here may call into flash (no memcpy: the copy loop is written out).
static void __no_inline_not_in_flash_func(copy_and_reboot)(uint32_t size) {
    (void)save_and_disable_interrupts();
    for (uint32_t off = 0; off < size; off += FLASH_SECTOR_SIZE) {
        const volatile uint8_t *src = (const volatile uint8_t *)(XIP_BASE + STAGING_OFFSET + off);
        for (uint32_t i = 0; i < FLASH_SECTOR_SIZE; i++) sector_buf[i] = src[i];
        flash_range_erase(off, FLASH_SECTOR_SIZE);
        flash_range_program(off, sector_buf, FLASH_SECTOR_SIZE);
    }
    // Full chip reset into the normal boot path.
    watchdog_hw->scratch[4] = 0;
    hw_set_bits(&psm_hw->wdsel, PSM_WDSEL_BITS & ~(PSM_WDSEL_ROSC_BITS | PSM_WDSEL_XOSC_BITS));
    hw_set_bits(&watchdog_hw->ctrl, WATCHDOG_CTRL_TRIGGER_BITS);
    for (;;) __asm volatile("nop");
}

const char *fwupdate_commit(void) {
    if (!active) return "no update in progress";
    flush_page();
    if (written != img_size) return "incomplete";
    uint32_t crc = crc32_update(0, (const uint8_t *)(XIP_BASE + STAGING_OFFSET), img_size);
    if (crc != img_crc) {
        active = false;
        return "crc mismatch";
    }
    active = false;
    // Let the OK reach the host and detach cleanly before the copy.
    return NULL;
}

void fwupdate_apply(void) {
    sleep_ms(50);
    tud_disconnect();
    sleep_ms(300);
    copy_and_reboot(img_size);
}
