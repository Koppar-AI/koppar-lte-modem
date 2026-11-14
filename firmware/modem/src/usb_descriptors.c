// USB descriptors: UAC2 mono headset + CDC control port.
//
// VID/PID: 0x1209/0x0001 is the pid.codes test PID, fine on your own desk.
// Apply for a free PID at pid.codes (or ask Raspberry Pi for an RP2040 PID)
// before giving boards to anyone else.

#include <string.h>

#include "pico/unique_id.h"
#include "tusb.h"
#include "usb_descriptors.h"

#define USB_VID 0x1209
#define USB_PID 0x0001

static const tusb_desc_device_t desc_device = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0200,
    .bDeviceClass       = TUSB_CLASS_MISC,     // IAD composite
    .bDeviceSubClass    = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol    = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = USB_VID,
    .idProduct          = USB_PID,
    .bcdDevice          = 0x0100,
    .iManufacturer      = 1,
    .iProduct           = 2,
    .iSerialNumber      = 3,
    .bNumConfigurations = 1,
};

const uint8_t *tud_descriptor_device_cb(void) { return (const uint8_t *)&desc_device; }

#define EPNUM_CDC_NOTIF 0x83
#define EPNUM_CDC_OUT   0x04
#define EPNUM_CDC_IN    0x84

enum {
    STRID_LANGID = 0,
    STRID_MANUFACTURER,
    STRID_PRODUCT,
    STRID_SERIAL,
    STRID_AUDIO,
    STRID_SPK,
    STRID_MIC,
    STRID_CDC,
};

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + KOPPAR_AUDIO_DESC_LEN + TUD_CDC_DESC_LEN)

#define AC_TOTAL_LEN (TUD_AUDIO20_DESC_CLK_SRC_LEN + TUD_AUDIO20_DESC_INPUT_TERM_LEN \
    + TUD_AUDIO20_DESC_FEATURE_UNIT_LEN(1) + TUD_AUDIO20_DESC_OUTPUT_TERM_LEN \
    + TUD_AUDIO20_DESC_INPUT_TERM_LEN + TUD_AUDIO20_DESC_OUTPUT_TERM_LEN)

#define FU_CTRLS (AUDIO20_CTRL_RW << AUDIO20_FEATURE_UNIT_CTRL_MUTE_POS | AUDIO20_CTRL_RW << AUDIO20_FEATURE_UNIT_CTRL_VOLUME_POS)

static const uint8_t desc_configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 100),

    // ---- Audio function ----
    TUD_AUDIO20_DESC_IAD(ITF_NUM_AUDIO_CONTROL, 3, 0x00),
    TUD_AUDIO20_DESC_STD_AC(ITF_NUM_AUDIO_CONTROL, 0x00, STRID_AUDIO),
    TUD_AUDIO20_DESC_CS_AC(0x0200, AUDIO20_FUNC_HEADSET, AC_TOTAL_LEN, AUDIO20_CS_AS_INTERFACE_CTRL_LATENCY_POS),
    // Internal fixed clock; frequency readable, not writable.
    TUD_AUDIO20_DESC_CLK_SRC(UAC2_ENTITY_CLOCK, AUDIO20_CLOCK_SOURCE_ATT_INT_FIX_CLK,
                           (AUDIO20_CTRL_R << AUDIO20_CLOCK_SOURCE_CTRL_CLK_FRQ_POS | AUDIO20_CTRL_R << AUDIO20_CLOCK_SOURCE_CTRL_CLK_VAL_POS),
                           0x00, 0x00),
    // Speaker path: USB -> feature unit -> phone line
    TUD_AUDIO20_DESC_INPUT_TERM(UAC2_ENTITY_SPK_INPUT_TERMINAL, AUDIO_TERM_TYPE_USB_STREAMING, 0x00, UAC2_ENTITY_CLOCK,
                              1, AUDIO20_CHANNEL_CONFIG_NON_PREDEFINED, 0x00, 0x0000, 0x00),
    TUD_AUDIO20_DESC_FEATURE_UNIT(UAC2_ENTITY_SPK_FEATURE_UNIT, UAC2_ENTITY_SPK_INPUT_TERMINAL, 0x00, FU_CTRLS, FU_CTRLS),
    TUD_AUDIO20_DESC_OUTPUT_TERM(UAC2_ENTITY_SPK_OUTPUT_TERMINAL, AUDIO_TERM_TYPE_OUT_GENERIC_SPEAKER, 0x00,
                               UAC2_ENTITY_SPK_FEATURE_UNIT, UAC2_ENTITY_CLOCK, 0x0000, 0x00),
    // Microphone path: phone line -> USB
    TUD_AUDIO20_DESC_INPUT_TERM(UAC2_ENTITY_MIC_INPUT_TERMINAL, AUDIO_TERM_TYPE_IN_GENERIC_MIC, 0x00, UAC2_ENTITY_CLOCK,
                              1, AUDIO20_CHANNEL_CONFIG_NON_PREDEFINED, 0x00, 0x0000, 0x00),
    TUD_AUDIO20_DESC_OUTPUT_TERM(UAC2_ENTITY_MIC_OUTPUT_TERMINAL, AUDIO_TERM_TYPE_USB_STREAMING, 0x00,
                               UAC2_ENTITY_MIC_INPUT_TERMINAL, UAC2_ENTITY_CLOCK, 0x0000, 0x00),

    // Speaker streaming interface
    TUD_AUDIO20_DESC_STD_AS_INT(ITF_NUM_AUDIO_STREAMING_SPK, 0x00, 0x00, STRID_SPK),
    TUD_AUDIO20_DESC_STD_AS_INT(ITF_NUM_AUDIO_STREAMING_SPK, 0x01, 0x01, STRID_SPK),
    TUD_AUDIO20_DESC_CS_AS_INT(UAC2_ENTITY_SPK_INPUT_TERMINAL, AUDIO20_CTRL_NONE, AUDIO20_FORMAT_TYPE_I,
                             AUDIO20_DATA_FORMAT_TYPE_I_PCM, 1, AUDIO20_CHANNEL_CONFIG_NON_PREDEFINED, 0x00),
    TUD_AUDIO20_DESC_TYPE_I_FORMAT(AUDIO_N_BYTES_PER_SAMPLE, AUDIO_RESOLUTION),
    TUD_AUDIO20_DESC_STD_AS_ISO_EP(EPNUM_AUDIO_OUT,
                                 (uint8_t)(TUSB_XFER_ISOCHRONOUS | TUSB_ISO_EP_ATT_ADAPTIVE | TUSB_ISO_EP_ATT_DATA),
                                 AUDIO_EP_SZ, 0x01),
    TUD_AUDIO20_DESC_CS_AS_ISO_EP(AUDIO20_CS_AS_ISO_DATA_EP_ATT_NON_MAX_PACKETS_OK, AUDIO20_CTRL_NONE,
                                AUDIO20_CS_AS_ISO_DATA_EP_LOCK_DELAY_UNIT_MILLISEC, 0x0001),

    // Microphone streaming interface
    TUD_AUDIO20_DESC_STD_AS_INT(ITF_NUM_AUDIO_STREAMING_MIC, 0x00, 0x00, STRID_MIC),
    TUD_AUDIO20_DESC_STD_AS_INT(ITF_NUM_AUDIO_STREAMING_MIC, 0x01, 0x01, STRID_MIC),
    TUD_AUDIO20_DESC_CS_AS_INT(UAC2_ENTITY_MIC_OUTPUT_TERMINAL, AUDIO20_CTRL_NONE, AUDIO20_FORMAT_TYPE_I,
                             AUDIO20_DATA_FORMAT_TYPE_I_PCM, 1, AUDIO20_CHANNEL_CONFIG_NON_PREDEFINED, 0x00),
    TUD_AUDIO20_DESC_TYPE_I_FORMAT(AUDIO_N_BYTES_PER_SAMPLE, AUDIO_RESOLUTION),
    TUD_AUDIO20_DESC_STD_AS_ISO_EP(EPNUM_AUDIO_IN,
                                 (uint8_t)(TUSB_XFER_ISOCHRONOUS | TUSB_ISO_EP_ATT_ASYNCHRONOUS | TUSB_ISO_EP_ATT_DATA),
                                 AUDIO_EP_SZ, 0x01),
    TUD_AUDIO20_DESC_CS_AS_ISO_EP(AUDIO20_CS_AS_ISO_DATA_EP_ATT_NON_MAX_PACKETS_OK, AUDIO20_CTRL_NONE,
                                AUDIO20_CS_AS_ISO_DATA_EP_LOCK_DELAY_UNIT_UNDEFINED, 0x0000),

    // ---- CDC control port ----
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, STRID_CDC, EPNUM_CDC_NOTIF, 8, EPNUM_CDC_OUT, EPNUM_CDC_IN, 64),
};

_Static_assert(sizeof(desc_configuration) == CONFIG_TOTAL_LEN, "configuration descriptor length");

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return desc_configuration;
}

static const char *const string_desc[] = {
    [STRID_MANUFACTURER] = "Koppar",
    [STRID_PRODUCT]      = "Koppar LTE modem",
    [STRID_AUDIO]        = "Koppar LTE modem",
    [STRID_SPK]          = "Koppar call audio out",
    [STRID_MIC]          = "Koppar call audio in",
    [STRID_CDC]          = "Koppar LTE modem control",
};

static uint16_t desc_str[40];

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    size_t n;
    if (index == STRID_LANGID) {
        desc_str[1] = 0x0409;
        n = 1;
    } else if (index == STRID_SERIAL) {
        char id[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
        pico_get_unique_board_id_string(id, sizeof id);
        for (n = 0; id[n]; n++) desc_str[1 + n] = (uint16_t)id[n];
    } else {
        if (index >= TU_ARRAY_SIZE(string_desc) || !string_desc[index]) return NULL;
        const char *s = string_desc[index];
        n = strlen(s);
        if (n > TU_ARRAY_SIZE(desc_str) - 1) n = TU_ARRAY_SIZE(desc_str) - 1;
        for (size_t i = 0; i < n; i++) desc_str[1 + i] = (uint16_t)s[i];
    }
    desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * n + 2));
    return desc_str;
}
