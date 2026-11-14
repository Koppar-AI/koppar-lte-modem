// Koppar LTE modem, rev B: RP2040 pin map (from the rev B netlist).
#pragma once

// Through the TXS0108E (3.3 V side). Names are from the EC25's point of view:
// PCM_IN is the module's input (we drive it), PCM_OUT its output (we read it).
#define PIN_PCM_CLK   16   // in,  from the module (PCM master)
#define PIN_PCM_SYNC  17   // in,  from the module
#define PIN_PCM_IN    18   // out, uplink audio to the module
#define PIN_PCM_OUT   19   // in,  downlink audio from the module
#define PIN_RI        20   // in,  ring indicator: idle high, 120 ms low per URC
#define PIN_STATUS    21   // in,  open drain: low while the module is on

// 2N7002 gates with 100 k pull-downs: high = the module pin pulled low.
#define PIN_PWRKEY_G  22
#define PIN_RESET_G   23

// LEDs through 1 k, active high.
#define PIN_LED_NET   24   // green, D2
#define PIN_LED_RING  25   // amber, D3
#define PIN_LED_CALL  26   // blue,  D5
