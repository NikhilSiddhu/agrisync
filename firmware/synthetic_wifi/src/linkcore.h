#pragma once
/*
 * The box's side of the link to the Pi (firmware 1.6.0): the parts with no
 * hardware in them, so they can be tested on a computer
 * (test/link_host_test.cpp).
 *
 *   Settings  Every reply to a reading can carry a signed config string from
 *             the Pi (src/device_link.py on the server):
 *                 "cfg":"v=1;i=300;b=12[;ota=1.6.1;osz=N;osha=<hex>]"
 *                 "cfg_ts":1793170000, "cfg_sig":"<hex HMAC>"
 *             The box applies it only if the HMAC over "<cfg_ts>.<cfg>"
 *             matches, cfg_ts is within 15 minutes of its own clock, and it
 *             is newer than the last config it applied (no replays).
 *
 *   Interval  i, clamped to 60..1800 s, then slowed by the box itself when
 *             the battery is low, whatever the Pi asked for.
 *
 *   Updates   ota/osz/osha name an image; the download, hashing and
 *             installing live in ota.h. SHA-256 and base64 are written out
 *             here rather than borrowed from mbedtls, so the same code runs
 *             on the board and in the test, across core versions.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const long CFG_MAX_SKEW_S = 900;
static const long IVL_MIN_S = 60, IVL_MAX_S = 1800;
static const int  BATCH_MAX = 12;
static const long OTA_SLOT_BYTES = 0x140000;   // one app slot of the default esp32dev partition table

struct BoxCfg {
  int    v = 0;
  long   interval_s = 0;   // 0: not given
  int    batch = 0;        // 0: not given
  String ota;              // version on offer, empty if none
  long   osz = 0;
  String osha;             // 64 lowercase hex chars
};

/* ---------------------------------------------------------------- strings */

static bool isHex(const String &s, unsigned n) {
  if (s.length() != n) return false;
  for (unsigned i = 0; i < n; i++) {
    char c = s[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  return true;
}

/* 1.6.0, 1.6, 1.6.0.2: two to four whole numbers with dots. */
static bool isVersion(const String &s) {
  if (s.length() < 3 || s.length() > 15) return false;
  int dots = 0;
  bool digit = false;
  for (unsigned i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c >= '0' && c <= '9') digit = true;
    else if (c == '.' && digit) { dots++; digit = false; }
    else return false;
  }
  return digit && dots >= 1 && dots <= 3;
}

/* <0, 0, >0 like strcmp, number by number: 1.10.0 is after 1.9.0, and 1.6
   equals 1.6.0. */
static int versionCmp(const char *a, const char *b) {
  for (int part = 0; part < 4; part++) {
    char *ea, *eb;
    long x = strtol(a, &ea, 10), y = strtol(b, &eb, 10);
    if (x != y) return x < y ? -1 : 1;
    a = (*ea == '.') ? ea + 1 : ea;
    b = (*eb == '.') ? eb + 1 : eb;
    if (!*a && !*b) return 0;
  }
  return 0;
}

/* Constant time, so a wrong signature takes as long to refuse as a nearly right one. */
static bool ctEqual(const String &a, const String &b) {
  if (a.length() != b.length()) return false;
  uint8_t d = 0;
  for (unsigned i = 0; i < a.length(); i++) d |= (uint8_t)(a[i] ^ b[i]);
  return d == 0;
}

/* The value of key in "k=v;k=v". */
static bool cfgValue(const String &cfg, const char *key, String &out) {
  size_t kl = strlen(key);
  const char *s = cfg.c_str();
  while (*s) {
    const char *end = strchr(s, ';');
    size_t len = end ? (size_t)(end - s) : strlen(s);
    if (len > kl && strncmp(s, key, kl) == 0 && s[kl] == '=') {
      char buf[80];
      size_t vl = len - kl - 1;
      if (vl >= sizeof(buf)) return false;
      memcpy(buf, s + kl + 1, vl);
      buf[vl] = 0;
      out = String(buf);
      return true;
    }
    if (!end) break;
    s = end + 1;
  }
  return false;
}

static long toLong(const String &s, bool &ok) {
  char *end = nullptr;
  long v = strtol(s.c_str(), &end, 10);
  ok = s.length() > 0 && end && *end == 0;
  return v;
}

/* Fields that are present and sensible; anything else is left unset. False
   if the string does not even carry a version. */
static bool cfgParse(const String &cfg, BoxCfg &c) {
  c = BoxCfg();
  String v;
  bool ok;
  if (!cfgValue(cfg, "v", v)) return false;
  c.v = (int)toLong(v, ok);
  if (!ok) return false;
  if (cfgValue(cfg, "i", v)) {
    long i = toLong(v, ok);
    if (ok && i >= IVL_MIN_S && i <= IVL_MAX_S) c.interval_s = i;
  }
  if (cfgValue(cfg, "b", v)) {
    long b = toLong(v, ok);
    if (ok && b >= 1 && b <= BATCH_MAX) c.batch = (int)b;
  }
  String ver, sz, sha;
  if (cfgValue(cfg, "ota", ver) && cfgValue(cfg, "osz", sz) && cfgValue(cfg, "osha", sha)) {
    long n = toLong(sz, ok);
    if (isVersion(ver) && ok && n > 0 && n <= OTA_SLOT_BYTES && isHex(sha, 64)) {
      c.ota = ver; c.osz = n; c.osha = sha;
    }
  }
  return true;
}

/* ---------------------------------------------------------------- the reply */

/* A string value from a flat JSON reply, as Flask writes it. Enough for the
   Pi's own reply: no escapes in these values. */
static bool jsonStr(const String &body, const char *key, String &out) {
  char pat[40];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char *s = strstr(body.c_str(), pat);
  if (!s) return false;
  s += strlen(pat);
  while (*s == ' ') s++;
  if (*s++ != ':') return false;
  while (*s == ' ') s++;
  if (*s++ != '"') return false;
  const char *e = strchr(s, '"');
  if (!e || e - s > 300) return false;
  char buf[304];
  memcpy(buf, s, e - s);
  buf[e - s] = 0;
  out = String(buf);
  return true;
}

static bool jsonLong(const String &body, const char *key, long &out) {
  char pat[40];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char *s = strstr(body.c_str(), pat);
  if (!s) return false;
  s += strlen(pat);
  while (*s == ' ') s++;
  if (*s++ != ':') return false;
  while (*s == ' ') s++;
  char *end = nullptr;
  long v = strtol(s, &end, 10);
  if (end == s) return false;
  out = v;
  return true;
}

static bool replyConfig(const String &body, String &cfg, long &ts, String &sig) {
  return jsonStr(body, "cfg", cfg) && jsonLong(body, "cfg_ts", ts) && jsonStr(body, "cfg_sig", sig);
}

enum CfgVerdict { CFG_OK = 0, CFG_NONE, CFG_BADSIG, CFG_STALE, CFG_REPLAY };

/* expect: HMAC-SHA256(secret, "<ts>.<cfg>") in hex, worked out by the caller. */
static CfgVerdict cfgCheck(const String &cfg, long ts, const String &sig, const String &expect,
                           long now, long lastTs) {
  if (!cfg.length() || !sig.length()) return CFG_NONE;
  if (!ctEqual(sig, expect)) return CFG_BADSIG;
  if (now <= 0 || labs(now - ts) > CFG_MAX_SKEW_S) return CFG_STALE;
  if (ts <= lastTs) return CFG_REPLAY;
  return CFG_OK;
}

/* What the box actually runs at: the Pi's wish, clamped, and never faster
   than the battery allows. battV <= 1 means "not measured". */
static long effectiveInterval(long wanted, float battV) {
  long i = wanted < IVL_MIN_S ? IVL_MIN_S : wanted > IVL_MAX_S ? IVL_MAX_S : wanted;
  if (battV > 1.0f) {
    if (battV < 11.6f && i < 900) i = 900;
    else if (battV < 11.9f && i < 600) i = 600;
  }
  return i;
}

static String chunkPath(const String &ver, long off, long len) {
  char buf[96];
  snprintf(buf, sizeof(buf), "/api/v1/firmware/chunk?v=%s&off=%ld&len=%ld", ver.c_str(), off, len);
  return String(buf);
}

/* ---------------------------------------------------------------- base64 */

static int b64val(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

/* Decodes into out (room for cap bytes). Whitespace is skipped; anything
   else that is not base64 makes it fail. Returns bytes written, or -1. */
static long b64decode(const char *in, size_t n, uint8_t *out, size_t cap) {
  uint32_t acc = 0;
  int bits = 0, pad = 0;
  size_t o = 0;
  for (size_t i = 0; i < n; i++) {
    char c = in[i];
    if (c == '\r' || c == '\n' || c == ' ') continue;
    if (c == '=') { pad++; continue; }
    if (pad) return -1;                       // data after padding
    int v = b64val(c);
    if (v < 0) return -1;
    acc = (acc << 6) | (uint32_t)v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (o >= cap) return -1;
      out[o++] = (uint8_t)(acc >> bits);
    }
  }
  if (pad > 2) return -1;
  return (long)o;
}

/* ---------------------------------------------------------------- SHA-256 */

class Sha256 {
 public:
  Sha256() { reset(); }
  void reset() {
    static const uint32_t init[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(h, init, sizeof(h));
    len = 0;
    fill = 0;
  }
  void update(const uint8_t *p, size_t n) {
    len += n;
    while (n) {
      size_t k = 64 - fill < n ? 64 - fill : n;
      memcpy(buf + fill, p, k);
      fill += k; p += k; n -= k;
      if (fill == 64) { block(buf); fill = 0; }
    }
  }
  /* 64 lowercase hex chars into out[65]. Ends this hash. */
  void hex(char out[65]) {
    uint64_t bitsLen = len * 8;
    uint8_t one = 0x80, zero = 0;
    update(&one, 1);
    while (fill != 56) update(&zero, 1);
    uint8_t lenb[8];
    for (int i = 0; i < 8; i++) lenb[i] = (uint8_t)(bitsLen >> (56 - 8 * i));
    update(lenb, 8);
    for (int i = 0; i < 8; i++) snprintf(out + i * 8, 9, "%08x", (unsigned)h[i]);
    out[64] = 0;
  }

 private:
  uint32_t h[8];
  uint8_t buf[64];
  size_t fill;
  uint64_t len;
  static uint32_t ror(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
  void block(const uint8_t *p) {
    static const uint32_t K[64] = {
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2};
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
      w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
      uint32_t s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
      uint32_t s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
      uint32_t S1 = ror(e, 6) ^ ror(e, 11) ^ ror(e, 25);
      uint32_t ch = (e & f) ^ (~e & g);
      uint32_t t1 = hh + S1 + ch + K[i] + w[i];
      uint32_t S0 = ror(a, 2) ^ ror(a, 13) ^ ror(a, 22);
      uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
      uint32_t t2 = S0 + mj;
      hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
  }
};
