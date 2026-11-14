// Settings kept in the last 4 kB sector of the flash.
#pragma once
#include <stdbool.h>

typedef struct {
    bool autopower;   // power the module on at boot
} settings_t;

void settings_load(settings_t *s);
bool settings_save(const settings_t *s);
