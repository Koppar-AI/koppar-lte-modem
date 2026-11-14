#include "settings.h"

#include <string.h>

#include "hardware/flash.h"
#include "hardware/sync.h"
#include "pico/stdlib.h"

#define SETTINGS_OFFSET (PICO_FLASH_SIZE_BYTES - FLASH_SECTOR_SIZE)
#define SETTINGS_MAGIC  0x4b505231u   // "KPR1"

typedef struct {
    uint32_t magic;
    uint32_t autopower;
    uint32_t check;
} stored_t;

static uint32_t check_of(const stored_t *s) { return s->magic ^ s->autopower ^ 0xa5a5a5a5u; }

void settings_load(settings_t *s) {
    const stored_t *st = (const stored_t *)(XIP_BASE + SETTINGS_OFFSET);
    memset(s, 0, sizeof *s);
    if (st->magic == SETTINGS_MAGIC && st->check == check_of(st)) s->autopower = st->autopower != 0;
}

bool settings_save(const settings_t *s) {
    static uint8_t page[FLASH_PAGE_SIZE];
    stored_t st = {.magic = SETTINGS_MAGIC, .autopower = s->autopower};
    st.check = check_of(&st);
    memset(page, 0xff, sizeof page);
    memcpy(page, &st, sizeof st);

    // Code runs from this flash: no interrupts (USB, PCM) while it is busy.
    // A save stalls everything for ~50 ms, so it is never done during a call.
    uint32_t irq = save_and_disable_interrupts();
    flash_range_erase(SETTINGS_OFFSET, FLASH_SECTOR_SIZE);
    flash_range_program(SETTINGS_OFFSET, page, FLASH_PAGE_SIZE);
    restore_interrupts(irq);

    settings_t back;
    settings_load(&back);
    return back.autopower == s->autopower;
}
