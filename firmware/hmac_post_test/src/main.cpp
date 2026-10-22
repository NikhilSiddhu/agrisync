/*
 * AgriSync - signed POST over plain HTTP   v2
 *
 * v1 result: everything worked except the clock.
 *   USERDATA custom header      -> OK      (this was the big unknown)
 *   HTTPDATA body               -> OK
 *   HTTPACTION                  -> 1,401,25
 *   server replied              -> {"error":"unauthorized"}
 *
 *   Cause: +CCLK: "70/01/01" - the modem never got network time, so the
 *   signature carried a 1970 timestamp and the server rejected it as far
 *   outside the 15 minute window. Correct behaviour on the server's part.
 *
 * v2 adds:
 *   - AT+CTZU=1, so the network can set the clock when it offers
 *   - NTP sync over the cellular link, since Airtel did not offer
 *   - a plausibility check on the epoch instead of a naive year test
 *   - packet-data recovery: ask to attach, then cycle the radio
 *
 * The recovery logic is not just for this test. A box in a field cannot have
 * someone pull a fuse for it, and CGATT has dropped to 0 twice already.
 *
 * Goes at:  ~/agrisync/firmware/hmac_post_test/src/main.cpp
 */

#include <Arduino.h>
#include "mbedtls/md.h"

// ======================== CHANGE THIS ========================
#define HMAC_SECRET "PUT_THE_SECRET_HERE"
// =============================================================

#define API_HOST "ingest.agrisync.tech"
#define API_PATH "/api/v1/sensors/readings"
#define DEVICE_ID "esp32-smoke-01"

#define MODEM_TX_PIN 26
#define MODEM_RX_PIN 27
#define MODEM_BAUD   115200
#define QUIET_MS 500

// Anything before 2025-01-01 means the clock is not set.
#define EPOCH_FLOOR 1735689600L

// ---------------------------------------------------------------------------

void drain() {
  uint32_t last = millis();
  while (millis() - last < QUIET_MS) {
    while (Serial2.available()) { Serial.write(Serial2.read()); last = millis(); }
    delay(10);
  }
}

String cmd(const char *c, const char *want = "OK", uint32_t t = 5000) {
  while (Serial2.available()) Serial2.read();
  Serial.printf("\n>> %s\n", c);
  Serial2.print(c); Serial2.print("\r\n");

  String buf; buf.reserve(512);
  uint32_t start = millis();
  while (millis() - start < t) {
    while (Serial2.available()) {
      char ch = (char)Serial2.read();
      Serial.write(ch);
      if (buf.length() < 3000) buf += ch;
    }
    if (want && buf.indexOf(want) >= 0) break;
    if (buf.indexOf("ERROR") >= 0) break;
    delay(5);
  }
  drain();
  return buf;
}

bool ok(const char *c, uint32_t t = 5000) { return cmd(c, "OK", t).indexOf("OK") >= 0; }
bool ok(const String &c, uint32_t t = 5000) { return ok(c.c_str(), t); }

int actionStatus(const String &r) {
  int i = r.indexOf("+HTTPACTION:");
  if (i < 0) return -1;
  int c1 = r.indexOf(',', i), c2 = r.indexOf(',', c1 + 1);
  if (c1 < 0 || c2 < 0) return -1;
  return r.substring(c1 + 1, c2).toInt();
}

// ---------------------------------------------------------------------------
// HMAC-SHA256. mbedtls ships with the ESP32 core, nothing to install.
// ---------------------------------------------------------------------------

String hmacSha256Hex(const String &key, const String &msg) {
  byte out[32];
  mbedtls_md_context_t ctx;
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);

  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, info, 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char *)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char *)msg.c_str(), msg.length());
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);

  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", out[i]);
  hex[64] = 0;
  return String(hex);
}

// ---------------------------------------------------------------------------
// Packet data. CGATT has dropped to 0 twice, so ask rather than just poll.
// ---------------------------------------------------------------------------

bool attached() {
  return cmd("AT+CGATT?", "OK", 5000).indexOf("+CGATT: 1") >= 0;
}

bool ensureAttached() {
  Serial.println("\n\n========== PACKET DATA ==========");

  for (int i = 0; i < 6; i++) { if (attached()) { Serial.println("\n[ok] attached"); return true; } delay(3000); }

  Serial.println("\n[recover] asking to attach");
  ok("AT+CGATT=1", 20000);
  for (int i = 0; i < 6; i++) { if (attached()) { Serial.println("\n[ok] attached"); return true; } delay(3000); }

  Serial.println("\n[recover] cycling the radio");
  ok("AT+CFUN=0", 15000);
  delay(3000);
  ok("AT+CFUN=1", 15000);
  delay(8000);
  for (int i = 0; i < 15; i++) { if (attached()) { Serial.println("\n[ok] attached"); return true; } delay(3000); }

  Serial.println("\n[FAIL] still not attached.");
  Serial.println("       Check the SIM has an active data plan.");
  return false;
}

// ---------------------------------------------------------------------------
// Time. Without a correct clock the signature is worthless, because the
// server rejects anything more than 15 minutes off.
// ---------------------------------------------------------------------------

long readClock(bool quiet = false) {
  String r = cmd("AT+CCLK?", "OK", 5000);
  int q = r.indexOf('"');
  if (q < 0) return 0;

  int yy = r.substring(q + 1,  q + 3).toInt();
  int mo = r.substring(q + 4,  q + 6).toInt();
  int dd = r.substring(q + 7,  q + 9).toInt();
  int hh = r.substring(q + 10, q + 12).toInt();
  int mi = r.substring(q + 13, q + 15).toInt();
  int ss = r.substring(q + 16, q + 18).toInt();

  int tzq = 0;
  char sign = r.charAt(q + 18);
  if (sign == '+' || sign == '-') {
    tzq = r.substring(q + 19, q + 21).toInt();
    if (sign == '-') tzq = -tzq;
  }

  struct tm t = {};
  t.tm_year = (2000 + yy) - 1900;
  t.tm_mon  = mo - 1;
  t.tm_mday = dd;
  t.tm_hour = hh;
  t.tm_min  = mi;
  t.tm_sec  = ss;

  long utc = mktime(&t) - (long)tzq * 15 * 60;

  // "70/01/01" is the unset default. Rather than guess at year ranges, just
  // check the result is a plausible date.
  if (utc < EPOCH_FLOOR) {
    if (!quiet) Serial.printf("\n[time] clock not set (reads 20%02d-%02d-%02d)\n", yy, mo, dd);
    return 0;
  }

  Serial.printf("\n[time] 20%02d-%02d-%02d %02d:%02d:%02d UTC%+d  epoch %ld\n",
                yy, mo, dd, hh, mi, ss, tzq / 4, utc);
  return utc;
}

long syncTime() {
  Serial.println("\n\n========== CLOCK ==========");

  long t = readClock(true);
  if (t) { Serial.println("[time] already set"); return t; }

  // Let the network set it if it ever offers. Airtel did not, but this
  // costs nothing and other networks do.
  ok("AT+CTZU=1");

  // NTP over the cellular link. Timezone 0 so we get UTC directly and skip
  // the offset arithmetic entirely.
  ok("AT+CNTPCID=1", 3000);

  const char *servers[] = { "pool.ntp.org", "time.google.com", "in.pool.ntp.org" };
  for (auto s : servers) {
    Serial.printf("\n[time] trying %s\n", s);
    ok(String("AT+CNTP=\"") + s + "\",0");
    cmd("AT+CNTP", "+CNTP:", 30000);
    delay(1500);
    t = readClock();
    if (t) { Serial.println("[time] synced"); return t; }
  }

  Serial.println("\n[FAIL] could not set the clock.");
  Serial.println("       Without it the signature cannot be validated.");
  return 0;
}

// ---------------------------------------------------------------------------

bool signedPost(long ts) {
  Serial.println("\n\n========== SIGNED POST ==========");

  String body = String("{\"device_id\":\"" DEVICE_ID "\",")
              + "\"air_temp_c\":31.2,"
              + "\"humidity_pct\":74.5,"
              + "\"sent_at\":" + String(ts) + "}";

  String sig    = hmacSha256Hex(HMAC_SECRET, String(ts) + "." + body);
  String header = String(ts) + "." + sig;

  Serial.printf("\n[body] %s\n", body.c_str());
  Serial.printf("[sig ] X-Sig: %s\n", header.c_str());

  ok("AT+HTTPTERM", 3000);
  if (!ok("AT+HTTPINIT", 8000)) { Serial.println("[FAIL] HTTPINIT"); return false; }

  ok("AT+HTTPPARA=\"URL\",\"http://" API_HOST API_PATH "\"");
  ok("AT+HTTPPARA=\"CONTENT\",\"application/json\"");
  ok(String("AT+HTTPPARA=\"USERDATA\",\"X-Sig: ") + header + "\"");

  String req = "AT+HTTPDATA=" + String(body.length()) + ",10000";
  if (cmd(req.c_str(), "DOWNLOAD", 8000).indexOf("DOWNLOAD") < 0) {
    Serial.println("\n[FAIL] no DOWNLOAD prompt");
    ok("AT+HTTPTERM", 3000);
    return false;
  }

  Serial2.print(body);                 // exactly the body, no terminator
  if (cmd("", "OK", 12000).indexOf("OK") < 0) {
    Serial.println("\n[FAIL] body rejected");
    ok("AT+HTTPTERM", 3000);
    return false;
  }

  int s = actionStatus(cmd("AT+HTTPACTION=1", "+HTTPACTION:", 60000));
  Serial.printf("\n\n>>> HTTP %d\n", s);

  cmd("AT+HTTPREAD=0,400", "OK", 15000);
  ok("AT+HTTPTERM", 5000);

  if (s >= 200 && s < 300) {
    Serial.println("\n\n**********************************");
    Serial.println("  PASS - signed, sent, stored.");
    Serial.println("  Confirm the document in MongoDB.");
    Serial.println("**********************************");
    return true;
  }

  Serial.println("\n[FAIL]");
  Serial.println("  301  Cloudflare forcing HTTPS. Check the hostname");
  Serial.println("  401  signature rejected. Secret mismatch, or clock skew");
  Serial.println("  400  signature fine, body malformed");
  Serial.println("  500  DEVICE_HMAC_SECRET missing on the Pi");
  return false;
}

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  Serial2.begin(MODEM_BAUD, SERIAL_8N1, MODEM_RX_PIN, MODEM_TX_PIN);
  delay(1500);

  Serial.println("\n\n=== AgriSync signed POST test v2 ===");
  Serial.printf("host %s\n", API_HOST);

  if (String(HMAC_SECRET) == "PUT_THE_SECRET_HERE") {
    Serial.println("\n*** HMAC_SECRET is still the placeholder. Will 401. ***");
  }

  bool alive = false;
  for (int i = 0; i < 15; i++) { if (ok("AT", 1000)) { alive = true; break; } delay(1000); }
  if (!alive) { Serial.println("[FAIL] modem silent"); return; }

  ok("ATE0");
  ok("AT+CMEE=2");
  ok("AT+HTTPTERM", 3000);
  cmd("AT+CSQ");

  if (!ensureAttached()) return;

  long ts = syncTime();
  if (!ts) return;

  signedPost(ts);

  Serial.println("\n=== done. press RST to repeat ===");
}

void loop() {}
