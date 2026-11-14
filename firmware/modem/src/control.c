#include "control.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio.h"
#include "fwupdate.h"
#include "board.h"
#include "modem.h"
#include "pcm.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"
#include "settings.h"
#include "tusb.h"

extern volatile uint32_t main_loops;

static char line[384];   // "fw w <offset> <256 hex digits>" fits
static size_t line_len;

static void out(const char *fmt, ...) {
    if (!tud_cdc_connected()) return;
    char buf[160];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf - 2, fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n > sizeof buf - 3) n = sizeof buf - 3;
    buf[n++] = '\r';
    buf[n++] = '\n';
    // Never block or re-enter the USB stack here: if the host is not reading
    // and the 1 kB TX buffer is full, the rest of the line is dropped.
    tud_cdc_write(buf, (uint32_t)n);
    tud_cdc_write_flush();
}

void control_event(const char *event) { out("! %s", event); }

void control_init(void) { line_len = 0; }

// Leave USB cleanly before the ROM bootloader takes over: drop the pull-up
// long enough for the hub and host to see a detach, so they enumerate the
// bootloader as a new device instead of talking to the old address.
static void enter_bootloader(void) {
    sleep_ms(50);                          // let the OK reach the host
    tud_disconnect();
    sleep_ms(300);
    watchdog_disable();                    // the ROM bootloader doesn't feed it
    // The ROM bootloader drives the NET LED (green) as its activity light:
    // lit = the bootloader is running, even if the computer fails to see it.
    reset_usb_boot(1u << PIN_LED_NET, 0);
}

static const char *const led_names[LED_COUNT] = {"net", "ring", "call"};

static int parse_led(const char *s) {
    for (int i = 0; i < LED_COUNT; i++)
        if (!strcmp(s, led_names[i])) return i;
    return -1;
}

static int parse_led_mode(const char *s) {
    for (int m = LED_AUTO; m <= LED_FAST; m++)
        if (!strcmp(s, led_mode_name((led_mode_t)m))) return m;
    return -1;
}

static void cmd_status(void) {
    pcm_stats_t st;
    pcm_get_stats(&st);
    settings_t cfg;
    settings_load(&cfg);
    out("fw=%s rate=%u", KOPPAR_FW_VERSION, (unsigned)KOPPAR_SAMPLE_RATE);
    out("module=%s status_pin=%s ri_count=%lu", modem_state_name(modem_state()),
        modem_status_pin_on() ? "on" : "off", (unsigned long)modem_ri_count());
    out("pcm=%s pio=%s fps=%lu frames=%lu", pcm_active() ? "active" : "idle", pcm_enabled() ? "on" : "off",
        (unsigned long)pcm_frame_rate(), (unsigned long)st.frames);
    out("audio=%s spk=%s mic=%s volume_db=%d muted=%d", audio_mode_name(audio_get_mode()),
        audio_spk_streaming() ? "streaming" : "idle", audio_mic_streaming() ? "streaming" : "idle",
        audio_spk_volume_db(), audio_spk_muted());
    out("ul_underruns=%lu ul_slips=%lu dl_overruns=%lu", (unsigned long)st.ul_underruns,
        (unsigned long)st.ul_slips, (unsigned long)st.dl_overruns);
    out("led_net=%s led_ring=%s led_call=%s", led_mode_name(led_get_mode(LED_NET)),
        led_mode_name(led_get_mode(LED_RING)), led_mode_name(led_get_mode(LED_CALL)));
    out("autopower=%d reset=%s uptime_s=%lu loops=%lu", cfg.autopower, reset_reason(),
        (unsigned long)(to_ms_since_boot(get_absolute_time()) / 1000), (unsigned long)main_loops);
}

static void help(void) {
    out("version                 firmware version and sample rate");
    out("status                  module state, PCM link, audio, counters");
    out("power on|off|reset      PWRKEY on / off pulse, RESET_N pulse");
    out("autopower [on|off]      power the module on at boot (saved)");
    out("audio [pcm|loopback|tone]  route: the call / speaker->mic / 1 kHz tone");
    out("led net|ring|call auto|on|off|slow|fast");
    out("fw begin|w|commit       firmware update over this port (koppar-lte-modem flash)");
    out("bootloader              reboot into the USB bootloader (RPI-RP2)");
}

static void execute(char *cmd) {
    char *argv[4] = {0};
    int argc = 0;
    for (char *tok = strtok(cmd, " \t"); tok && argc < 4; tok = strtok(NULL, " \t")) argv[argc++] = tok;
    if (argc == 0) return;
    const char *c = argv[0];

    if (!strcmp(c, "help") || !strcmp(c, "?")) {
        help();
    } else if (!strcmp(c, "version")) {
        out("koppar-modem %s rate=%u fwupdate=1", KOPPAR_FW_VERSION, (unsigned)KOPPAR_SAMPLE_RATE);
    } else if (!strcmp(c, "status")) {
        cmd_status();
    } else if (!strcmp(c, "power") && argc == 2) {
        bool ok;
        if (!strcmp(argv[1], "on")) ok = modem_power_on();
        else if (!strcmp(argv[1], "off")) ok = modem_power_off();
        else if (!strcmp(argv[1], "reset")) ok = modem_reset();
        else { out("ERR power on|off|reset"); return; }
        if (!ok) { out("ERR module is %s", modem_state_name(modem_state())); return; }
    } else if (!strcmp(c, "autopower")) {
        settings_t cfg;
        settings_load(&cfg);
        if (argc == 2) {
            if (pcm_active()) { out("ERR not during a call"); return; }
            cfg.autopower = !strcmp(argv[1], "on");
            if (!settings_save(&cfg)) { out("ERR flash write failed"); return; }
        }
        out("autopower=%d", cfg.autopower);
    } else if (!strcmp(c, "audio")) {
        if (argc == 2) {
            if (!strcmp(argv[1], "pcm")) audio_set_mode(AUDIO_MODE_PCM);
            else if (!strcmp(argv[1], "loopback")) audio_set_mode(AUDIO_MODE_LOOPBACK);
            else if (!strcmp(argv[1], "tone")) audio_set_mode(AUDIO_MODE_TONE);
            else { out("ERR audio pcm|loopback|tone"); return; }
        }
        out("audio=%s", audio_mode_name(audio_get_mode()));
    } else if (!strcmp(c, "led") && argc == 3) {
        int led = parse_led(argv[1]), mode = parse_led_mode(argv[2]);
        if (led < 0 || mode < 0) { out("ERR led net|ring|call auto|on|off|slow|fast"); return; }
        led_set_mode((led_id_t)led, (led_mode_t)mode);
    } else if (!strcmp(c, "fw") && argc >= 2) {
        const char *err = "fw begin <size> <crc32> | fw w <offset> <hex> | fw commit";
        if (pcm_active()) err = "not during a call";
        else if (!strcmp(argv[1], "begin") && argc == 4)
            err = fwupdate_begin(strtoul(argv[2], NULL, 0), strtoul(argv[3], NULL, 16));
        else if (!strcmp(argv[1], "w") && argc == 4)
            err = fwupdate_write(strtoul(argv[2], NULL, 16), argv[3]);
        else if (!strcmp(argv[1], "commit") && argc == 2) {
            err = fwupdate_commit();
            if (!err) {
                out("OK");
                fwupdate_apply();          // does not return
            }
        }
        if (err) { out("ERR %s", err); return; }
    } else if (!strcmp(c, "bootloader")) {
        out("OK");
        enter_bootloader();
    } else {
        out("ERR unknown command '%s' (try help)", c);
        return;
    }
    out("OK");
}

void control_task(void) {
    while (tud_cdc_available()) {
        int ch = tud_cdc_read_char();
        if (ch < 0) break;
        if (ch == '\r' || ch == '\n') {
            if (line_len) {
                line[line_len] = 0;
                line_len = 0;
                execute(line);
            }
        } else if (line_len < sizeof line - 1) {
            line[line_len++] = (char)ch;
        }
    }
}

// Opening the port at 1200 baud reboots into the bootloader (picotool / IDE convention).
void tud_cdc_line_coding_cb(uint8_t itf, cdc_line_coding_t const *coding) {
    if (coding->bit_rate == 1200) enter_bootloader();
}
