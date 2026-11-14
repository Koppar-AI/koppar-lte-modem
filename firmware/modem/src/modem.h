// EC25 power control (PWRKEY, RESET_N), its STATUS and RI lines, and the LEDs.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum {
    MODEM_OFF = 0,
    MODEM_POWERING_ON,     // PWRKEY held low
    MODEM_BOOTING,         // PWRKEY released, waiting for STATUS
    MODEM_ON,
    MODEM_POWERING_OFF,    // PWRKEY held low for the off pulse
    MODEM_SHUTTING_DOWN,   // waiting for STATUS to go away
    MODEM_RESETTING,       // RESET_N held low
} modem_state_t;

typedef enum { LED_NET = 0, LED_RING, LED_CALL, LED_COUNT } led_id_t;
typedef enum { LED_AUTO = 0, LED_OFF, LED_ON, LED_SLOW, LED_FAST } led_mode_t;

// Events reported to the control port.
typedef void (*modem_event_fn)(const char *event);

void modem_init(modem_event_fn on_event);
void modem_task(void);
void modem_boot_flash(void);
const char *reset_reason(void);   // what reset the RP2040 last: por, run, watchdog, debug

bool modem_power_on(void);     // false if not in a state to do it
bool modem_power_off(void);
bool modem_reset(void);

modem_state_t modem_state(void);
const char *modem_state_name(modem_state_t s);
bool modem_status_pin_on(void);  // STATUS low = module on
uint32_t modem_ri_count(void);

void led_set_mode(led_id_t led, led_mode_t mode);
led_mode_t led_get_mode(led_id_t led);
const char *led_mode_name(led_mode_t m);
