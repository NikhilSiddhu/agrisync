/*
 * Host test for the 1.6.0 link to the Pi: signed settings (src/linkcore.h)
 * and the update download (src/ota.h). No board, no modem.
 *
 *   cd firmware/synthetic_wifi
 *   g++ -std=c++17 -Wall -I src -I test test/link_host_test.cpp -lcrypto -o ltest && ./ltest
 *
 * OpenSSL is used only here, as the independent reference for SHA-256 and
 * HMAC; the board uses linkcore.h's own SHA-256 and modem.h's HMAC.
 */
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/sha.h>

#include <cstdio>
#include <functional>
#include <map>
#include <string>
#include <vector>

#include "arduino_shim.h"
#include "linkcore.h"
#include "ota.h"

static int g_pass = 0, g_fail = 0;
static const char *g_case = "";
#define CHECK(c) do { if (c) g_pass++; else { g_fail++; printf("  FAIL [%s] %s:%d: %s\n", g_case, __FILE__, __LINE__, #c); } } while (0)
static void beginCase(const char *n) { g_case = n; printf("== %s\n", n); }

static std::string hexOf(const unsigned char *d, size_t n) {
  std::string s; char b[3];
  for (size_t i = 0; i < n; i++) { snprintf(b, 3, "%02x", d[i]); s += b; }
  return s;
}
static std::string refSha(const std::string &data) {
  unsigned char d[32]; SHA256((const unsigned char *)data.data(), data.size(), d); return hexOf(d, 32);
}
static std::string refHmac(const std::string &key, const std::string &msg) {
  unsigned char d[32]; unsigned int n = 32;
  HMAC(EVP_sha256(), key.data(), (int)key.size(), (const unsigned char *)msg.data(), msg.size(), d, &n);
  return hexOf(d, n);
}
static std::string refB64(const std::string &data) {
  std::string out(4 * ((data.size() + 2) / 3) + 1, '\0');
  int n = EVP_EncodeBlock((unsigned char *)&out[0], (const unsigned char *)data.data(), (int)data.size());
  out.resize(n); return out;
}
static std::string mySha(const std::string &data, size_t step = 0) {
  Sha256 h;
  if (!step) h.update((const uint8_t *)data.data(), data.size());
  else for (size_t i = 0; i < data.size(); i += step) h.update((const uint8_t *)data.data() + i, std::min(step, data.size() - i));
  char hex[65]; h.hex(hex); return hex;
}

// ------------------------------------------------------------ fake Pi for the download
struct FakePi {
  std::string image, version = "1.6.1";
  std::function<int(long off, long len, std::string &body)> tamper;   // return a code to override
  int calls = 0;
  bool begun = false, ended = false, aborted = false;
  std::string written;
  size_t beginSize = 0;
  bool failWrite = false, failEnd = false;
};
static FakePi pi;

int otaFetch(const String &path, String &out) {
  pi.calls++;
  std::string p = path.s;
  long off = -1, len = -1; char v[32] = {0};
  if (sscanf(p.c_str(), "/api/v1/firmware/chunk?v=%31[^&]&off=%ld&len=%ld", v, &off, &len) != 3) return 400;
  if (pi.version != v) return 404;
  if (off < 0 || off >= (long)pi.image.size() || len <= 0 || len > 3072) return 416;
  std::string body = refB64(pi.image.substr(off, len));
  if (pi.tamper) { int c = pi.tamper(off, len, body); if (c) return c; }
  out = String(body);
  return 200;
}
bool otaInstallBegin(size_t size) { pi.begun = true; pi.beginSize = size; pi.written.clear(); return size <= (size_t)OTA_SLOT_BYTES; }
bool otaInstallWrite(const uint8_t *p, size_t n) { if (pi.failWrite) return false; pi.written.append((const char *)p, n); return true; }
bool otaInstallEnd() { if (pi.failEnd) return false; pi.ended = true; return true; }
void otaInstallAbort() { pi.aborted = true; }

static std::string makeImage(size_t n, unsigned seed) {
  std::string s(n, '\0');
  unsigned x = seed;
  for (size_t i = 0; i < n; i++) { x = x * 1103515245u + 12345u; s[i] = (char)(x >> 16); }
  s[0] = (char)0xE9;
  return s;
}
static BoxCfg offer(const std::string &img, const char *ver = "1.6.1", std::string sha = "") {
  BoxCfg c; c.v = 1; c.ota = ver; c.osz = (long)img.size(); c.osha = sha.empty() ? refSha(img).c_str() : sha.c_str();
  return c;
}
static int runToEnd(int maxSteps = 500) {
  int r = 0;
  for (int i = 0; i < maxSteps; i++) { r = otaStep(); if (r != 0 || !otaJob.active) return r; }
  return r;
}
static void resetPi(const std::string &img) { pi = FakePi(); pi.image = img; otaJob = OtaJob(); otaReport = String(); }

int main() {
  // ---------------------------------------------------------- SHA-256
  beginCase("sha256 matches OpenSSL, whole and in pieces");
  CHECK(mySha("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  CHECK(mySha("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  for (size_t n : {1, 55, 56, 63, 64, 65, 119, 128, 3072, 10007, 1048576}) {
    std::string d = makeImage(n, (unsigned)n);
    CHECK(mySha(d) == refSha(d));
    CHECK(mySha(d, 3072) == refSha(d));
    CHECK(mySha(d, 7) == refSha(d));
  }

  // ---------------------------------------------------------- base64
  beginCase("base64 decodes what OpenSSL encodes, refuses junk");
  for (size_t n : {0, 1, 2, 3, 4, 3071, 3072}) {
    std::string d = makeImage(n, 99), e = refB64(d);
    std::vector<uint8_t> out(4000);
    long got = b64decode(e.c_str(), e.size(), out.data(), out.size());
    CHECK(got == (long)n && std::string((char *)out.data(), n) == d);
  }
  { uint8_t o[16]; CHECK(b64decode("SGVsbG8=", 8, o, 16) == 5 && memcmp(o, "Hello", 5) == 0);
    CHECK(b64decode("SGV$bG8=", 8, o, 16) == -1);
    CHECK(b64decode("SGVsbG8=QQ", 10, o, 16) == -1);
    CHECK(b64decode("SGVsbG8gd29ybGQ=", 16, o, 4) == -1); }

  // ---------------------------------------------------------- versions
  beginCase("versions");
  CHECK(versionCmp("1.6.1", "1.6.0") > 0);
  CHECK(versionCmp("1.10.0", "1.9.9") > 0);
  CHECK(versionCmp("1.6", "1.6.0") == 0);
  CHECK(versionCmp("1.5.0", "1.6.0") < 0);
  CHECK(isVersion("1.6.1") && isVersion("2.0") && !isVersion("1") && !isVersion("1..2") && !isVersion("v1.6") && !isVersion("1.6.") );

  // ---------------------------------------------------------- config string
  beginCase("config: parse what the Pi sends, drop what is out of range");
  std::string sha = refSha("x");
  BoxCfg c;
  CHECK(cfgParse(String("v=1;i=150;b=12"), c) && c.v == 1 && c.interval_s == 150 && c.batch == 12 && c.ota.length() == 0);
  CHECK(cfgParse(String(("v=1;i=300;b=8;ota=1.6.1;osz=1040000;osha=" + sha).c_str()), c) &&
        std::string(c.ota.c_str()) == "1.6.1" && c.osz == 1040000 && std::string(c.osha.c_str()) == sha);
  CHECK(cfgParse(String("v=1;i=5;b=99"), c) && c.interval_s == 0 && c.batch == 0);
  CHECK(cfgParse(String(("v=1;ota=1.6.1;osz=99999999;osha=" + sha).c_str()), c) && c.ota.length() == 0);
  CHECK(cfgParse(String("v=1;ota=1.6.1;osz=1000;osha=ABC"), c) && c.ota.length() == 0);
  CHECK(cfgParse(String("v=1;ota=latest;osz=1000;osha=" + sha), c) && c.ota.length() == 0);
  CHECK(!cfgParse(String("i=150"), c));
  CHECK(cfgParse(String("v=2;iv=150;i=200"), c) && c.interval_s == 200);

  beginCase("config: read from the Pi's reply, compact or spaced");
  String cfg, sig; long ts = 0;
  String compact("{\"status\":\"stored\",\"id\":\"x\",\"cfg\":\"v=1;i=150;b=12\",\"cfg_sig\":\"abcd\",\"cfg_ts\":1793170000,\"quality_flags\":[]}");
  CHECK(replyConfig(compact, cfg, ts, sig) && std::string(cfg.c_str()) == "v=1;i=150;b=12" && ts == 1793170000 && std::string(sig.c_str()) == "abcd");
  String spaced("{\n  \"cfg\": \"v=1;i=300;b=12\",\n  \"cfg_sig\": \"ef01\",\n  \"cfg_ts\": 1793170001\n}");
  CHECK(replyConfig(spaced, cfg, ts, sig) && std::string(cfg.c_str()) == "v=1;i=300;b=12" && ts == 1793170001);
  CHECK(!replyConfig(String("{\"status\":\"stored\"}"), cfg, ts, sig));

  beginCase("config: signature, freshness, no replays");
  std::string key = "test-hmac-secret-not-real", cs = "v=1;i=150;b=12";
  long now = 1793170000;
  std::string good = refHmac(key, std::to_string(now) + "." + cs);
  String expect(good.c_str());
  CHECK(cfgCheck(String(cs.c_str()), now, expect, expect, now + 30, now - 300) == CFG_OK);
  std::string tampered = refHmac(key, std::to_string(now) + ".v=1;i=60;b=12");
  CHECK(cfgCheck(String(cs.c_str()), now, String(tampered.c_str()), expect, now, 0) == CFG_BADSIG);
  CHECK(cfgCheck(String(cs.c_str()), now, expect, expect, now + 901, 0) == CFG_STALE);
  CHECK(cfgCheck(String(cs.c_str()), now, expect, expect, 0, 0) == CFG_STALE);
  CHECK(cfgCheck(String(cs.c_str()), now, expect, expect, now, now) == CFG_REPLAY);
  CHECK(cfgCheck(String(""), now, expect, expect, now, 0) == CFG_NONE);

  beginCase("interval: clamped, and the battery has the last word");
  CHECK(effectiveInterval(150, 0) == 150);
  CHECK(effectiveInterval(10, 0) == 60 && effectiveInterval(99999, 0) == 1800);
  CHECK(effectiveInterval(150, 12.6f) == 150);
  CHECK(effectiveInterval(150, 11.8f) == 600 && effectiveInterval(1200, 11.8f) == 1200);
  CHECK(effectiveInterval(150, 11.4f) == 900);

  // ---------------------------------------------------------- download
  std::string img = makeImage(100000, 5);   // 33 chunks, the last one short

  beginCase("ota: a clean download installs, byte for byte");
  resetPi(img);
  CHECK(otaStart(offer(img), "1.6.0", String()));
  CHECK(runToEnd() == 1);
  CHECK(pi.ended && !pi.aborted && pi.written == img && pi.beginSize == img.size());
  CHECK(pi.calls == (int)((img.size() + 3071) / 3072));

  beginCase("ota: spread over several loops when the budget is short");
  resetPi(img);
  otaStart(offer(img), "1.6.0", String());
  { int loops = 0, r = 0;
    // each fetch "takes" 5 s of the 60 s budget: 12 chunks a loop
    pi.tamper = [](long, long, std::string &) { g_millis += 5000; return 0; };
    while ((r = otaStep()) == 0 && otaJob.active && loops < 100) loops++;
    CHECK(r == 1 && loops >= 2 && pi.written == img); }

  beginCase("ota: an altered chunk is caught by the hash, never installed");
  resetPi(img);
  pi.tamper = [](long off, long, std::string &body) { if (off == 3072 * 7) body[10] = (body[10] == 'A' ? 'B' : 'A'); return 0; };
  otaStart(offer(img), "1.6.0", String());
  CHECK(runToEnd() == -1 && pi.aborted && !pi.ended);
  CHECK(std::string(otaReport.c_str()) == "fail:sha:1.6.1");

  beginCase("ota: a short chunk is fetched again; six in a row gives up");
  resetPi(img);
  { int bad = 2;
    pi.tamper = [&bad](long off, long, std::string &body) { if (off == 3072 * 3 && bad > 0) { bad--; body.resize(100); } return 0; };
    otaStart(offer(img), "1.6.0", String());
    CHECK(runToEnd() == 1 && pi.written == img); }
  resetPi(img);
  pi.tamper = [](long off, long, std::string &) { return off == 0 ? 503 : 0; };
  otaStart(offer(img), "1.6.0", String());
  CHECK(runToEnd() == -1 && std::string(otaReport.c_str()) == "fail:fetch:1.6.1" && pi.aborted);

  beginCase("ota: a failed fetch waits for the next loop, then resumes");
  resetPi(img);
  { int down = 1;
    pi.tamper = [&down](long off, long, std::string &) { if (off == 3072 * 10 && down) { down--; return -1; } return 0; };
    otaStart(offer(img), "1.6.0", String());
    CHECK(otaStep() == 0 && otaJob.active && otaJob.off == 3072 * 10);
    CHECK(otaPercent() == (int)(3072L * 10 * 100 / (long)img.size()));
    CHECK(runToEnd() == 1 && pi.written == img); }

  beginCase("ota: only newer, never the one that failed here");
  resetPi(img);
  CHECK(!otaStart(offer(img, "1.6.0"), "1.6.0", String()));
  CHECK(!otaStart(offer(img, "1.5.9"), "1.6.0", String()));
  CHECK(!otaStart(offer(img, "1.6.1"), "1.6.0", String("1.6.1")));
  CHECK(otaStart(offer(img, "1.6.2"), "1.6.0", String("1.6.1")));
  CHECK(!otaStart(offer(img, "1.6.3"), "1.6.0", String()));           // one job at a time
  CHECK(otaPercent() == 0);

  beginCase("ota: write and finish failures abort cleanly");
  resetPi(img); pi.failWrite = true;
  otaStart(offer(img), "1.6.0", String());
  CHECK(runToEnd() == -1 && std::string(otaReport.c_str()) == "fail:write:1.6.1" && pi.aborted);
  resetPi(img); pi.failEnd = true;
  otaStart(offer(img), "1.6.0", String());
  CHECK(runToEnd() == -1 && std::string(otaReport.c_str()) == "fail:end:1.6.1");
  resetPi(img);
  { BoxCfg big = offer(img); big.osz = OTA_SLOT_BYTES + 1;
    otaStart(big, "1.6.0", String());
    CHECK(runToEnd() == -1 && std::string(otaReport.c_str()) == "fail:begin:1.6.1"); }

  if (getenv("LTEST_CANARY")) { beginCase("canary: must fail"); CHECK(1 == 2); }
  printf("\n%d checks passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
