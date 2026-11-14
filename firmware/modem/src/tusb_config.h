#pragma once

#include "usb_descriptors.h"

#ifndef CFG_TUSB_MCU
#error CFG_TUSB_MCU must be defined
#endif
#ifndef CFG_TUSB_OS
#define CFG_TUSB_OS OPT_OS_NONE
#endif

#define CFG_TUD_ENABLED          1
#define CFG_TUSB_RHPORT0_MODE    OPT_MODE_DEVICE
#define CFG_TUD_ENDPOINT0_SIZE   64
#define CFG_TUSB_MEM_ALIGN       __attribute__((aligned(4)))

#define CFG_TUD_CDC              1
#define CFG_TUD_AUDIO            1
#define CFG_TUD_MSC              0
#define CFG_TUD_HID              0
#define CFG_TUD_MIDI             0
#define CFG_TUD_VENDOR           0

#define CFG_TUD_CDC_RX_BUFSIZE   256
#define CFG_TUD_CDC_TX_BUFSIZE   1024
#define CFG_TUD_CDC_RX_EPSIZE    64
#define CFG_TUD_CDC_TX_EPSIZE    64

// Audio: one function, mono 16-bit each way, one sample rate.
#define CFG_TUD_AUDIO_FUNC_1_DESC_LEN       KOPPAR_AUDIO_DESC_LEN
#define CFG_TUD_AUDIO_FUNC_1_N_AS_INT       2
#define CFG_TUD_AUDIO_FUNC_1_CTRL_BUF_SZ    64

#define CFG_TUD_AUDIO_ENABLE_EP_IN          1
#define CFG_TUD_AUDIO_FUNC_1_EP_IN_SZ_MAX   AUDIO_EP_SZ
// TinyUSB's IN flow control steers this FIFO to half full, sending one sample
// more or less per packet: about 4 ms of microphone latency at this size.
#define CFG_TUD_AUDIO_FUNC_1_EP_IN_SW_BUF_SZ  (8 * AUDIO_EP_SZ)

#define CFG_TUD_AUDIO_ENABLE_EP_OUT         1
#define CFG_TUD_AUDIO_FUNC_1_EP_OUT_SZ_MAX  AUDIO_EP_SZ
#define CFG_TUD_AUDIO_FUNC_1_EP_OUT_SW_BUF_SZ (8 * AUDIO_EP_SZ)
