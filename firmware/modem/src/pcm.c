// PCM link to the EC25: a PIO slave clocked by the module, serviced by an
// interrupt once per frame (8 or 16 kHz).
//
// Two single-producer / single-consumer rings decouple the PCM clock (the
// module's) from the USB clock (the computer's):
//   downlink: written by the IRQ, read by the main loop (USB mic). TinyUSB's
//             IN flow control absorbs the clock difference on that side.
//   uplink:   written by the main loop (USB speaker), read by the IRQ. The
//             IRQ keeps the fill level near a target by dropping or
//             repeating one sample now and then (a few per second at most
//             for crystal-to-crystal drift, inaudible on a voice call).

#include "pcm.h"

#include "board.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/sync.h"
#include "pcm.pio.h"
#include "pico/stdlib.h"

#define PCM_PIO   pio0
#define PCM_SM    0

#define RING_SIZE 512u                     // samples, power of two
#define RING_MASK (RING_SIZE - 1u)

// Uplink fill levels, in samples. USB delivers rate/1000 samples per 1 ms
// packet, so the level swings by about one packet around its average.
#define SPP        (KOPPAR_SAMPLE_RATE / 1000)
#define UL_TARGET  (3 * SPP)
#define UL_LOW     (2 * SPP)
#define UL_HIGH    (5 * SPP)
#define SLIP_EVERY 16                      // frames between corrections

typedef struct {
    int16_t buf[RING_SIZE];
    volatile uint32_t head;                // written by the producer only
    volatile uint32_t tail;                // written by the consumer only
} ring_t;

static ring_t dl, ul;
static volatile bool ul_flush_req;
static bool ul_primed;
static int16_t ul_last;
static uint32_t slip_count;
static pcm_stats_t stats;

static bool enabled;
static uint offset_prog;
static uint32_t rate_last_frames;
static absolute_time_t rate_next;
static uint32_t rate_fps;

static inline uint32_t ring_count(const ring_t *r) { return r->head - r->tail; }

static inline int16_t ul_next(void) {
    if (ul_flush_req) {
        ul.tail = ul.head;
        ul_flush_req = false;
        ul_primed = false;
    }
    uint32_t n = ring_count(&ul);
    if (!ul_primed) {
        if (n < UL_TARGET) return 0;
        ul_primed = true;
    }
    if (n == 0) {
        stats.ul_underruns++;
        ul_primed = false;                 // re-buffer to the target
        return 0;
    }
    if (++slip_count >= SLIP_EVERY) {
        if (n < UL_LOW) {                  // computer slower than the module
            slip_count = 0;
            stats.ul_slips++;
            return ul_last;                // repeat, leave the ring alone
        }
        if (n > UL_HIGH) {                 // computer faster than the module
            slip_count = 0;
            stats.ul_slips++;
            ul.tail++;                     // drop one
        }
    }
    ul_last = ul.buf[ul.tail & RING_MASK];
    ul.tail++;
    return ul_last;
}

static void __not_in_flash_func(pcm_irq)(void) {
    while (!pio_sm_is_rx_fifo_empty(PCM_PIO, PCM_SM)) {
        int16_t s = (int16_t)(pio_sm_get(PCM_PIO, PCM_SM) & 0xffffu);
        stats.frames++;

        if (ring_count(&dl) < RING_SIZE) {
            dl.buf[dl.head & RING_MASK] = s;
            dl.head++;
        } else {
            stats.dl_overruns++;
        }

        int16_t u = ul_next();
        if (!pio_sm_is_tx_fifo_full(PCM_PIO, PCM_SM))
            pio_sm_put(PCM_PIO, PCM_SM, ((uint32_t)(uint16_t)u) << 16);
    }
}

void pcm_init(void) {
    gpio_init(PIN_PCM_CLK);
    gpio_set_dir(PIN_PCM_CLK, GPIO_IN);
    gpio_init(PIN_PCM_SYNC);
    gpio_set_dir(PIN_PCM_SYNC, GPIO_IN);
    gpio_init(PIN_PCM_OUT);
    gpio_set_dir(PIN_PCM_OUT, GPIO_IN);
    // No pulls on the PCM lines: the TXS0108E drives them while the module
    // is on and its one-shots dislike strong external loads.

    offset_prog = pio_add_program(PCM_PIO, &pcm_slave_program);
    pcm_slave_program_init(PCM_PIO, PCM_SM, offset_prog, PIN_PCM_IN, PIN_PCM_OUT);

    pio_set_irq0_source_enabled(PCM_PIO, pis_sm0_rx_fifo_not_empty, true);
    irq_set_exclusive_handler(PIO0_IRQ_0, pcm_irq);
    irq_set_priority(PIO0_IRQ_0, PICO_HIGHEST_IRQ_PRIORITY);
    rate_next = make_timeout_time_ms(100);
}

void pcm_enable(bool on) {
    if (on == enabled) return;
    enabled = on;
    if (on) {
        pio_sm_restart(PCM_PIO, PCM_SM);
        pio_sm_clear_fifos(PCM_PIO, PCM_SM);
        pio_sm_exec(PCM_PIO, PCM_SM, pio_encode_jmp(offset_prog));
        pio_sm_put(PCM_PIO, PCM_SM, 0);    // first frame: silence
        pio_sm_set_enabled(PCM_PIO, PCM_SM, true);
        irq_set_enabled(PIO0_IRQ_0, true);
    } else {
        irq_set_enabled(PIO0_IRQ_0, false);
        pio_sm_set_enabled(PCM_PIO, PCM_SM, false);
        pio_sm_clear_fifos(PCM_PIO, PCM_SM);
        rate_fps = 0;
    }
}

bool pcm_enabled(void) { return enabled; }

void pcm_task(void) {
    if (absolute_time_diff_us(rate_next, get_absolute_time()) < 0) return;
    rate_next = make_timeout_time_ms(100);
    uint32_t f = stats.frames;
    rate_fps = (f - rate_last_frames) * 10u;
    rate_last_frames = f;
}

bool pcm_active(void) {
    // Within 10 % of the nominal rate: a real clock, not a floating input.
    return rate_fps > KOPPAR_SAMPLE_RATE * 9u / 10u && rate_fps < KOPPAR_SAMPLE_RATE * 11u / 10u;
}

uint32_t pcm_frame_rate(void) { return rate_fps; }

uint32_t pcm_downlink_read(int16_t *dst, uint32_t max) {
    uint32_t n = ring_count(&dl);
    if (n > max) n = max;
    for (uint32_t i = 0; i < n; i++) dst[i] = dl.buf[(dl.tail + i) & RING_MASK];
    dl.tail += n;
    return n;
}

void pcm_downlink_flush(void) {
    // The consumer owns the tail: catching up to the head is safe here.
    dl.tail = dl.head;
}

uint32_t pcm_uplink_write(const int16_t *src, uint32_t n) {
    uint32_t space = RING_SIZE - ring_count(&ul);
    if (n > space) n = space;
    for (uint32_t i = 0; i < n; i++) ul.buf[(ul.head + i) & RING_MASK] = src[i];
    ul.head += n;
    return n;
}

void pcm_uplink_flush(void) {
    // The IRQ owns the uplink tail; ask it. With no PCM clock it never runs,
    // so also drop the head back when the link is idle.
    ul_flush_req = true;
    if (!pcm_active()) {
        uint32_t irq = save_and_disable_interrupts();
        ul.tail = ul.head;
        ul_primed = false;
        ul_flush_req = false;
        restore_interrupts(irq);
    }
}

void pcm_get_stats(pcm_stats_t *out) {
    uint32_t irq = save_and_disable_interrupts();
    *out = stats;
    restore_interrupts(irq);
}
