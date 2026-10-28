#pragma once
/*
 * SIMCOM A7670C over UART2, signed HTTP to the AgriSync ingest endpoint.
 *
 * REPLACES the version written from the SIM7080 manual before the hardware
 * arrived. Every command in that file (AT+CNACT, AT+SHCONF, AT+SHCONN,
 * AT+SHREQ) returns ERROR on an A7670C. Verified: AT+CNACT? gives a bare
 * ERROR with CMEE=2 set, while AT+HTTPINIT gives OK.
 *
 * v2 fixes two parsing bugs found on the first live run:
 *   - readUntil() returned the instant a token matched, so "+HTTPACTION:"
 *     was captured without the " 1,201,132" that follows it and every POST
 *     reported -1. Now it collects the rest of the line.
 *   - localIP() looked for quotes. This firmware answers
 *     "+CGPADDR: 1,100.66.107.166" with none.
 *
 * WHY PLAIN HTTP, NOT HTTPS
 *   This modem cannot complete a TLS handshake with Cloudflare's edge.
 *   Measured: every Cloudflare host returns error 715, howsmyssl.com and
 *   thingspeak return 200. Cloudflare's Universal SSL serves ECDSA
 *   certificates and this firmware cannot negotiate one.
 *
 * WHY THAT IS STILL SAFE
 *   Each payload is signed with HMAC-SHA256. The secret is never sent, only
 *   a signature derived from it, so watching the traffic gains an attacker
 *   nothing reusable. They can read the temperature. They cannot forge one,
 *   and forgery is the failure that matters.
 *
 * WIRING
 *   ESP32 GPIO26 (TX2) -> modem RXD
 *   ESP32 GPIO27 (RX2) <- modem TXD
 *   common ground, modem on its own supply with 1000uF at its pins
 *
 * POWER
 *   The A7670C draws up to 2A in bursts. A supply that sags resets it
 *   mid-request, which looks exactly like a firmware bug and is not one.
 *
 * ANTENNA
 *   Attached before power, every time.
 */

#include <Arduino.h>
#include "mbedtls/md.h"
#include "mbedtls/base64.h"

/*
 * Images go over the wire base64 encoded. Measured, not assumed: a JPEG sent
 * raw through AT+HTTPDATA arrives with the SAME byte count and a valid JPEG
 * header, but a different hash - so the modem is substituting byte values in
 * flight, not dropping them. JSON readings never hit this because they are
 * plain ASCII.
 *
 * Base64 puts the image in that same safe character set. It costs 33% more
 * data (a 45KB image becomes 60KB, roughly 5.5MB/day at 15 minute intervals)
 * which is nothing on a data SIM, and it removes a whole class of problem.
 *
 * Encoded in 3072-byte chunks. That is a multiple of 3, so each chunk encodes
 * cleanly with no padding until the last one, and the pieces concatenate to
 * exactly base64(whole image). Chunking keeps a 60KB string off the heap.
 */
#define B64_CHUNK_IN  3072
#define B64_CHUNK_OUT 4100

#ifndef MODEM_TRACE
#define MODEM_TRACE 0
#endif

// Anything before 2025-01-01 means the clock was never set.
#define MODEM_EPOCH_FLOOR 1735689600L

class Modem {
public:
  Modem(HardwareSerial &port, int rxPin, int txPin, const char *apn)
      : _s(port), _rx(rxPin), _tx(txPin), _apn(apn) {}

  void begin(uint32_t baud = 115200) {
    _s.begin(baud, SERIAL_8N1, _rx, _tx);
    delay(200);
  }

  /* The A7670 ignores the first few bytes after power-up, so be patient
     rather than clever. */
  bool sync(uint8_t attempts = 15) {
    for (uint8_t i = 0; i < attempts; i++) {
      if (at("AT", 1000)) {
        at("ATE0", 1000);          // stop it echoing our own commands back
        at("AT+CMEE=2", 1000);     // verbose errors, worth having
        return true;
      }
      delay(800);
    }
    return false;
  }

  bool simReady()   { return atContains("AT+CPIN?", "READY", 5000); }
  bool registered() { return atContains("AT+CREG?", ",1", 3000)
                          || atContains("AT+CREG?", ",5", 3000); }
  bool attached()   { return atContains("AT+CGATT?", "+CGATT: 1", 8000); }

  int signal() {
    String r;
    if (!atRead("AT+CSQ", r, 3000)) return 99;
    int i = r.indexOf("+CSQ:");
    if (i < 0) return 99;
    return r.substring(i + 6, r.indexOf(',', i)).toInt();   // 0-31, 99 = none
  }

  /*
   * Bring up packet data, and recover it if it has dropped.
   *
   * CGATT going to 0 while signal stays strong has happened repeatedly on
   * this SIM. A box in a field cannot have someone pull a fuse for it, so
   * this asks, and then cycles the radio if asking does not work.
   */
  bool dataUp() {
    char buf[96];
    snprintf(buf, sizeof(buf), "AT+CGDCONT=1,\"IP\",\"%s\"", _apn);
    at(buf, 5000);

    for (int i = 0; i < 6; i++) { if (attached()) return true; delay(2500); }

    trace("--", "not attached, asking");
    at("AT+CGATT=1", 20000);
    for (int i = 0; i < 6; i++) { if (attached()) return true; delay(2500); }

    trace("--", "still not attached, cycling the radio");
    at("AT+CFUN=0", 15000);
    delay(3000);
    at("AT+CFUN=1", 15000);
    delay(8000);
    for (int i = 0; i < 15; i++) { if (attached()) return true; delay(2500); }

    return false;
  }

  /* +CGPADDR: 1,100.66.107.166,254.128...    no quotes on this firmware */
  String localIP() {
    String r;
    atRead("AT+CGPADDR=1", r, 3000);
    int i = r.indexOf("+CGPADDR:");
    if (i < 0) return "";
    int c1 = r.indexOf(',', i);
    if (c1 < 0) return "";

    int c2 = r.indexOf(',', c1 + 1);
    int nl = r.indexOf('\r', c1);
    int end = r.length();
    if (c2 > 0)               end = c2;
    if (nl > 0 && nl < end)   end = nl;
    return r.substring(c1 + 1, end);
  }

  // -------------------------------------------------------------------------
  // Clock. Without it the signature is worthless: the server rejects anything
  // more than 15 minutes off. Airtel does not send network time, so NTP over
  // the cellular link is not optional.
  // -------------------------------------------------------------------------

  long epoch() {
    String r;
    if (!atRead("AT+CCLK?", r, 5000)) return 0;
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

    // "70/01/01" is the unset default. Checking the result is plausible beats
    // guessing at year ranges - 70 parsed as 2070 slipped through once.
    return (utc < MODEM_EPOCH_FLOOR) ? 0 : utc;
  }

  /*
   * Returns a usable epoch.
   *
   * Forces one real NTP sync per boot rather than trusting whatever the clock
   * already reads. A clock can be plausible and still wrong: this modem once
   * sat 8 hours fast, which passes every sanity check and fails every
   * signature. After the first successful sync we trust it for the session.
   *
   * CTZU stays OFF. Letting the network supply a timezone is what put those
   * 8 hours on in the first place - Airtel offered an offset that got applied
   * on top of the NTP result.
   */
  long syncTime() {
    long t = epoch();
    if (t && _timeSynced) return t;        // already synced this boot

    at("AT+CTZU=0", 3000);      // do NOT let the network touch the clock
    at("AT+CNTPCID=1", 3000);   // ERROR on some firmware, harmless

    const char *servers[] = { "pool.ntp.org", "time.google.com", "in.pool.ntp.org" };
    for (auto s : servers) {
      char buf[64];
      snprintf(buf, sizeof(buf), "AT+CNTP=\"%s\",0", s);   // 0 = give us UTC
      at(buf, 5000);

      /*
       * AT+CNTP answers OK the moment it accepts the command, and reports
       * the actual outcome much later as a "+CNTP: 0" URC. Waiting only for
       * the OK reads the clock before the sync has landed - which cost a
       * failed POST on every boot until this was fixed.
       */
      String r;
      trace(">>", "AT+CNTP");
      _s.print("AT+CNTP"); _s.print("\r\n");
      bool landed = readUntil("+CNTP:", r, 45000);
      readRestOfLine(r, 2000);
      trace("<<", r);
      settle();

      if (!landed) continue;              // no result, try the next server
      if (r.indexOf("+CNTP: 0") < 0) continue;   // non-zero means it failed

      delay(300);
      t = epoch();
      if (t) { _timeSynced = true; return t; }
    }

    // NTP failed. Fall back to whatever the clock says, but do not mark it
    // synced, so the next attempt tries again.
    return epoch();
  }

  // -------------------------------------------------------------------------
  // Signed POST
  // -------------------------------------------------------------------------

  /*
   * POST one JSON body, signed.
   *
   *   header:  X-Sig: <unix_timestamp>.<hex_hmac_sha256>
   *   signed:  "<unix_timestamp>." + body
   *
   * One header, not two. Embedding a CRLF inside an AT command string to send
   * multiple headers is unreliable on this firmware, so the timestamp travels
   * in the same value after a dot.
   *
   * Returns the HTTP status, -1 if the exchange never completed, or -2 if
   * there is no usable clock.
   */
  int postJson(const char *host, const char *path, const String &body,
               const char *hmacSecret, String *reply = nullptr) {
    long ts = syncTime();
    if (!ts) { trace("--", "no clock, cannot sign"); return -2; }

    String sig = hmacHex(hmacSecret, String(ts) + "." + body);
    String hdr = String(ts) + "." + sig;

    char buf[256];

    at("AT+HTTPTERM", 3000);                       // clear any half-open session
    if (!at("AT+HTTPINIT", 8000)) {
      delay(1000);
      if (!at("AT+HTTPINIT", 8000)) return -1;
    }

    snprintf(buf, sizeof(buf), "AT+HTTPPARA=\"URL\",\"http://%s%s\"", host, path);
    if (!at(buf, 5000)) { at("AT+HTTPTERM", 3000); return -1; }

    at("AT+HTTPPARA=\"CONTENT\",\"application/json\"", 5000);

    String h = "AT+HTTPPARA=\"USERDATA\",\"X-Sig: " + hdr + "\"";
    at(h.c_str(), 5000);

    // The modem answers DOWNLOAD then counts bytes. Anything extra you send
    // becomes part of the payload.
    snprintf(buf, sizeof(buf), "AT+HTTPDATA=%d,10000", body.length());
    trace(">>", buf);
    _s.print(buf); _s.print("\r\n");
    if (!waitFor("DOWNLOAD", 8000)) {
      trace("--", "no DOWNLOAD prompt");
      at("AT+HTTPTERM", 3000);
      return -1;
    }

    // A reading is ~250 bytes and goes in one write. A 1.6.0 batch is up to
    // 3.6 KB, and this modem duplicates bytes when fed much more than that
    // without a pause (see postBinary), so long bodies are paced the same way.
    if (body.length() > 1024) writePaced((const uint8_t *)body.c_str(), body.length());
    else _s.print(body);
    String tmp;
    if (!readUntil("OK", tmp, 12000)) {
      trace("--", "body not accepted");
      at("AT+HTTPTERM", 3000);
      return -1;
    }
    settle();

    trace(">>", "AT+HTTPACTION=1");
    String resp;
    _s.print("AT+HTTPACTION=1"); _s.print("\r\n");     // 1 = POST
    if (!readUntil("+HTTPACTION:", resp, 60000)) {
      trace("--", "no HTTPACTION response");
      at("AT+HTTPTERM", 3000);
      return -1;
    }

    // readUntil stops the moment the token matches, so " 1,201,132" is still
    // arriving. This is the bug that made every POST report -1.
    readRestOfLine(resp, 5000);
    trace("<<", resp);

    long len = 0;
    int status = actionResult(resp, len);

    // From 1.6.0 the reply carries the Pi's signed settings. Read at most
    // 768 bytes of it: the Pi keeps it well under that.
    if (reply) {
      *reply = "";
      if (status >= 200 && status < 300 && len > 0) readBody(0, len > 768 ? 768 : len, *reply);
    }

    at("AT+HTTPTERM", 5000);
    return status;
  }

  /*
   * GET, signed over the path (1.6.0: firmware pieces). There is no body to
   * sign, so the signature covers "<ts>." + path, query included, which ties
   * it to that one request. The reply, up to maxLen bytes, lands in out.
   */
  int getSigned(const char *host, const String &path, const char *hmacSecret,
                String &out, size_t maxLen) {
    out = "";
    long ts = syncTime();
    if (!ts) { trace("--", "no clock, cannot sign"); return -2; }
    String hdr = String(ts) + "." + hmacHex(hmacSecret, String(ts) + "." + path);

    at("AT+HTTPTERM", 3000);
    if (!at("AT+HTTPINIT", 8000)) {
      delay(1000);
      if (!at("AT+HTTPINIT", 8000)) return -1;
    }
    String u = "AT+HTTPPARA=\"URL\",\"http://" + String(host) + path + "\"";
    if (!at(u.c_str(), 5000)) { at("AT+HTTPTERM", 3000); return -1; }
    String h = "AT+HTTPPARA=\"USERDATA\",\"X-Sig: " + hdr + "\"";
    at(h.c_str(), 5000);

    trace(">>", "AT+HTTPACTION=0");
    String resp;
    _s.print("AT+HTTPACTION=0"); _s.print("\r\n");     // 0 = GET
    if (!readUntil("+HTTPACTION:", resp, 60000)) {
      at("AT+HTTPTERM", 3000);
      return -1;
    }
    readRestOfLine(resp, 5000);
    trace("<<", resp);
    long len = 0;
    int status = actionResult(resp, len);
    if (status == 200 && len > 0) {
      if ((size_t)len > maxLen) { at("AT+HTTPTERM", 3000); return -4; }
      // In pieces of up to 1000 bytes: readUntil keeps at most ~1400
      // characters of a reply, and a piece plus its framing fits in that.
      for (long off = 0; off < len; ) {
        long n = len - off > 1000 ? 1000 : len - off;
        String piece;
        if (!readBody(off, n, piece) || (long)piece.length() != n) { at("AT+HTTPTERM", 3000); return -1; }
        out += piece;
        off += n;
      }
    }
    at("AT+HTTPTERM", 5000);
    return status;
  }

  /* HMAC-SHA256 in hex, the same as every signature this file sends. Public
     so main.cpp can check the Pi's signature on the settings it sends back. */
  static String sign(const char *key, const String &msg) { return hmacHex(key, msg); }

  /*
   * POST raw bytes - used for JPEGs from the camera.
   *
   * Separate from postJson because Arduino's String is null-terminated and
   * cannot hold binary safely: a JPEG containing a zero byte would be
   * silently truncated.
   *
   * The signature covers "<ts>." followed by the raw bytes, exactly as for a
   * JSON body, so the server needs only one verification path.
   *
   * Put the filename in the query string of `path`. This firmware can only
   * carry one custom header reliably and the signature has it.
   */
  int postBinary(const char *host, const char *path,
                 const uint8_t *data, size_t len, const char *hmacSecret) {
    long ts = syncTime();
    if (!ts) { trace("--", "no clock, cannot sign"); return -2; }

    size_t b64len = ((len + 2) / 3) * 4;

    if (b64len > 60000) {
      /* AT+HTTPDATA has a body ceiling on this firmware, and its <time>
         argument tops out at 60000 - 90000 is rejected outright and the
         DOWNLOAD prompt never arrives. Measured the hard way. */
      trace("--", "encoded body too large for AT+HTTPDATA");
      return -3;
    }

    /* ---- single buffer, one hash, one write --------------------------------
       The previous version encoded the image twice - once to feed the HMAC,
       once to feed the modem - and started a separate write burst per chunk.
       A size sweep showed that any payload spanning more than one chunk with a
       partial last chunk arrived with exactly one byte wrong, at the first
       character of that final chunk. 21848 bytes sent, one byte altered at
       offset 20480. Always the same place.

       Encoding once and writing one continuous stream removes the boundary,
       and with it the bug. It costs roughly 4/3 of the image in heap, which
       is a fair price for uploads that actually verify. */
    uint8_t *b64 = (uint8_t *)malloc(b64len + 1);
    if (!b64) { trace("--", "no heap for encode buffer"); return -1; }

    size_t olen = 0;
    if (mbedtls_base64_encode(b64, b64len + 1, &olen, data, len) != 0 ||
        olen != b64len) {
      free(b64);
      trace("--", "base64 encode failed");
      return -1;
    }

    char pfx[24];
    int pfxLen = snprintf(pfx, sizeof(pfx), "%ld.", ts);

    /* Signs what actually goes on the wire - the base64 text, not the raw
       image - so any alteration in flight is caught. hmacHex2 hashes the
       prefix and the body without joining them, keeping one more copy of a
       30KB payload off the heap. */
    String hdr = String(ts) + "." +
                 hmacHex2(hmacSecret, (const uint8_t *)pfx, pfxLen, b64, olen);

    char buf[256];

    at("AT+HTTPTERM", 3000);
    if (!at("AT+HTTPINIT", 8000)) {
      delay(1000);
      if (!at("AT+HTTPINIT", 8000)) { free(b64); return -1; }
    }

    snprintf(buf, sizeof(buf),
             "AT+HTTPPARA=\"URL\",\"http://%s%s&enc=b64\"", host, path);
    if (!at(buf, 5000)) { at("AT+HTTPTERM", 3000); free(b64); return -1; }

    at("AT+HTTPPARA=\"CONTENT\",\"text/plain\"", 5000);

    String h = "AT+HTTPPARA=\"USERDATA\",\"X-Sig: " + hdr + "\"";
    at(h.c_str(), 5000);

    snprintf(buf, sizeof(buf), "AT+HTTPDATA=%u,60000", (unsigned)b64len);
    trace(">>", buf);
    _s.print(buf); _s.print("\r\n");
    if (!waitFor("DOWNLOAD", 15000)) {
      trace("--", "no DOWNLOAD prompt");
      at("AT+HTTPTERM", 3000);
      free(b64);
      return -1;
    }

    // One uniform stream. Every piece is the same size except the last, and
    // there are no restarts partway through.
    /* Pacing matters as much as the buffer.

       Measured: fed more than 4096 bytes without a pause, the modem repeats
       two characters at the boundary - not substitution, duplication, which
       shifts everything after it. The old per-chunk encoding inserted a gap
       at roughly that point by accident, which is why exactly-divisible
       payloads used to survive. That was luck, not design.

       So: smaller pieces, flush after each so the UART has genuinely drained
       before more is queued, and a longer breath every 2KB. Costs well under
       a second on a 32KB body, which is nothing against an upload that
       verifies. */
    writePaced(b64, olen);
    free(b64);

    String tmp;
    if (!readUntil("OK", tmp, 30000)) {
      trace("--", "body not accepted");
      at("AT+HTTPTERM", 3000);
      return -1;
    }
    settle();

    trace(">>", "AT+HTTPACTION=1");
    String resp;
    _s.print("AT+HTTPACTION=1"); _s.print("\r\n");
    if (!readUntil("+HTTPACTION:", resp, 90000)) {
      at("AT+HTTPTERM", 3000);
      return -1;
    }
    readRestOfLine(resp, 5000);
    trace("<<", resp);

    int status = -1;
    int i  = resp.indexOf("+HTTPACTION:");
    int c1 = resp.indexOf(',', i);
    int c2 = resp.indexOf(',', c1 + 1);
    if (c1 > 0 && c2 > c1) status = resp.substring(c1 + 1, c2).toInt();

    at("AT+HTTPTERM", 5000);
    return status;
  }

  /* True once NTP has set the modem's clock this power-up. Only then is its
     time worth copying to the board (main.cpp adoptModemClock). */
  bool timeSynced() const { return _timeSynced; }

  void sleep() { at("AT+CSCLK=1", 2000); }
  void wake()  { at("AT+CSCLK=0", 2000); }

private:
  /* 256 bytes at a time, flushed, with a breath every 2 KB (see postBinary). */
  void writePaced(const uint8_t *p, size_t n) {
    size_t sent = 0;
    while (sent < n) {
      size_t w = (n - sent > 256) ? 256 : (n - sent);
      _s.write(p + sent, w);
      _s.flush();
      sent += w;
      delay(4);
      if ((sent & 2047) == 0) delay(20);
    }
  }

  /* "+HTTPACTION: <method>,<status>,<length>" -> status, and length. */
  static int actionResult(const String &resp, long &len) {
    int status = -1;
    len = 0;
    int i  = resp.indexOf("+HTTPACTION:");
    if (i < 0) return -1;
    int c1 = resp.indexOf(',', i);
    int c2 = c1 > 0 ? resp.indexOf(',', c1 + 1) : -1;
    if (c1 > 0 && c2 > c1) {
      status = resp.substring(c1 + 1, c2).toInt();
      len = resp.substring(c2 + 1).toInt();
    }
    return status;
  }

  /*
   * n bytes of the reply from offset off:
   *     AT+HTTPREAD=<off>,<n>
   *     OK
   *     +HTTPREAD: <n>
   *     <n bytes>
   *     +HTTPREAD: 0
   * Only text replies come this way (JSON, base64), so the bytes are safe in
   * a String.
   */
  bool readBody(long off, long n, String &out) {
    out = "";
    char cmd[48];
    snprintf(cmd, sizeof(cmd), "AT+HTTPREAD=%ld,%ld", off, n);
    while (_s.available()) _s.read();
    trace(">>", cmd);
    _s.print(cmd); _s.print("\r\n");
    String r;
    if (!readUntil("+HTTPREAD: 0", r, 15000)) { trace("--", "no HTTPREAD end"); return false; }
    settle();
    int i = r.indexOf("+HTTPREAD:");
    while (i >= 0) {
      int j = i + 10;
      while (j < (int)r.length() && r[j] == ' ') j++;
      if (r.substring(j, j + 5) == "DATA,") j += 5;      // some firmware says "+HTTPREAD: DATA,<n>"
      long got = r.substring(j).toInt();
      int nl = r.indexOf('\n', j);
      if (got > 0 && nl > 0) {
        if (nl + 1 + got > (long)r.length()) return false;
        out = r.substring(nl + 1, nl + 1 + got);
        return true;
      }
      i = r.indexOf("+HTTPREAD:", i + 10);
    }
    return false;
  }

  HardwareSerial &_s;
  int _rx, _tx;
  const char *_apn;
  bool _timeSynced = false;

  static String hmacHex(const char *key, const String &msg) {
    byte out[32];
    mbedtls_md_context_t ctx;
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);

    mbedtls_md_init(&ctx);
    mbedtls_md_setup(&ctx, info, 1);            // 1 = HMAC mode
    mbedtls_md_hmac_starts(&ctx, (const unsigned char *)key, strlen(key));
    mbedtls_md_hmac_update(&ctx, (const unsigned char *)msg.c_str(), msg.length());
    mbedtls_md_hmac_finish(&ctx, out);
    mbedtls_md_free(&ctx);

    char hex[65];
    for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", out[i]);
    hex[64] = 0;
    return String(hex);
  }

  /* HMAC over two buffers without joining them. Used by postBinary so a
     30KB image never has to be duplicated in RAM just to be signed. */
  static String hmacHex2(const char *key,
                         const uint8_t *a, size_t alen,
                         const uint8_t *b, size_t blen) {
    byte out[32];
    mbedtls_md_context_t ctx;
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);

    mbedtls_md_init(&ctx);
    mbedtls_md_setup(&ctx, info, 1);
    mbedtls_md_hmac_starts(&ctx, (const unsigned char *)key, strlen(key));
    mbedtls_md_hmac_update(&ctx, a, alen);
    mbedtls_md_hmac_update(&ctx, b, blen);
    mbedtls_md_hmac_finish(&ctx, out);
    mbedtls_md_free(&ctx);

    char hex[65];
    for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", out[i]);
    hex[64] = 0;
    return String(hex);
  }

  void trace(const char *dir, const String &s) {
#if MODEM_TRACE
    Serial.printf("  %s %s\n", dir, s.c_str());
#else
    (void)dir; (void)s;
#endif
  }

  bool at(const char *cmd, uint32_t timeout) {
    String r;
    return atRead(cmd, r, timeout) && r.indexOf("OK") >= 0;
  }

  bool atContains(const char *cmd, const char *needle, uint32_t timeout) {
    String r;
    return atRead(cmd, r, timeout) && r.indexOf(needle) >= 0;
  }

  bool atRead(const char *cmd, String &out, uint32_t timeout) {
    while (_s.available()) _s.read();          // clear anything stale
    trace(">>", cmd);
    _s.print(cmd); _s.print("\r\n");
    bool got = readUntil("OK", out, timeout) || out.indexOf("ERROR") >= 0;
    trace("<<", out);
    settle();
    return got;
  }

  /* Let trailing bytes finish arriving before the next command goes out.
     Without this a late reply is read as the answer to the NEXT command and
     every response after it is off by one. */
  void settle(uint32_t quietMs = 200) {
    uint32_t last = millis();
    while (millis() - last < quietMs) {
      while (_s.available()) { _s.read(); last = millis(); }
      delay(5);
    }
  }

  /* readUntil returns the moment its token completes, so whatever follows on
     the same line is still in flight. Append the rest. */
  void readRestOfLine(String &out, uint32_t timeout = 3000) {
    uint32_t start = millis();
    while (millis() - start < timeout) {
      while (_s.available()) {
        char c = (char)_s.read();
        out += c;
        if (c == '\n') return;
      }
      delay(5);
    }
  }

  bool waitFor(const char *token, uint32_t timeout) {
    String s;
    return readUntil(token, s, timeout);
  }

  bool readUntil(const char *token, String &out, uint32_t timeout) {
    out = "";
    uint32_t start = millis();
    while (millis() - start < timeout) {
      while (_s.available()) {
        out += (char)_s.read();
        if (out.indexOf(token) >= 0) return true;
        if (out.length() > 1400) out.remove(0, 700);   // never grow unbounded
      }
      delay(5);
    }
    return false;
  }
};
