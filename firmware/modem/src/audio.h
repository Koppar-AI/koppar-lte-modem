// USB sound card: routes call audio between USB and the PCM link.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum {
    AUDIO_MODE_PCM = 0,     // USB <-> the module's PCM (silence when no call)
    AUDIO_MODE_LOOPBACK,    // USB speaker -> USB microphone, for testing without a call
    AUDIO_MODE_TONE,        // 1 kHz tone into the USB microphone (and the uplink)
} audio_mode_t;

void audio_init(void);
void audio_task(void);

void audio_set_mode(audio_mode_t m);
audio_mode_t audio_get_mode(void);
const char *audio_mode_name(audio_mode_t m);

bool audio_spk_streaming(void);   // the computer is playing into the call
bool audio_mic_streaming(void);   // the computer is recording the call
bool audio_spk_muted(void);
int  audio_spk_volume_db(void);
