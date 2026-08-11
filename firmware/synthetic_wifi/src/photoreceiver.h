#pragma once
/*
 * Board A - receive an image from the camera and post it over cellular.
 *
 * Goes at:  ~/agrisync/firmware/synthetic_wifi/src/photoreceiver.h
 *
 * WHAT IT DOES
 *   Watches UART1 for a framed image from Board B, holds it in RAM just long
 *   enough to POST it through the modem, and answers yes or no.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 *   Buffer images. If the POST fails it says no, and the camera keeps the file
 *   on its SD card to retry later. The card already holds weeks of images;
 *   copying that responsibility onto Board A's small flash would create a
 *   second thing to fill up and go wrong, for no gain.
 *
 * WHY IT CANNOT DISTURB THE SENSOR CYCLE
 *   photoReceiverService() returns immediately unless a frame is actually
 *   arriving. Only when it sees the magic bytes does it block, and then only
 *   for as long as the transfer plus one POST takes. Readings are every five
 *   minutes; an image takes a few seconds.
 *
 * MEMORY
 *   One malloc of the image size, freed before returning. A 30KB image on a
 *   board with 320KB is comfortable, and the allocation is checked - if it
 *   fails the frame is drained and refused rather than crashing the node that
 *   is doing the actual job.
 */

#include <Arduino.h>
#include "photolink.h"
#include "modem.h"

#ifndef PHOTO_PATH
#define PHOTO_PATH "/api/v1/photos"
#endif

HardwareSerial camSerial(1);        // UART1: modem has UART2, USB has UART0

void photoReceiverBegin() {
  /*
   * 8KB receive buffer, up from the 256-byte default. MUST be called before
   * begin() or it is ignored.
   *
   * The camera pushes 24KB in one continuous burst. With 256 bytes of slack,
   * any pause on this side - a timer interrupt, the rain gauge ISR, the modem
   * driver - overflows the buffer and those bytes are gone. The receiver then
   * waits forever for data that no longer exists and reports a stall.
   *
   * 8KB is 70ms of headroom at 115200 instead of 2ms. It costs 8KB of a heap
   * with 270KB spare.
   */
  camSerial.setRxBufferSize(8192);

  camSerial.begin(PHOTOLINK_BAUD, SERIAL_8N1,
                  PHOTOLINK_A_RX_PIN, PHOTOLINK_A_TX_PIN);
  camSerial.setTimeout(50);
}

/* Blocking read of exactly n bytes, with an idle timeout. */
static bool camRead(uint8_t *dst, size_t n, uint32_t idleMs) {
  size_t got = 0;
  uint32_t last = millis();
  while (got < n) {
    if (camSerial.available()) {
      dst[got++] = (uint8_t)camSerial.read();
      last = millis();
    } else {
      if (millis() - last > idleMs) return false;
      delay(1);
    }
  }
  return true;
}

static void camDrain(uint32_t quietMs = 300) {
  uint32_t last = millis();
  while (millis() - last < quietMs) {
    while (camSerial.available()) { camSerial.read(); last = millis(); }
    delay(2);
  }
}

/*
 * Call from loop(). Cheap when nothing is happening.
 *
 * Returns true if an image was received and accepted by the server.
 */
bool photoReceiverService(Modem &modem, const char *host, const char *secret,
                          bool cellReady) {
  // Scan for the magic. Anything else on the wire is the camera's debug
  // output and is discarded a byte at a time, which is why debug prints on
  // Board B are harmless rather than something to switch off.
  static uint8_t win[PHOTOLINK_MAGIC_LEN] = {0};
  while (camSerial.available()) {
    memmove(win, win + 1, PHOTOLINK_MAGIC_LEN - 1);
    win[PHOTOLINK_MAGIC_LEN - 1] = (uint8_t)camSerial.read();
    if (memcmp(win, PHOTOLINK_MAGIC, PHOTOLINK_MAGIC_LEN) == 0) {
      memset(win, 0, sizeof(win));
      goto have_frame;
    }
  }
  return false;

have_frame:
  Serial.println("\n[photo] frame incoming");

  uint8_t nameLen = 0;
  if (!camRead(&nameLen, 1, PHOTOLINK_RX_IDLE_MS) ||
      nameLen == 0 || nameLen > PHOTOLINK_MAX_NAME) {
    Serial.println("[photo] bad name length");
    camDrain(); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  char name[PHOTOLINK_MAX_NAME + 1];
  if (!camRead((uint8_t *)name, nameLen, PHOTOLINK_RX_IDLE_MS)) {
    Serial.println("[photo] name truncated");
    camDrain(); camSerial.print(PHOTOLINK_NAK);
    return false;
  }
  name[nameLen] = 0;

  uint8_t hdr[8];
  if (!camRead(hdr, 8, PHOTOLINK_RX_IDLE_MS)) {
    Serial.println("[photo] header truncated");
    camDrain(); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  uint32_t len = (uint32_t)hdr[0] | ((uint32_t)hdr[1] << 8)
               | ((uint32_t)hdr[2] << 16) | ((uint32_t)hdr[3] << 24);
  uint32_t wantCrc = (uint32_t)hdr[4] | ((uint32_t)hdr[5] << 8)
                   | ((uint32_t)hdr[6] << 16) | ((uint32_t)hdr[7] << 24);

  Serial.printf("[photo] %s  %lu bytes\n", name, (unsigned long)len);

  if (len == 0 || len > PHOTOLINK_MAX_BYTES) {
    Serial.println("[photo] implausible length");
    camDrain(); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  uint8_t *buf = (uint8_t *)malloc(len);
  if (!buf) {
    // Refuse cleanly rather than crashing the board that is taking the
    // measurements. The camera still has the file.
    Serial.printf("[photo] no RAM for %lu bytes\n", (unsigned long)len);
    camDrain(2000); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  if (!camRead(buf, len, PHOTOLINK_RX_IDLE_MS)) {
    Serial.println("[photo] transfer stalled");
    free(buf); camDrain(); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  uint32_t gotCrc = photolinkCrc32Final(photolinkCrc32(buf, len));
  if (gotCrc != wantCrc) {
    // A truncated or corrupted JPEG that reaches the server is worse than one
    // that never arrives, because it looks like data.
    Serial.printf("[photo] CRC mismatch (%08lx vs %08lx)\n",
                  (unsigned long)gotCrc, (unsigned long)wantCrc);
    free(buf); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  if (!cellReady) {
    Serial.println("[photo] no cellular link - refusing, camera keeps it");
    free(buf); camSerial.print(PHOTOLINK_NAK);
    return false;
  }

  // Filename in the query string. This modem can only carry one custom
  // header and the signature has it.
  char path[128];
  snprintf(path, sizeof(path), PHOTO_PATH "?name=%s", name);

  int code = modem.postBinary(host, path, buf, len, secret);
  free(buf);

  Serial.printf("[photo] HTTP %d\n", code);

  if (code >= 200 && code < 300) {
    camSerial.print(PHOTOLINK_ACK);
    return true;
  }

  if (code == 401) Serial.println("        signature rejected - secret or clock");
  camSerial.print(PHOTOLINK_NAK);
  return false;
}
