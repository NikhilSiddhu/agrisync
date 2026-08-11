#pragma once
/*
 * AgriSync photo link - wire protocol.
 *
 * IDENTICAL COPY REQUIRED IN BOTH PROJECTS:
 *   ~/agrisync/firmware/esp32cam_capture/src/photolink.h   (sender)
 *   ~/agrisync/firmware/synthetic_wifi/src/photolink.h     (receiver)
 *
 * If you change anything here, change it in both or the link silently stops
 * framing and you will spend an evening on it.
 *
 * ---------------------------------------------------------------------------
 * WIRING
 *   Board B (camera) U0T  GPIO1  ->  Board A GPIO18   (Board A RX1)
 *   Board B (camera) U0R  GPIO3  <-  Board A GPIO19   (Board A TX1)
 *   common ground
 *
 *   The camera uses its U0 pins because with the SD card in 4-bit mode almost
 *   nothing else is free. That is the same port you flash through, so
 *   DISCONNECT THESE TWO WIRES BEFORE FLASHING THE CAMERA.
 *
 * ---------------------------------------------------------------------------
 * FRAME
 *   magic     4 bytes   "AGS1"
 *   nameLen   1 byte    1..63
 *   name      nameLen   filename, no path, no trailing null
 *   dataLen   4 bytes   little endian, <= PHOTOLINK_MAX_BYTES
 *   crc32     4 bytes   little endian, over the image bytes only
 *   data      dataLen   the JPEG
 *
 * REPLY  (receiver -> sender, one line)
 *   "AGSOK\n"   stored on the server
 *   "AGSNO\n"   not stored; sender keeps the file and retries later
 *
 * WHY A MAGIC HEADER
 *   The camera's U0 is also its debug console, so ordinary Serial.println
 *   output flows down the same wire. The receiver scans for "AGS1" and
 *   discards everything else, which means debug text is harmless rather than
 *   something that has to be switched off.
 *
 * WHY CRC32 AND NOT A SUM
 *   A checksum that only adds bytes cannot see two errors that cancel out.
 *   At 460800 baud over jumper wire in a box next to a transmitting modem,
 *   corruption is a real possibility, and a silently truncated JPEG is worse
 *   than no JPEG.
 */

#include <Arduino.h>

/*
 * 115200, not faster, on purpose.
 *
 * On Board B this port is ALSO the debug console. Running the link at a
 * different speed would mean every Serial.println came out as garbage at the
 * monitor's baud rate. Sharing one speed keeps the console readable and lets
 * the magic header sort frames from text.
 *
 * 30KB at 115200 takes about 2.6 seconds. At one image per fifteen minutes
 * that is not worth optimising.
 */
#define PHOTOLINK_BAUD        115200
#define PHOTOLINK_MAGIC       "AGS1"
#define PHOTOLINK_MAGIC_LEN   4

#define PHOTOLINK_MAX_BYTES   200000   // ~6x a typical 30KB frame
#define PHOTOLINK_MAX_NAME    63

#define PHOTOLINK_ACK         "AGSOK\n"
#define PHOTOLINK_NAK         "AGSNO\n"

// Sender waits this long for a reply. A POST over cellular can take a while:
// registration, then the request itself.
#define PHOTOLINK_REPLY_MS    90000

// Receiver gives up if the sender stalls mid-frame.
#define PHOTOLINK_RX_IDLE_MS  8000

// Board A pins for UART1. GPIO16/17 are avoided - they are consumed by PSRAM
// on some modules and are not broken out on others.
#define PHOTOLINK_A_RX_PIN    18   // <- camera TX
#define PHOTOLINK_A_TX_PIN    19   // -> camera RX


/* Bitwise CRC32, no lookup table. A table would be faster and cost 1KB of
   RAM; at 30KB per quarter hour the speed is irrelevant. */
static inline uint32_t photolinkCrc32(const uint8_t *data, size_t len,
                                      uint32_t crc = 0xFFFFFFFF) {
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) {
      crc = (crc >> 1) ^ (0xEDB88320u & (-(int32_t)(crc & 1)));
    }
  }
  return crc;
}

static inline uint32_t photolinkCrc32Final(uint32_t crc) {
  return crc ^ 0xFFFFFFFF;
}
