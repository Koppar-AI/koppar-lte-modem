// Koppar LTE modem, rev B: RP2040 firmware.
//
// USB sound card for call audio (EC25 PCM <-> UAC2) and a control port for
// the module's power, the LEDs and diagnostics. See ../README.md.

#include "audio.h"
#include "control.h"
#include "modem.h"
#include "pcm.h"
#include "pico/stdlib.h"
#include "settings.h"
#include "tusb.h"

volatile uint32_t main_loops;

// If the host has not configured us a few seconds after we attached (a missed
// bus reset, a hub that didn't notice the reconnect), detach and attach again.
static void usb_watch_task(void) {
    static absolute_time_t deadline;
    static bool detached;
    static bool armed;
    if (!armed) {
        deadline = make_timeout_time_ms(3000);
        armed = true;
    }
    // Mounted stays true through a legitimate suspend (the computer asleep).
    // An unconfigured device on a port the host has given up on also sees no
    // traffic, which looks like suspend: that case must still retry.
    if (tud_mounted()) {
        deadline = make_timeout_time_ms(3000);
        return;
    }
    if (absolute_time_diff_us(deadline, get_absolute_time()) < 0) return;
    if (!detached) {
        tud_disconnect();
        detached = true;
        deadline = make_timeout_time_ms(500);
    } else {
        tud_connect();
        detached = false;
        deadline = make_timeout_time_ms(3000);
    }
}   // reported by `status`: a stall shows up here

int main(void) {
    modem_init(control_event);   // pins first: PWRKEY and RESET_N stay released
    modem_boot_flash();          // all three LEDs for 300 ms: the firmware started
    pcm_init();
    audio_init();
    control_init();

    tusb_rhport_init_t dev_init = {.role = TUSB_ROLE_DEVICE, .speed = TUSB_SPEED_AUTO};
    tusb_init(0, &dev_init);

    settings_t cfg;
    settings_load(&cfg);
    if (cfg.autopower) modem_power_on();

    // No watchdog: a watchdog reset left the board off the bus until it was
    // replugged (it kept running in the ROM bootloader after `bootloader`).
    for (;;) {
        main_loops++;
        tud_task();
        usb_watch_task();
        pcm_task();
        audio_task();
        modem_task();
        control_task();
    }
}
