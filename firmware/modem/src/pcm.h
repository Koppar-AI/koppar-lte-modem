// PCM link to the EC25 (PIO slave) and the two sample rings around it.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t frames;        // PCM frames seen since boot
    uint32_t dl_overruns;   // downlink samples dropped: ring full (USB side not reading)
    uint32_t ul_underruns;  // uplink frames with no sample ready (sent silence or repeat)
    uint32_t ul_slips;      // uplink drift corrections (dropped or repeated samples)
} pcm_stats_t;

void pcm_init(void);

// Run the PIO slave and its interrupt only while the module is on. With the
// module off the TXS0108E is unpowered and the PCM lines float: noise on them
// must not turn into "frames" and an interrupt storm.
void pcm_enable(bool on);
bool pcm_enabled(void);

// Call every few ms: tracks whether the module is clocking frames.
void pcm_task(void);
bool pcm_active(void);              // frames arriving at about the expected rate
uint32_t pcm_frame_rate(void);      // measured frames per second

// Downlink: module -> USB microphone. Returns samples read.
uint32_t pcm_downlink_read(int16_t *dst, uint32_t max);
void pcm_downlink_flush(void);

// Uplink: USB speaker -> module. Returns samples accepted.
uint32_t pcm_uplink_write(const int16_t *src, uint32_t n);
void pcm_uplink_flush(void);

void pcm_get_stats(pcm_stats_t *out);
