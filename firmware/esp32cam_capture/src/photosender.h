#pragma once
/*
 * Board B (camera) - get an image off the board.
 *
 * Goes at:  ~/agrisync/firmware/esp32cam_capture/src/photosender.h
 *
 * THREE PLACES AN IMAGE CAN GO, IN ORDER
 *   1. The SD card          always, first, before anything else is tried
 *   2. Board A over UART    the field path: Board A posts it over cellular
 *   3. WiFi direct          the fallback: only works in range
 *
 * The card comes first deliberately. Both upload paths are best effort on top
 * of a file that already exists, so a power cut, a dead modem or a wedged
 * Board A costs you nothing but a delay.
 *
 * WHY WIFI IS STILL HERE
 *   It is not the plan, it is the backup. On the bench it is faster than
 *   waking the modem. In a field, set your phone hotspot to the same SSID and
 *   password as your home WiFi and the box will empty its backlog while you
 *   stand next to it - useful the day the UART link or the modem is the thing
 *   that has gone wrong.
 *
 * CATCH-UP
 *   Every image that fails both paths stays on the card, and its name goes in
 *   /canopy/pending.txt. On each cycle, once a link works, up to
 *   CATCHUP_PER_CYCLE older images are sent too. A single line file is used
 *   rather than an in-memory list because a fortnight of backlog is thousands
 *   of filenames and this board has better uses for its RAM.
 *
 * BUILD FLAGS (platformio.ini)
 *   -DHMAC_SECRET='"${sysenv.DEVICE_HMAC_SECRET}"'
 *   -DWIFI_SSID='"${sysenv.WIFI_SSID}"'          optional, fallback only
 *   -DWIFI_PASSWORD='"${sysenv.WIFI_PASSWORD}"'  optional
 *   -DUPLOAD_HOST='"192.168.1.35"'               optional, the Pi on the LAN
 *   -DUPLOAD_PORT=5000
 */

#include <Arduino.h>
#include <FS.h>
#include <SD_MMC.h>
#include "photolink.h"

#if defined(WIFI_SSID) && defined(UPLOAD_HOST)
  #define PHOTO_WIFI_FALLBACK 1
  #include <WiFi.h>
  #include <HTTPClient.h>
  #include "mbedtls/md.h"
  #ifndef UPLOAD_PORT
  #define UPLOAD_PORT 5000
  #endif
#else
  #define PHOTO_WIFI_FALLBACK 0
#endif

#define PENDING_PATH      "/canopy/pending.txt"
#define CATCHUP_PER_CYCLE 3
#define UART_CHUNK        512

// If not a single byte comes back within this long, nothing is listening on
// the other end and there is no point waiting out PHOTOLINK_REPLY_MS.
// Sender-side only, so changing it does not require reflashing Board A.
#define PHOTOLINK_FIRSTBYTE_MS 4000

/*
 * The link IS the debug console.
 *
 * With the SD card in 4-bit mode, U0 (GPIO1/GPIO3) is the only UART free on
 * this board - and that is the same port Serial prints to. So rather than
 * fight it, frames and log text share one port at one baud rate.
 *
 * Board A scans for the "AGS1" magic and discards everything else, so debug
 * output is harmless. The only rule is that nothing may print in the middle
 * of a frame, and since this is single threaded, nothing does.
 */
#define linkSerial Serial

// ---------------------------------------------------------------------------

/* Nothing to open - Serial is already running. Kept so main.cpp reads
   clearly and so there is somewhere to put setup if this ever moves to its
   own UART. */
void photoSenderBegin() {
  Serial.println("[link] sharing U0 with the console at 115200");
}

/* ------------------------------------------------------------------ pending */

static void pendingAdd(const char *name) {
  File f = SD_MMC.open(PENDING_PATH, FILE_APPEND);
  if (!f) return;
  f.println(name);
  f.close();
}

/* Rewrites the list without `name`. Called after a successful send. */
static void pendingRemove(const char *name) {
  if (!SD_MMC.exists(PENDING_PATH)) return;

  File in = SD_MMC.open(PENDING_PATH, FILE_READ);
  if (!in) return;
  File out = SD_MMC.open("/canopy/pending.tmp", FILE_WRITE);
  if (!out) { in.close(); return; }

  while (in.available()) {
    String line = in.readStringUntil('\n');
    line.trim();
    if (line.length() && line != name) out.println(line);
  }
  in.close();
  out.close();

  SD_MMC.remove(PENDING_PATH);
  SD_MMC.rename("/canopy/pending.tmp", PENDING_PATH);
}

static int pendingCount() {
  if (!SD_MMC.exists(PENDING_PATH)) return 0;
  File f = SD_MMC.open(PENDING_PATH, FILE_READ);
  if (!f) return 0;
  int n = 0;
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length()) n++;
  }
  f.close();
  return n;
}

/* Nth entry, or empty. Reading by index keeps memory flat regardless of how
   long the backlog is. */
static String pendingAt(int index) {
  if (!SD_MMC.exists(PENDING_PATH)) return String();
  File f = SD_MMC.open(PENDING_PATH, FILE_READ);
  if (!f) return String();
  int n = 0;
  String found;
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (!line.length()) continue;
    if (n == index) { found = line; break; }
    n++;
  }
  f.close();
  return found;
}

/* --------------------------------------------------------------- UART path */

static bool sendOverUart(const char *name, const uint8_t *data, size_t len) {
  size_t nameLen = strlen(name);
  if (nameLen == 0 || nameLen > PHOTOLINK_MAX_NAME) return false;
  if (len == 0 || len > PHOTOLINK_MAX_BYTES) return false;

  while (linkSerial.available()) linkSerial.read();     // drop stale bytes

  uint32_t crc = photolinkCrc32Final(photolinkCrc32(data, len));

  linkSerial.write((const uint8_t *)PHOTOLINK_MAGIC, PHOTOLINK_MAGIC_LEN);
  linkSerial.write((uint8_t)nameLen);
  linkSerial.write((const uint8_t *)name, nameLen);

  uint8_t hdr[8];
  hdr[0] = len       & 0xFF;  hdr[1] = (len >> 8)  & 0xFF;
  hdr[2] = (len >> 16) & 0xFF; hdr[3] = (len >> 24) & 0xFF;
  hdr[4] = crc       & 0xFF;  hdr[5] = (crc >> 8)  & 0xFF;
  hdr[6] = (crc >> 16) & 0xFF; hdr[7] = (crc >> 24) & 0xFF;
  linkSerial.write(hdr, 8);

  // Chunked so the driver's TX buffer is never overrun.
  size_t sent = 0;
  while (sent < len) {
    size_t n = min((size_t)UART_CHUNK, len - sent);
    linkSerial.write(data + sent, n);
    sent += n;
    delay(1);
  }
  linkSerial.flush();

  /*
   * Two-stage wait.
   *
   * Board A only answers once its POST has finished, and over cellular that
   * genuinely takes up to a minute - so once it starts talking we are patient.
   *
   * But if the jumpers are not connected, or Board A is not running, NOTHING
   * ever arrives and waiting the full ninety seconds stalls the capture loop
   * for no reason. So: bail quickly if there is no sign of life at all, and
   * only wait properly once a byte has come back.
   */
  String reply;
  uint32_t start = millis();
  bool alive = false;

  while (true) {
    uint32_t elapsed = millis() - start;
    if (!alive && elapsed > PHOTOLINK_FIRSTBYTE_MS) return false;  // nobody home
    if (elapsed > PHOTOLINK_REPLY_MS)               return false;  // gave up

    while (linkSerial.available()) {
      alive = true;
      reply += (char)linkSerial.read();
      if (reply.length() > 300) reply.remove(0, 150);
      if (reply.indexOf("AGSOK") >= 0) return true;
      if (reply.indexOf("AGSNO") >= 0) return false;
    }
    delay(10);
  }
}

/* --------------------------------------------------------------- WiFi path */

#if PHOTO_WIFI_FALLBACK

static String hmacHex(const char *key, const uint8_t *msg, size_t len) {
  byte out[32];
  mbedtls_md_context_t ctx;
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, info, 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char *)key, strlen(key));
  mbedtls_md_hmac_update(&ctx, msg, len);
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);

  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", out[i]);
  hex[64] = 0;
  return String(hex);
}

static bool wifiUp(uint32_t timeoutMs = 12000) {
  if (WiFi.status() == WL_CONNECTED) return true;

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(250);
  if (WiFi.status() != WL_CONNECTED) return false;

  if (time(nullptr) < 1735689600L) {          // before 2025 = clock unset
    configTime(0, 0, "pool.ntp.org");         // 0,0 = UTC
    uint32_t t1 = millis();
    while (time(nullptr) < 1735689600L && millis() - t1 < 8000) delay(200);
  }
  return time(nullptr) >= 1735689600L;        // no clock, no valid signature
}

static bool sendOverWifi(const char *name, const uint8_t *data, size_t len) {
  if (!wifiUp()) return false;

  long ts = (long)time(nullptr);

  // Signed string is "<ts>." then the raw image. Built in one buffer so it is
  // byte-identical to what the server recomputes.
  char pfx[24];
  int pfxLen = snprintf(pfx, sizeof(pfx), "%ld.", ts);
  uint8_t *buf = (uint8_t *)malloc(pfxLen + len);
  if (!buf) return false;
  memcpy(buf, pfx, pfxLen);
  memcpy(buf + pfxLen, data, len);
  String sig = hmacHex(HMAC_SECRET, buf, pfxLen + len);
  free(buf);

  // Filename in the query string, not a header. The cellular path can only
  // carry one custom header and that is the signature, so both paths use the
  // same shape and the server needs one code path.
  String url = String("http://") + UPLOAD_HOST + ":" + String(UPLOAD_PORT)
             + "/api/v1/photos?name=" + name;

  HTTPClient http;
  http.begin(url);
  http.setTimeout(20000);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Sig", String(ts) + "." + sig);

  int code = http.POST((uint8_t *)data, len);
  http.end();

  return code >= 200 && code < 300;
}

#endif  // PHOTO_WIFI_FALLBACK

/* ------------------------------------------------------------------ public */

/*
 * Try both paths for one image already sitting in RAM.
 * Returns true if the server has it.
 */
bool photoSend(const char *name, const uint8_t *data, size_t len) {
  if (sendOverUart(name, data, len)) {
    Serial.printf("[link] %s sent via Board A\n", name);
    return true;
  }

#if PHOTO_WIFI_FALLBACK
  if (sendOverWifi(name, data, len)) {
    Serial.printf("[link] %s sent via WiFi fallback\n", name);
    return true;
  }
#endif

  Serial.printf("[link] %s not sent - stays on the card\n", name);
  return false;
}

/*
 * Called straight after the SD write. Sends this image, and if that works,
 * clears a little of the backlog while the link is known good.
 */
void photoSendAndCatchUp(const char *name, const uint8_t *data, size_t len) {
  bool ok = photoSend(name, data, len);
  if (!ok) {
    pendingAdd(name);
    Serial.printf("[link] backlog now %d\n", pendingCount());
    return;
  }

  // Link works right now. Spend a little of that on older images, capped so a
  // long backlog cannot stall the capture cycle.
  for (int i = 0; i < CATCHUP_PER_CYCLE; i++) {
    String old = pendingAt(0);
    if (!old.length()) break;

    String path = "/canopy/" + old;
    File f = SD_MMC.open(path.c_str(), FILE_READ);
    if (!f) { pendingRemove(old.c_str()); continue; }   // gone, stop tracking

    size_t n = f.size();
    if (n == 0 || n > PHOTOLINK_MAX_BYTES) {
      f.close();
      pendingRemove(old.c_str());
      continue;
    }

    uint8_t *buf = (uint8_t *)malloc(n);
    if (!buf) { f.close(); break; }                     // out of RAM, try later
    f.read(buf, n);
    f.close();

    bool sent = photoSend(old.c_str(), buf, n);
    free(buf);

    if (!sent) break;                                   // link went away
    pendingRemove(old.c_str());
    Serial.printf("[link] backlog now %d\n", pendingCount());
  }
}
