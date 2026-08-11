#pragma once
/*
 * Board B image upload over WiFi.
 *
 * Goes at:  ~/agrisync/firmware/esp32cam_capture/src/uploader.h
 *
 * WHY WIFI AND NOT CELLULAR
 *   The modem lives on Board A and its UART already has one master. Putting
 *   a second one on it turns a working uplink into a fragile one. The camera
 *   has WiFi built in and an image is only ~30KB, so while the box is in
 *   range this is the cheapest path that exists.
 *
 *   Out of range, uploadImage() fails fast and the image stays on the SD
 *   card, which is exactly what happens today. Nothing is lost, so a field
 *   deployment degrades to card collection rather than breaking.
 *
 * WHY THE SD WRITE COMES FIRST
 *   The card is the record of truth. Upload is best effort on top of it. A
 *   power cut mid-upload costs you nothing.
 *
 * AUTH
 *   Same HMAC scheme as the sensor readings: the secret never leaves the
 *   board, only a signature derived from it. Someone who finds the URL still
 *   cannot put files on your disk.
 *
 * BUILD FLAGS (platformio.ini)
 *   -DWIFI_SSID='"${sysenv.WIFI_SSID}"'
 *   -DWIFI_PASSWORD='"${sysenv.WIFI_PASSWORD}"'
 *   -DUPLOAD_HOST='"192.168.1.35"'      the Pi's LAN address
 *   -DUPLOAD_PORT=5000
 *   -DHMAC_SECRET='"${sysenv.DEVICE_HMAC_SECRET}"'
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "mbedtls/md.h"

#ifndef UPLOAD_PORT
#define UPLOAD_PORT 5000
#endif

// The camera has no clock. Time comes from NTP, because the server rejects a
// signature more than 15 minutes out and an unset ESP32 clock reads 1970.
#ifndef NTP_SERVER
#define NTP_SERVER "pool.ntp.org"
#endif

#define WIFI_TIMEOUT_MS   15000
#define UPLOAD_TIMEOUT_MS 20000

static bool wifiReady = false;

// ---------------------------------------------------------------------------

static String hmacSha256Hex(const char *key, const uint8_t *msg, size_t len) {
  byte out[32];
  mbedtls_md_context_t ctx;
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);

  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, info, 1);              // 1 = HMAC mode
  mbedtls_md_hmac_starts(&ctx, (const unsigned char *)key, strlen(key));
  mbedtls_md_hmac_update(&ctx, msg, len);
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);

  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", out[i]);
  hex[64] = 0;
  return String(hex);
}

/* Connects and syncs the clock. Safe to call repeatedly - returns fast if
   already up. Never blocks longer than WIFI_TIMEOUT_MS. */
bool uploaderBegin() {
  if (WiFi.status() == WL_CONNECTED) return true;

  Serial.printf("[wifi] connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    delay(300);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] not connected - images stay on the card");
    wifiReady = false;
    return false;
  }

  Serial.printf("[wifi] %s\n", WiFi.localIP().toString().c_str());

  if (time(nullptr) < 1735689600L) {          // before 2025 = clock not set
    configTime(0, 0, NTP_SERVER);             // 0,0 = we want UTC
    uint32_t t0 = millis();
    while (time(nullptr) < 1735689600L && millis() - t0 < 8000) delay(200);
  }

  time_t now = time(nullptr);
  if (now < 1735689600L) {
    Serial.println("[wifi] clock not set - uploads will be rejected");
    wifiReady = false;
    return false;
  }

  Serial.printf("[wifi] clock ok (epoch %ld)\n", (long)now);
  wifiReady = true;
  return true;
}

/*
 * POST one JPEG. Returns true only on 2xx.
 *
 * The image is already in RAM from the capture, so nothing is re-read from
 * the card. Call this straight after the SD write.
 */
bool uploadImage(const char *filename, const uint8_t *jpg, size_t len) {
  if (!uploaderBegin()) return false;

  long ts = (long)time(nullptr);

  // Signed string is "<ts>." followed by the raw image bytes. Building it in
  // one buffer keeps it identical to what the server recomputes.
  size_t prefixLen = snprintf(nullptr, 0, "%ld.", ts);
  size_t total = prefixLen + len;

  uint8_t *buf = (uint8_t *)malloc(total);
  if (!buf) {
    Serial.println("[up] out of memory for signing");
    return false;
  }
  snprintf((char *)buf, prefixLen + 1, "%ld.", ts);
  memcpy(buf + prefixLen, jpg, len);

  String sig = hmacSha256Hex(HMAC_SECRET, buf, total);
  free(buf);

  String url = String("http://") + UPLOAD_HOST + ":" + String(UPLOAD_PORT)
             + "/api/v1/photos";

  HTTPClient http;
  http.begin(url);
  http.setTimeout(UPLOAD_TIMEOUT_MS);
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("X-Sig", String(ts) + "." + sig);
  http.addHeader("X-Filename", filename);

  int code = http.POST((uint8_t *)jpg, len);
  String body = (code > 0) ? http.getString() : String();
  http.end();

  if (code >= 200 && code < 300) {
    Serial.printf("[up] %s uploaded (%u bytes)\n", filename, (unsigned)len);
    return true;
  }

  Serial.printf("[up] failed, HTTP %d %s\n", code, body.c_str());
  if (code == 401) {
    Serial.println("     signature rejected: HMAC_SECRET mismatch, or clock skew");
  } else if (code <= 0) {
    Serial.println("     no reply: is UPLOAD_HOST right, is the API running,");
    Serial.println("     is Flask bound to 0.0.0.0 rather than 127.0.0.1?");
  }
  Serial.println("     image is safe on the SD card either way");
  return false;
}
