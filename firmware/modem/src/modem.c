// EC25 power sequencing, from the EC25 hardware design V2.4:
//   on:    VBAT stable >= 30 ms, then PWRKEY low >= 500 ms; release once
//          STATUS goes low (open drain, low = on).
//   off:   PWRKEY low >= 650 ms, then the module shuts down (AT+QPOWD is the
//          gentler way; the host software uses it first).
//   reset: RESET_N low for 150-460 ms; only when the other two fail.
// RI idles high and pulses low for 120 ms on each URC (incoming call, SMS...).
//
// Both control pins are driven through 2N7002s: GPIO high = pin pulled low.
// STATUS and RI come through the TXS0108E, which is disabled while the
// module is off (no VDD_EXT): the RP2040 pull-ups then read "off" and "idle".

#include "modem.h"

#include "audio.h"
#include "board.h"
#include "pcm.h"
#include "hardware/structs/vreg_and_chip_reset.h"
#include "hardware/structs/watchdog.h"
#include "pico/stdlib.h"

#define PWRKEY_ON_MIN_MS    550
#define PWRKEY_ON_MAX_MS    1000
#define BOOT_TIMEOUT_MS     15000
#define PWRKEY_OFF_MS       800
#define SHUTDOWN_TIMEOUT_MS 35000
#define RESET_MS            300
#define VBAT_SETTLE_MS      100

static modem_state_t state = MODEM_OFF;
static absolute_time_t state_since;
static modem_event_fn emit;
static uint32_t ri_count;
static bool ri_last = true;
static absolute_time_t ri_flash_until;
static bool status_last;
static bool pcm_last;

static led_mode_t led_modes[LED_COUNT];
static const char *boot_reason = "?";
static const uint led_pins[LED_COUNT] = {PIN_LED_NET, PIN_LED_RING, PIN_LED_CALL};

static void set_state(modem_state_t s) {
    state = s;
    state_since = get_absolute_time();
}

static uint32_t ms_in_state(void) {
    return (uint32_t)(absolute_time_diff_us(state_since, get_absolute_time()) / 1000);
}

static void pwrkey(bool assert) { gpio_put(PIN_PWRKEY_G, assert); }
static void reset_n(bool assert) { gpio_put(PIN_RESET_G, assert); }

bool modem_status_pin_on(void) { return !gpio_get(PIN_STATUS); }

void modem_init(modem_event_fn on_event) {
    emit = on_event;
    const uint outs[] = {PIN_PWRKEY_G, PIN_RESET_G, PIN_LED_NET, PIN_LED_RING, PIN_LED_CALL};
    for (unsigned i = 0; i < count_of(outs); i++) {
        gpio_init(outs[i]);
        gpio_put(outs[i], 0);
        gpio_set_dir(outs[i], GPIO_OUT);
    }
    gpio_init(PIN_STATUS);
    gpio_set_dir(PIN_STATUS, GPIO_IN);
    gpio_pull_up(PIN_STATUS);
    gpio_init(PIN_RI);
    gpio_set_dir(PIN_RI, GPIO_IN);
    gpio_pull_up(PIN_RI);

    uint32_t cr = vreg_and_chip_reset_hw->chip_reset;
    if (watchdog_hw->reason) boot_reason = "watchdog";
    else if (cr & VREG_AND_CHIP_RESET_CHIP_RESET_HAD_PSM_RESTART_BITS) boot_reason = "debug";
    else if (cr & VREG_AND_CHIP_RESET_CHIP_RESET_HAD_RUN_BITS) boot_reason = "run";
    else if (cr & VREG_AND_CHIP_RESET_CHIP_RESET_HAD_POR_BITS) boot_reason = "por";

    // The module may already be on (the RP2040 was reflashed or reset while
    // the board stayed powered).
    sleep_ms(VBAT_SETTLE_MS);
    status_last = modem_status_pin_on();
    set_state(status_last ? MODEM_ON : MODEM_OFF);
}

bool modem_power_on(void) {
    if (state != MODEM_OFF) return false;
    pwrkey(true);
    set_state(MODEM_POWERING_ON);
    emit("power on");
    return true;
}

bool modem_power_off(void) {
    if (state != MODEM_ON && state != MODEM_BOOTING) return false;
    pwrkey(true);
    set_state(MODEM_POWERING_OFF);
    emit("power off");
    return true;
}

bool modem_reset(void) {
    if (state == MODEM_RESETTING) return false;
    pwrkey(false);
    reset_n(true);
    set_state(MODEM_RESETTING);
    emit("reset");
    return true;
}

modem_state_t modem_state(void) { return state; }

const char *modem_state_name(modem_state_t s) {
    switch (s) {
    case MODEM_OFF: return "off";
    case MODEM_POWERING_ON: return "powering-on";
    case MODEM_BOOTING: return "booting";
    case MODEM_ON: return "on";
    case MODEM_POWERING_OFF: return "powering-off";
    case MODEM_SHUTTING_DOWN: return "shutting-down";
    case MODEM_RESETTING: return "resetting";
    }
    return "?";
}

uint32_t modem_ri_count(void) { return ri_count; }

static void power_task(void) {
    bool on = modem_status_pin_on();
    uint32_t t = ms_in_state();

    switch (state) {
    case MODEM_OFF:
        if (on) {                                  // turned on by something else
            set_state(MODEM_ON);
            emit("status on");
        }
        break;
    case MODEM_POWERING_ON:
        if ((t >= PWRKEY_ON_MIN_MS && on) || t >= PWRKEY_ON_MAX_MS) {
            pwrkey(false);
            set_state(on ? MODEM_ON : MODEM_BOOTING);
            if (on) emit("status on");
        }
        break;
    case MODEM_BOOTING:
        if (on) {
            set_state(MODEM_ON);
            emit("status on");
        } else if (t >= BOOT_TIMEOUT_MS) {
            set_state(MODEM_OFF);
            emit("error boot timeout: STATUS never went low");
        }
        break;
    case MODEM_ON:
        if (!on) {
            set_state(MODEM_OFF);
            emit("status off");
        }
        break;
    case MODEM_POWERING_OFF:
        if (t >= PWRKEY_OFF_MS) {
            pwrkey(false);
            set_state(MODEM_SHUTTING_DOWN);
        }
        break;
    case MODEM_SHUTTING_DOWN:
        if (!on) {
            set_state(MODEM_OFF);
            emit("status off");
        } else if (t >= SHUTDOWN_TIMEOUT_MS) {
            set_state(MODEM_ON);
            emit("error shutdown timeout: STATUS still low");
        }
        break;
    case MODEM_RESETTING:
        if (t >= RESET_MS) {
            reset_n(false);
            set_state(MODEM_BOOTING);
        }
        break;
    }
}

static void signals_task(void) {
    // RI: count falling edges (only meaningful while the module is on).
    bool ri = gpio_get(PIN_RI);
    if (ri_last && !ri && modem_status_pin_on()) {
        ri_count++;
        ri_flash_until = make_timeout_time_ms(300);
        emit("ri");
    }
    ri_last = ri;

    bool pcm = pcm_active();
    if (pcm != pcm_last) {
        pcm_last = pcm;
        emit(pcm ? "pcm on" : "pcm off");
    }
}

static bool blink(uint32_t period_ms, uint32_t on_ms) {
    return (to_ms_since_boot(get_absolute_time()) % period_ms) < on_ms;
}

static bool led_auto(led_id_t id) {
    switch (id) {
    case LED_NET:
        // Off with the module off; fast while starting; slow heartbeat when on.
        // The host turns it solid (LED_ON) once the network is registered.
        if (state == MODEM_OFF) return blink(3000, 20);   // standby: "firmware alive"
        if (state == MODEM_ON) return blink(2000, 100);
        return blink(250, 125);
    case LED_RING:
        return absolute_time_diff_us(get_absolute_time(), ri_flash_until) > 0;
    case LED_CALL:
        return pcm_active() || audio_get_mode() != AUDIO_MODE_PCM;
    default:
        return false;
    }
}

static void led_task(void) {
    for (int i = 0; i < LED_COUNT; i++) {
        bool v;
        switch (led_modes[i]) {
        case LED_OFF: v = false; break;
        case LED_ON: v = true; break;
        case LED_SLOW: v = blink(1000, 500); break;
        case LED_FAST: v = blink(250, 125); break;
        default: v = led_auto((led_id_t)i); break;
        }
        gpio_put(led_pins[i], v);
    }
}

void modem_task(void) {
    power_task();
    pcm_enable(modem_status_pin_on());
    signals_task();
    led_task();
}

void led_set_mode(led_id_t led, led_mode_t mode) {
    if (led < LED_COUNT) led_modes[led] = mode;
}

led_mode_t led_get_mode(led_id_t led) { return led < LED_COUNT ? led_modes[led] : LED_AUTO; }

const char *led_mode_name(led_mode_t m) {
    switch (m) {
    case LED_AUTO: return "auto";
    case LED_OFF: return "off";
    case LED_ON: return "on";
    case LED_SLOW: return "slow";
    case LED_FAST: return "fast";
    }
    return "?";
}

void modem_boot_flash(void) {
    for (int i = 0; i < LED_COUNT; i++) gpio_put(led_pins[i], 1);
    sleep_ms(300);
    for (int i = 0; i < LED_COUNT; i++) gpio_put(led_pins[i], 0);
}

const char *reset_reason(void) { return boot_reason; }
