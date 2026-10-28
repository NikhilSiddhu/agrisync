#pragma once
/*
 * Firmware updates over 4G (1.6.0). The Pi offers an image in its signed
 * config (ota, osz, osha; see linkcore.h). The box fetches it between
 * readings, 3 KB at a time as base64 text, hashes every byte, and installs
 * it only if the SHA-256 matches what the Pi signed. Then it reboots into
 * the new image on trial (main.cpp): if that image cannot get a reading
 * through within three boots, the box goes back to the old one.
 *
 * The download is spread over many loops, OTA_BUDGET_MS at a time, so
 * readings keep going out every interval while an update comes in. A reboot
 * mid-download starts it again from the top: nothing half-written is ever
 * booted, because the old image stays the boot image until the new one has
 * been checked whole.
 *
 * Kept apart from main.cpp so it can be tested on a computer
 * (test/link_host_test.cpp). It needs from the sketch:
 */
int  otaFetch(const String &path, String &out);        // signed GET; HTTP status
bool otaInstallBegin(size_t size);
bool otaInstallWrite(const uint8_t *p, size_t n);
bool otaInstallEnd();                                  // finished, checked, set to boot next
void otaInstallAbort();

static const long OTA_CHUNK = 3072;                    // 4096 characters of base64
static const unsigned long OTA_BUDGET_MS = 60UL * 1000UL;
static const int OTA_MAX_STRIKES = 6;                  // failed fetches in a row before giving up this boot

struct OtaJob {
  bool   active = false;
  bool   begun = false;
  String ver;
  long   size = 0;
  String sha;
  long   off = 0;
  int    strikes = 0;
  Sha256 hash;
};
static OtaJob otaJob;
static String otaReport;          // for the next reading: "fail:sha:1.6.1" etc.

/* Start a job for this offer, unless one is running or the version is not
   newer than this firmware, or it is the one that already failed here. */
static bool otaStart(const BoxCfg &c, const char *running, const String &bad) {
  if (otaJob.active || !c.ota.length()) return false;
  if (versionCmp(c.ota.c_str(), running) <= 0) return false;
  if (bad.length() && versionCmp(c.ota.c_str(), bad.c_str()) == 0) return false;
  otaJob = OtaJob();
  otaJob.active = true;
  otaJob.ver = c.ota;
  otaJob.size = c.osz;
  otaJob.sha = c.osha;
  Serial.printf("[ota] %s offered, %ld bytes: downloading between readings\n", c.ota.c_str(), c.osz);
  return true;
}

static int otaFail(const char *why) {
  if (otaJob.begun) otaInstallAbort();
  char buf[48];
  snprintf(buf, sizeof(buf), "fail:%s:%s", why, otaJob.ver.c_str());
  otaReport = String(buf);
  Serial.printf("[ota] %s\n", buf);
  otaJob.active = false;
  otaJob.begun = false;
  return -1;
}

/* Percent fetched, for the readings while it runs. -1 when idle. */
static int otaPercent() {
  if (!otaJob.active || otaJob.size <= 0) return -1;
  return (int)(otaJob.off * 100 / otaJob.size);
}

/* One slice of work. 0: idle or still going; 1: installed, reboot now;
   -1: given up (otaReport says why). */
static int otaStep(unsigned long budgetMs = OTA_BUDGET_MS) {
  if (!otaJob.active) return 0;
  if (!otaJob.begun) {
    if (!otaInstallBegin((size_t)otaJob.size)) return otaFail("begin");
    otaJob.begun = true;
  }
  unsigned long start = millis();
  static uint8_t bin[OTA_CHUNK];
  while (otaJob.off < otaJob.size && millis() - start < budgetMs) {
    long len = otaJob.size - otaJob.off < OTA_CHUNK ? otaJob.size - otaJob.off : OTA_CHUNK;
    String b64;
    int code = otaFetch(chunkPath(otaJob.ver, otaJob.off, len), b64);
    long got = code == 200 ? b64decode(b64.c_str(), b64.length(), bin, sizeof(bin)) : -1;
    if (got != len) {
      if (++otaJob.strikes >= OTA_MAX_STRIKES) return otaFail(code == 200 ? "chunk" : "fetch");
      return 0;                               // try again next loop
    }
    otaJob.strikes = 0;
    otaJob.hash.update(bin, (size_t)len);
    if (!otaInstallWrite(bin, (size_t)len)) return otaFail("write");
    otaJob.off += len;
  }
  if (otaJob.off < otaJob.size) return 0;
  char hex[65];
  otaJob.hash.hex(hex);
  if (!ctEqual(String(hex), otaJob.sha)) return otaFail("sha");
  if (!otaInstallEnd()) return otaFail("end");
  otaJob.active = false;
  Serial.printf("[ota] %s checked and installed: rebooting into it on trial\n", otaJob.ver.c_str());
  return 1;
}
