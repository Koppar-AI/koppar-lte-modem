// Koppar LTE modem, rev B: bring-up firmware.
//
// Works with or without the EC25 fitted. Opens a USB serial port and takes
// single-key commands (press ? for the list). Pin map from the rev B netlist:
//
//   GPIO16..19  PCM_CLK, PCM_SYNC, PCM_IN, PCM_OUT   (via TXS0108E, 3.3 V side)
//   GPIO20, 21  RI, STATUS                          (via TXS0108E, 3.3 V side)
//   GPIO22      PWRKEY gate  (Q1, 2N7002: high = PWRKEY pulled low)
//   GPIO23      RESET_N gate (Q2, 2N7002: high = RESET_N pulled low)
//   GPIO24      NET  LED, green (active high)
//   GPIO25      RING LED, amber (active high)
//   GPIO26      CALL LED, blue  (active high)

#include <stdio.h>
#include <string.h>

#include "pico/stdlib.h"
#include "pico/unique_id.h"
#include "pico/bootrom.h"
#include "hardware/flash.h"
#include "hardware/adc.h"
#include "hardware/clocks.h"
#include "hardware/sync.h"

#define PIN_PCM_CLK   16
#define PIN_PCM_SYNC  17
#define PIN_PCM_IN    18
#define PIN_PCM_OUT   19
#define PIN_RI        20
#define PIN_STATUS    21
#define PIN_PWRKEY_G  22
#define PIN_RESET_G   23
#define PIN_LED_NET   24
#define PIN_LED_RING  25
#define PIN_LED_CALL  26

static const struct { uint pin; const char *name; } leds[] = {
    { PIN_LED_NET,  "NET  (green, D2)" },
    { PIN_LED_RING, "RING (amber, D3)" },
    { PIN_LED_CALL, "CALL (blue,  D5)" },
};

static const struct { uint pin; const char *name; } inputs[] = {
    { PIN_PCM_CLK,  "PCM_CLK " }, { PIN_PCM_SYNC, "PCM_SYNC" },
    { PIN_PCM_IN,   "PCM_IN  " }, { PIN_PCM_OUT,  "PCM_OUT " },
    { PIN_RI,       "RI      " }, { PIN_STATUS,   "STATUS  " },
};

static bool heartbeat = true;

static void out_pin(uint pin) {
    gpio_init(pin);
    gpio_put(pin, 0);
    gpio_set_dir(pin, GPIO_OUT);
}

static void pins_init(void) {
    // Gates first: the 100k pull-downs already hold them off, keep it that way.
    out_pin(PIN_PWRKEY_G);
    out_pin(PIN_RESET_G);
    for (size_t i = 0; i < count_of(leds); i++) out_pin(leds[i].pin);
    for (size_t i = 0; i < count_of(inputs); i++) {
        gpio_init(inputs[i].pin);
        gpio_set_dir(inputs[i].pin, GPIO_IN);
        gpio_pull_down(inputs[i].pin);
    }
}

static void leds_all(bool on) {
    for (size_t i = 0; i < count_of(leds); i++) gpio_put(leds[i].pin, on);
}

static void led_chase(int rounds, int ms) {
    for (int r = 0; r < rounds; r++)
        for (size_t i = 0; i < count_of(leds); i++) {
            gpio_put(leds[i].pin, 1);
            sleep_ms(ms);
            gpio_put(leds[i].pin, 0);
        }
}

static void read_jedec_id(uint8_t out[3]) {
    uint8_t tx[4] = { 0x9f, 0, 0, 0 }, rx[4] = { 0 };
    uint32_t irq = save_and_disable_interrupts();
    flash_do_cmd(tx, rx, sizeof tx);
    restore_interrupts(irq);
    memcpy(out, rx + 1, 3);
}

static float core_temp_c(void) {
    adc_select_input(4);
    float v = adc_read() * 3.3f / 4096.0f;
    return 27.0f - (v - 0.706f) / 0.001721f;
}

static void cmd_info(void) {
    char uid[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
    pico_get_unique_board_id_string(uid, sizeof uid);
    uint8_t id[3];
    read_jedec_id(id);
    uint32_t xosc_khz = frequency_count_khz(CLOCKS_FC0_SRC_VALUE_XOSC_CLKSRC);

    printf("\n== Koppar LTE modem rev B, bring-up ==\n");
    printf("flash JEDEC ID : %02x %02x %02x  %s\n", id[0], id[1], id[2],
           (id[0] == 0xef && id[1] == 0x40 && id[2] == 0x15) ? "(Winbond W25Q16JV, OK)"
                                                             : "(UNEXPECTED: want ef 40 15)");
    printf("flash uid      : %s\n", uid);
    printf("crystal (XOSC) : %lu kHz  %s\n", (unsigned long)xosc_khz,
           (xosc_khz > 11950 && xosc_khz < 12050) ? "(OK)" : "(UNEXPECTED: want 12000)");
    printf("clk_sys        : %lu kHz\n", (unsigned long)(clock_get_hz(clk_sys) / 1000));
    printf("core temp      : %.1f C\n", core_temp_c());
    printf("PWRKEY gate    : %s\n", gpio_get_out_level(PIN_PWRKEY_G) ? "ON (PWRKEY held low)" : "off");
    printf("RESET_N gate   : %s\n", gpio_get_out_level(PIN_RESET_G) ? "ON (RESET_N held low)" : "off");
    printf("inputs (3V3 side of the TXS0108E, pulled down):\n");
    for (size_t i = 0; i < count_of(inputs); i++)
        printf("  GPIO%-2u %s = %d\n", inputs[i].pin, inputs[i].name, gpio_get(inputs[i].pin));
}

static void cmd_led_test(void) {
    bool hb = heartbeat;
    heartbeat = false;
    leds_all(0);
    for (size_t i = 0; i < count_of(leds); i++) {
        printf("GPIO%u %s on for 2 s\n", leds[i].pin, leds[i].name);
        gpio_put(leds[i].pin, 1);
        sleep_ms(2000);
        gpio_put(leds[i].pin, 0);
    }
    heartbeat = hb;
}

static void toggle_gate(uint pin, const char *name) {
    bool on = !gpio_get_out_level(pin);
    gpio_put(pin, on);
    printf("%s gate (GPIO%u) %s\n", name, pin, on ? "ON: 2N7002 conducting" : "off");
}

static void help(void) {
    printf("\ncommands:\n"
           "  i  info: flash ID, crystal, temperature, pin states\n"
           "  l  LED test, one at a time\n"
           "  1/2/3  toggle NET / RING / CALL LED (stops the heartbeat)\n"
           "  h  heartbeat on/off\n"
           "  p  toggle PWRKEY gate (Q1)\n"
           "  r  toggle RESET_N gate (Q2)\n"
           "  b  reboot into the USB bootloader (RPI-RP2 drive)\n"
           "  ?  this list\n");
}

int main(void) {
    pins_init();
    stdio_init_all();
    adc_init();
    adc_set_temp_sensor_enabled(true);

    led_chase(2, 200);

    absolute_time_t next_beat = get_absolute_time();
    bool beat = false;
    for (;;) {
        int c = getchar_timeout_us(10 * 1000);
        switch (c) {
        case PICO_ERROR_TIMEOUT: break;
        case 'i': cmd_info(); break;
        case 'l': cmd_led_test(); break;
        case '1': case '2': case '3': {
            heartbeat = false;
            uint pin = leds[c - '1'].pin;
            gpio_put(pin, !gpio_get_out_level(pin));
            printf("%s %s\n", leds[c - '1'].name, gpio_get_out_level(pin) ? "on" : "off");
            break;
        }
        case 'h':
            heartbeat = !heartbeat;
            if (!heartbeat) gpio_put(PIN_LED_NET, 0);
            printf("heartbeat %s\n", heartbeat ? "on" : "off");
            break;
        case 'p': toggle_gate(PIN_PWRKEY_G, "PWRKEY"); break;
        case 'r': toggle_gate(PIN_RESET_G, "RESET_N"); break;
        case 'b':
            printf("rebooting into the bootloader\n");
            sleep_ms(100);
            leds_all(0);
            reset_usb_boot(0, 0);
            break;
        case '?': case '\r': case '\n': help(); break;
        default: printf("unknown '%c', press ? for help\n", c); break;
        }

        if (heartbeat && absolute_time_diff_us(next_beat, get_absolute_time()) >= 0) {
            beat = !beat;
            gpio_put(PIN_LED_NET, beat);
            next_beat = make_timeout_time_ms(beat ? 100 : 900);
        }
    }
}
