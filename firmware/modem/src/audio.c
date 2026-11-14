// USB sound card (UAC2): speaker = what the caller hears, microphone = what
// the caller says.
//
// The microphone stream is always fed at the nominal rate, so recording apps
// see a steady device: samples from the PCM link while a call is up, a
// generated signal in LOOPBACK / TONE mode, silence otherwise.

#include "audio.h"

#include <math.h>
#include <string.h>

#include "pcm.h"
#include "pico/stdlib.h"
#include "tusb.h"
#include "usb_descriptors.h"

#define RATE KOPPAR_SAMPLE_RATE

static audio_mode_t mode = AUDIO_MODE_PCM;
static volatile bool spk_streaming, mic_streaming;

// Speaker feature unit: channel 0 (master) and 1.
static bool    spk_mute[2];
static int16_t spk_vol[2];               // 1/256 dB
static int32_t spk_gain_q15 = 32768;

// Loopback ring (speaker -> microphone), same clock domain: no drift handling.
#define LB_SIZE 512u
static int16_t lb_buf[LB_SIZE];
static uint32_t lb_head, lb_tail;

// Generated-signal clock for the microphone when no PCM is flowing.
static uint64_t gen_last_us;
static uint64_t gen_acc;                 // samples * 1e6, fractional carry
static uint32_t tone_phase;

static void update_gain(void) {
    if (spk_mute[0] || spk_mute[1]) {
        spk_gain_q15 = 0;
        return;
    }
    float db = (spk_vol[0] + spk_vol[1]) / 256.0f;
    spk_gain_q15 = (int32_t)(32768.0f * powf(10.0f, db / 20.0f));
}

static inline int16_t apply_gain(int16_t s) {
    int32_t v = (s * spk_gain_q15) >> 15;
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    return (int16_t)v;
}

void audio_init(void) {
    spk_vol[0] = spk_vol[1] = 0;
    update_gain();
    gen_last_us = time_us_64();
}

void audio_set_mode(audio_mode_t m) {
    mode = m;
    lb_head = lb_tail = 0;
    pcm_uplink_flush();
    pcm_downlink_flush();
}

audio_mode_t audio_get_mode(void) { return mode; }

const char *audio_mode_name(audio_mode_t m) {
    switch (m) {
    case AUDIO_MODE_PCM: return "pcm";
    case AUDIO_MODE_LOOPBACK: return "loopback";
    case AUDIO_MODE_TONE: return "tone";
    }
    return "?";
}

bool audio_spk_streaming(void) { return spk_streaming; }
bool audio_mic_streaming(void) { return mic_streaming; }
bool audio_spk_muted(void) { return spk_gain_q15 == 0; }
int audio_spk_volume_db(void) { return (spk_vol[0] + spk_vol[1]) / 256; }

// 1 kHz sine at -12 dBFS from a 16-point table and a 16-bit phase accumulator.
static int16_t tone_next(void) {
    static const int16_t sine16[16] = {
        0, 3135, 5793, 7568, 8192, 7568, 5793, 3135,
        0, -3135, -5793, -7568, -8192, -7568, -5793, -3135,
    };
    tone_phase = (tone_phase + 65536u * 1000u / RATE) & 0xffffu;
    return sine16[tone_phase >> 12];
}

static void mic_write(const int16_t *s, uint32_t n) {
    if (mic_streaming && n) tud_audio_write(s, (uint16_t)(n * 2));
}

// Samples due from the generated clock since the last call.
static uint32_t gen_due(void) {
    uint64_t now = time_us_64();
    gen_acc += (now - gen_last_us) * RATE;
    gen_last_us = now;
    uint32_t n = (uint32_t)(gen_acc / 1000000u);
    gen_acc -= (uint64_t)n * 1000000u;
    if (n > 64) n = 64;                  // after a stall, don't burst
    return n;
}

// Speaker (USB OUT) samples: pulled from TinyUSB's FIFO in the main loop.
static void speaker_task(void) {
    int16_t buf[CFG_TUD_AUDIO_FUNC_1_EP_OUT_SW_BUF_SZ / 2];
    uint32_t n = tud_audio_read(buf, sizeof buf) / 2;
    if (n == 0) return;
    for (uint32_t i = 0; i < n; i++) buf[i] = apply_gain(buf[i]);

    switch (mode) {
    case AUDIO_MODE_PCM:
        pcm_uplink_write(buf, n);
        break;
    case AUDIO_MODE_LOOPBACK:
        for (uint32_t i = 0; i < n && (lb_head - lb_tail) < LB_SIZE; i++) lb_buf[lb_head++ % LB_SIZE] = buf[i];
        break;
    case AUDIO_MODE_TONE:
        break;                           // the tone owns the uplink
    }
}

void audio_task(void) {
    int16_t buf[64];

    speaker_task();

    if (mode == AUDIO_MODE_PCM && pcm_active()) {
        uint32_t n;
        while ((n = pcm_downlink_read(buf, TU_ARRAY_SIZE(buf))) > 0) mic_write(buf, n);
        gen_due();                       // keep the generated clock current
        return;
    }

    // No call audio: drop anything stale from the PCM side.
    if (mode == AUDIO_MODE_PCM) pcm_downlink_flush();

    uint32_t n = gen_due();
    if (n == 0) return;
    for (uint32_t i = 0; i < n; i++) {
        switch (mode) {
        case AUDIO_MODE_TONE:
            buf[i] = tone_next();
            break;
        case AUDIO_MODE_LOOPBACK:
            buf[i] = (lb_head != lb_tail) ? lb_buf[lb_tail++ % LB_SIZE] : 0;
            break;
        default:
            buf[i] = 0;
            break;
        }
    }
    if (mode == AUDIO_MODE_TONE) pcm_uplink_write(buf, n);   // the far end hears it too
    mic_write(buf, n);
}

//--------------------------------------------------------------------+
// TinyUSB audio callbacks
//--------------------------------------------------------------------+

bool tud_audio_set_itf_cb(uint8_t rhport, tusb_control_request_t const *p_request) {
    uint8_t itf = tu_u16_low(tu_le16toh(p_request->wIndex));
    uint8_t alt = tu_u16_low(tu_le16toh(p_request->wValue));
    if (itf == ITF_NUM_AUDIO_STREAMING_SPK) {
        spk_streaming = alt != 0;
        lb_head = lb_tail = 0;
        if (!spk_streaming) pcm_uplink_flush();
    } else if (itf == ITF_NUM_AUDIO_STREAMING_MIC) {
        mic_streaming = alt != 0;
        if (mic_streaming) {
            gen_last_us = time_us_64();
            gen_acc = 0;
        }
    }
    return true;
}

bool tud_audio_set_itf_close_ep_cb(uint8_t rhport, tusb_control_request_t const *p_request) {
    uint8_t itf = tu_u16_low(tu_le16toh(p_request->wIndex));
    uint8_t alt = tu_u16_low(tu_le16toh(p_request->wValue));
    if (alt == 0) {
        if (itf == ITF_NUM_AUDIO_STREAMING_SPK) spk_streaming = false;
        if (itf == ITF_NUM_AUDIO_STREAMING_MIC) mic_streaming = false;
    }
    return true;
}

static bool clock_get(uint8_t rhport, tusb_control_request_t const *req) {
    uint8_t sel = TU_U16_HIGH(req->wValue);
    if (sel == AUDIO20_CS_CTRL_SAM_FREQ) {
        if (req->bRequest == AUDIO20_CS_REQ_CUR) {
            audio20_control_cur_4_t cur = {(int32_t)tu_htole32(RATE)};
            return tud_audio_buffer_and_schedule_control_xfer(rhport, req, &cur, sizeof cur);
        }
        if (req->bRequest == AUDIO20_CS_REQ_RANGE) {
            audio20_control_range_4_n_t(1) range = {
                .wNumSubRanges = tu_htole16(1),
                .subrange[0] = {.bMin = (int32_t)RATE, .bMax = (int32_t)RATE, .bRes = 0},
            };
            return tud_audio_buffer_and_schedule_control_xfer(rhport, req, &range, sizeof range);
        }
    } else if (sel == AUDIO20_CS_CTRL_CLK_VALID && req->bRequest == AUDIO20_CS_REQ_CUR) {
        audio20_control_cur_1_t valid = {.bCur = 1};
        return tud_audio_buffer_and_schedule_control_xfer(rhport, req, &valid, sizeof valid);
    }
    return false;
}

static bool feature_get(uint8_t rhport, tusb_control_request_t const *req) {
    uint8_t sel = TU_U16_HIGH(req->wValue);
    uint8_t ch = TU_U16_LOW(req->wValue);
    if (ch > 1) return false;
    if (sel == AUDIO20_FU_CTRL_MUTE && req->bRequest == AUDIO20_CS_REQ_CUR) {
        audio20_control_cur_1_t m = {.bCur = spk_mute[ch]};
        return tud_audio_buffer_and_schedule_control_xfer(rhport, req, &m, sizeof m);
    }
    if (sel == AUDIO20_FU_CTRL_VOLUME) {
        if (req->bRequest == AUDIO20_CS_REQ_RANGE) {
            audio20_control_range_2_n_t(1) r = {
                .wNumSubRanges = tu_htole16(1),
                .subrange[0] = {.bMin = tu_htole16(-50 * 256), .bMax = tu_htole16(0), .bRes = tu_htole16(256)},
            };
            return tud_audio_buffer_and_schedule_control_xfer(rhport, req, &r, sizeof r);
        }
        if (req->bRequest == AUDIO20_CS_REQ_CUR) {
            audio20_control_cur_2_t v = {.bCur = tu_htole16(spk_vol[ch])};
            return tud_audio_buffer_and_schedule_control_xfer(rhport, req, &v, sizeof v);
        }
    }
    return false;
}

bool tud_audio_get_req_entity_cb(uint8_t rhport, tusb_control_request_t const *p_request) {
    uint8_t entity = TU_U16_HIGH(p_request->wIndex);
    if (entity == UAC2_ENTITY_CLOCK) return clock_get(rhport, p_request);
    if (entity == UAC2_ENTITY_SPK_FEATURE_UNIT) return feature_get(rhport, p_request);
    return false;
}

bool tud_audio_set_req_entity_cb(uint8_t rhport, tusb_control_request_t const *p_request, uint8_t *buf) {
    uint8_t entity = TU_U16_HIGH(p_request->wIndex);
    uint8_t sel = TU_U16_HIGH(p_request->wValue);
    uint8_t ch = TU_U16_LOW(p_request->wValue);
    if (p_request->bRequest != AUDIO20_CS_REQ_CUR) return false;

    if (entity == UAC2_ENTITY_CLOCK && sel == AUDIO20_CS_CTRL_SAM_FREQ) {
        // Fixed clock: accept only the one rate we run at.
        return ((audio20_control_cur_4_t const *)buf)->bCur == RATE;
    }
    if (entity == UAC2_ENTITY_SPK_FEATURE_UNIT && ch <= 1) {
        if (sel == AUDIO20_FU_CTRL_MUTE) {
            spk_mute[ch] = ((audio20_control_cur_1_t const *)buf)->bCur;
            update_gain();
            return true;
        }
        if (sel == AUDIO20_FU_CTRL_VOLUME) {
            int16_t v = (int16_t)tu_le16toh(((audio20_control_cur_2_t const *)buf)->bCur);
            if (v > 0) v = 0;
            if (v < -50 * 256) v = -50 * 256;
            spk_vol[ch] = v;
            update_gain();
            return true;
        }
    }
    return false;
}
