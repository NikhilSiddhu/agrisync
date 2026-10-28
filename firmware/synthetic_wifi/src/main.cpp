/*
 * AgriSync Board A - ESP32 WROOM-32D sensor node   (firmware 1.6.0)
 *
 * Reads the I2C bus, derives leaf wetness from dew point, POSTs JSON home.
 *
 * What 1.6.0 adds, so the box never needs a cable again after this flash:
 *
 *  THE PI SETS THE PACE           Each reply to a reading can carry a signed
 *                       config from the Pi (linkcore.h): the interval, the
 *                       queue batch size, an update on offer. Applied only if
 *                       its HMAC checks, it is fresh and newer than the last.
 *                       Kept in flash, so it survives a power cut. The box
 *                       still slows itself on a low battery, whatever it says.
 *
 *  UPDATES OVER 4G                The image comes down 3 KB at a time between
 *                       readings, is hashed byte for byte against the SHA-256
 *                       the Pi signed, and boots on trial: no reading through
 *                       within three boots and the old image comes back.
 *
 *  THE KEY LIVES IN FLASH         A build with DEVICE_HMAC_SECRET set writes
 *                       the key to NVS; images sent over the air are built
 *                       with it empty, because the download is readable by
 *                       anyone on the network path.
 *
 *  THE QUEUE DRAINS IN BATCHES    Up to twelve readings a request after an
 *                       outage instead of one (queue.h).
 *
 *  THE RAIN INPUT IS DEBOUNCED    1.4.0 counted every flicker of the plate's
 *                       comparator: 53 million "tips". Edges closer than
 *                       200 ms now count once, and rain_wet_pct says how much
 *                       of the interval the plate read wet.
 *
 *  DIAGNOSTICS                    free_heap, min_heap, flash_free_kb,
 *                       interval_s, cfg_v, ota_state in every reading.
 *
 * 1.5.0:
 * What 1.5.0 changed, and why. The field data from August to October 2026
 * showed the queue of unsent readings only ever growing, 3,340 of them by
 * 22 October, including the whole week of 3 to 10 October:
 *
 *  THE QUEUE DRAINS ON CELLULAR   1.4.0 only flushed when WiFi was connected,
 *                       and WiFi is switched off in the field, so nothing
 *                       queued was ever re-sent. It now drains whenever the
 *                       link is up, whichever link that is.
 *
 *  THE BOARD KNOWS THE TIME       1.4.0 set the modem's clock from NTP and
 *                       never copied it to the board, so queued readings
 *                       carried no time and could only be stamped on
 *                       arrival. The board clock is now set from the modem
 *                       after its NTP sync, and every reading says when it
 *                       was taken.
 *
 *  NOTHING IS COPIED TO DRAIN     1.4.0 rewrote the whole buffer to drop the
 *                       lines it had sent, which needs as much free flash as
 *                       the buffer itself. The queue is now a row of small
 *                       segment files and a read cursor; a segment is
 *                       deleted once it is sent.
 *
 *  EVERY READING IS NAMED         boot counts power-ups and up_s is seconds
 *                       since this one. The server uses the pair to store a
 *                       re-sent reading once, and to place one taken before
 *                       the clock was set.
 *
 *  A LEAF WETNESS SENSOR          Optional, on ADS1115 A0, sent as raw
 *                       millivolts (-DLEAF_WET_SENSOR=1). The server turns
 *                       them into wetness once it knows what dry and wet read.
 *
 *  1.4.0's BUFFER IS KEPT ASIDE   Its readings have no time. Sending them now
 *                       would file them at the wrong hour, so they move to
 *                       /legacy.jsonl, are never sent, and come out with D
 *                       for scripts/recover_buffer.py.
 *
 * Earlier: store and forward (1.1.0), NTP over WiFi, emissivity correction,
 * LED fault codes, sensor presence in every payload.
 *
 * LED codes (GPIO2):
 *      solid on   everything healthy
 *      1 blink    WiFi down
 *      2 blinks   a core sensor is missing from the I2C bus
 *      3 blinks   cannot reach the API, readings are being queued
 *      4 blinks   clock not set, readings carry no time
 *
 * Serial commands (115200):
 *      D   print every queued and legacy reading. Deletes nothing.
 *      C   ask to clear the queue; then Y within 10 s to do it
 *      ?   status (with the interval, batch, config and update state)
 *      T   cellular upload size sweep
 *
 * Pin map:
 *   SDA        GPIO21      BME280 + MLX90614 + ADS1115
 *   SCL        GPIO22
 *   Raindrop   AO GPIO35, DO GPIO32
 *   Battery    GPIO39 (47k/10k divider, needs -DUSE_BATTERY_ADC=1)
 *   Leaf wet   ADS1115 A0, powered from LEAF_WET_POWER_PIN if set
 *   Status LED GPIO2
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <time.h>
#include <sys/time.h>
#include <esp_system.h>
#include <esp_timer.h>

/* ── Transport ────────────────────────────────────────────────────────────
   0  SERIAL  tethered to the Pi over USB. scripts/serial_bridge.py forwards.
   1  WIFI    POST over WiFi. Bench only; will not reach the field.
   2  CELL    A7670C over UART2, HTTP to ingest.agrisync.tech through the
              tunnel, every request HMAC-signed.

   Set with -DTRANSPORT=2 in platformio.ini.

   In WIFI and CELL mode a reading that cannot be sent is queued in flash
   and sent oldest first once the link is back. About two weeks of
   five-minute readings fit; past that the oldest day goes first.
   ──────────────────────────────────────────────────────────────────────── */
#define TRANSPORT_SERIAL 0
#define TRANSPORT_WIFI   1
#define TRANSPORT_CELL   2

#ifndef TRANSPORT
#define TRANSPORT TRANSPORT_SERIAL
#endif

/* Kept so older builds still work; TRANSPORT is the flag to use now. */
#define USE_WIFI_POST (TRANSPORT == TRANSPORT_WIFI)
#define USE_CELL      (TRANSPORT == TRANSPORT_CELL)
#define NETWORKED     (USE_WIFI_POST || USE_CELL)

#ifndef LEAF_WET_SENSOR
#define LEAF_WET_SENSOR 0
#endif
#ifndef LEAF_WET_POWER_PIN
#define LEAF_WET_POWER_PIN -1
#endif

#include <Adafruit_BME280.h>
#include <Adafruit_MLX90614.h>
#include <Adafruit_ADS1X15.h>

#ifndef HMAC_SECRET
#define HMAC_SECRET ""
#endif

#if USE_CELL
#include <Update.h>
#include <esp_ota_ops.h>
#include "modem.h"
#include "photoreceiver.h"
static const int PIN_MODEM_RX = 27;   // ESP32 RX2  <- modem TXD
static const int PIN_MODEM_TX = 26;   // ESP32 TX2  -> modem RXD
Modem modem(Serial2, PIN_MODEM_RX, PIN_MODEM_TX, CELL_APN);
bool cellReady = false;
#endif

// ---------- pins ----------
static const int PIN_SDA        = 21;
static const int PIN_SCL        = 22;
static const int PIN_RAIN_AO    = 35;
static const int PIN_RAIN_DO    = 32;
static const int PIN_BATTERY    = 39;
static const int PIN_STATUS_LED = 2;

// ---------- timing ----------
/* One a minute on the bench. Five in the field: it is plenty for wet-hour
   accounting (12 samples an hour), and it quadruples both battery life and
   flash capacity. */
#if USE_CELL
static long cfgIntervalS = 300;      // the Pi's wish from 1.6.0, kept in NVS
#else
static long cfgIntervalS = 60;
#endif
static float lastBattV = 0.0f;       // 0: not measured on this build
static const unsigned long WIFI_TIMEOUT_MS  = 20UL * 1000UL;
static const int  POST_RETRIES        = 3;
static const int  POST_RETRY_DELAY_MS = 2500;
static const unsigned long BOOT_SETTLE_MS = 3000;

// ---------- clock ----------
#if USE_CELL
static const unsigned long CLOCK_COPY_MS  = 3600UL * 1000UL;   // re-copy hourly once set
static const unsigned long CLOCK_RETRY_MS = 600UL * 1000UL;    // retry NTP every 10 min until set
#endif

// ---------- leaf wetness model ----------
// MUST stay in sync with src/ml/wetness.py on the Pi.
static const float MAGNUS_A   = 17.62f;
static const float MAGNUS_B   = 243.12f;
static const float DRY_MARGIN = 2.0f;

// ---------- fault codes ----------
enum Fault {
  FAULT_NONE       = 0,
  FAULT_WIFI       = 1,
  FAULT_SENSOR     = 2,
  FAULT_API        = 3,
  FAULT_CLOCK      = 4
};

Adafruit_BME280 bme;
Adafruit_MLX90614 mlx = Adafruit_MLX90614();
Adafruit_ADS1115 ads;

bool haveBME = false;
bool haveMLX = false;
bool haveADS = false;
bool clockSynced = false;

volatile unsigned long rainTips = 0;
volatile uint32_t rainLastEdgeUs = 0;
static const uint32_t RAIN_DEBOUNCE_US = 200000;   // edges closer than 200 ms are one event
static uint16_t rainWetN = 0, rainSampN = 0;        // plate samples this interval, and how many read wet
static unsigned long rainLastSample = 0;
unsigned long lastPost = 0;
Fault currentFault = FAULT_NONE;

uint32_t bootCount = 0;
bool firstReading = true;

#if NETWORKED
#define QUEUE_BATCH 12                 // 1.6.0: up to twelve queued readings a request
#endif
#include "queue.h"

#if USE_CELL
#include "linkcore.h"
#include "ota.h"
#endif

/* Between queued sends: keep the camera link answered, so a photograph
   arriving mid-drain is not left waiting for a minute. */
const char *deviceSecret();      // the key from NVS (below)

void queueIdle() {
#if USE_CELL
  photoReceiverService(modem, CELL_HOST, deviceSecret(), cellReady);
#endif
}

/* The plate's comparator chatters when the plate is half wet: 1.4.0 counted
   every flicker. Now a burst of edges is one event, once 200 ms pass quiet. */
void IRAM_ATTR onRainTip() {
  uint32_t now = (uint32_t)micros();
  if (now - rainLastEdgeUs > RAIN_DEBOUNCE_US) rainTips = rainTips + 1;
  rainLastEdgeUs = now;
}

/* Every 10 s, between readings: is the plate wet? DO is pulled low when it is. */
static void sampleRainPlate() {
  if (rainLastSample != 0 && millis() - rainLastSample < 10000UL) return;
  rainLastSample = millis();
  if (rainSampN < 60000) {
    rainSampN++;
    if (digitalRead(PIN_RAIN_DO) == LOW) rainWetN++;
  }
}

static unsigned long currentIntervalMs() {
#if USE_CELL
  return (unsigned long)effectiveInterval(cfgIntervalS, lastBattV) * 1000UL;
#else
  return (unsigned long)cfgIntervalS * 1000UL;
#endif
}

// ============================ the device key ============================
/* From 1.6.0 the key lives in NVS. A build made with DEVICE_HMAC_SECRET set
   (the cable flash) writes it there, replacing any older one, which is also
   how the key is rotated. A build made with it empty (every image sent over
   the air) uses what is stored. */
static String g_secret;
static void secretBegin() {
  Preferences p;
  if (!p.begin("agrisync", false)) return;
  String stored = p.getString("hmac", "");
  const char *built = HMAC_SECRET;
  if (strlen(built) >= 8 && stored != built) {
    p.putString("hmac", built);
    stored = built;
    Serial.println("[key] device key from this build stored in flash");
  }
  p.end();
  g_secret = stored;
  if (!g_secret.length()) Serial.println("[key] NO DEVICE KEY: flash once by cable with DEVICE_HMAC_SECRET set");
}
const char *deviceSecret() { return g_secret.c_str(); }

// ============================ the link to the Pi (1.6.0) ============================
#if USE_CELL
static long   lastCfgTs = 0;          // newest config applied, so an old one is never replayed
static int    cfgV = 0;
static BoxCfg pendingOffer;
static bool   offerPending = false;
static String otaBad;                 // a version that failed here: not tried again
static String otaNote;                // for the next reading: ok:<v>, rolled_back:<v>, fail:<why>:<v>
static bool   otaTrialPending = false;
static const uint32_t OTA_TRIAL_BOOTS = 3;

static void linkLoad() {
  Preferences p;
  if (!p.begin("agrisync", true)) return;
  long ivl = (long)p.getULong("ivl", 0);
  if (ivl >= IVL_MIN_S && ivl <= IVL_MAX_S) cfgIntervalS = ivl;
  uint8_t b = p.getUChar("batch", 0);
  if (b >= 1 && b <= BATCH_MAX) qBatch = b;
  lastCfgTs = (long)p.getULong("cfg_ts", 0);
  p.end();
}

static void noteSave(const String &note) {
  otaNote = note;
  Preferences p;
  if (!p.begin("agrisync", false)) return;
  if (note.length()) p.putString("ota_note", note); else p.remove("ota_note");
  p.end();
}

/* The Pi's signed settings, from the reply to a reading or a batch. */
static void applyReply(const String &reply) {
  String cfg, sig;
  long ts = 0;
  if (!reply.length() || !replyConfig(reply, cfg, ts, sig)) return;
  String expect = Modem::sign(deviceSecret(), String(ts) + "." + cfg);
  CfgVerdict v = cfgCheck(cfg, ts, sig, expect, clockSynced ? (long)time(nullptr) : 0, lastCfgTs);
  if (v != CFG_OK) {
    if (v != CFG_REPLAY) Serial.printf("[cfg] not applied (%s)\n",
                                       v == CFG_BADSIG ? "bad signature" : v == CFG_STALE ? "stale" : "none");
    return;
  }
  BoxCfg c;
  if (!cfgParse(cfg, c)) return;
  lastCfgTs = ts;
  cfgV = c.v;
  Preferences p;
  if (p.begin("agrisync", false)) {
    p.putULong("cfg_ts", (uint32_t)ts);
    if (c.interval_s && c.interval_s != cfgIntervalS) {
      cfgIntervalS = c.interval_s;
      p.putULong("ivl", (uint32_t)c.interval_s);
      Serial.printf("[cfg] interval now %ld s\n", c.interval_s);
    }
    if (c.batch && c.batch != qBatch) {
      qBatch = c.batch;
      p.putUChar("batch", (uint8_t)c.batch);
      Serial.printf("[cfg] queue batch now %d\n", c.batch);
    }
    p.end();
  }
  if (c.ota.length()) { pendingOffer = c; offerPending = true; }
}

/* A reading got through. On a trial boot that settles it: this image stays. */
static void linkOk() {
  if (!otaTrialPending) return;
  otaTrialPending = false;
  Preferences p;
  if (p.begin("agrisync", false)) { p.remove("ota_trial"); p.remove("ota_tries"); p.end(); }
  esp_ota_mark_app_valid_cancel_rollback();
  noteSave(String("ok:") + FIRMWARE_VERSION);
  Serial.println("[ota] trial passed: this image stays");
}

/* First thing at boot. A new image is on trial until a reading gets through;
   three boots without one, and the box goes back to the image before it. */
static void otaBootCheck() {
  Preferences p;
  if (!p.begin("agrisync", false)) return;
  otaBad = p.getString("ota_bad", "");
  otaNote = p.getString("ota_note", "");
  String trial = p.getString("ota_trial", "");
  if (trial.length()) {
    if (trial == FIRMWARE_VERSION) {
      uint32_t tries = p.getUInt("ota_tries", 0) + 1;
      p.putUInt("ota_tries", tries);
      if (tries > OTA_TRIAL_BOOTS) {
        String note = String("rolled_back:") + trial;
        p.putString("ota_bad", trial);
        p.putString("ota_note", note);
        p.remove("ota_trial");
        p.remove("ota_tries");
        p.end();
        const esp_partition_t *back = esp_ota_get_next_update_partition(NULL);
        Serial.printf("[ota] %s got no reading through in %u boots: going back\n", trial.c_str(), (unsigned)OTA_TRIAL_BOOTS);
        if (back && esp_ota_set_boot_partition(back) == ESP_OK) { delay(300); ESP.restart(); }
        return;
      }
      otaTrialPending = true;
      Serial.printf("[ota] %s on trial, boot %u of %u\n", trial.c_str(), (unsigned)tries, (unsigned)OTA_TRIAL_BOOTS);
    } else {
      // Something other than the trial image is running: it was rolled back.
      String note = String("rolled_back:") + trial;
      p.putString("ota_bad", trial);
      p.putString("ota_note", note);
      p.remove("ota_trial");
      p.remove("ota_tries");
      otaBad = trial;
      otaNote = note;
    }
  }
  p.end();
}

// ota.h's hooks: the signed GET and the Update library.
int otaFetch(const String &path, String &out) {
  if (!cellReady) return -1;
  return modem.getSigned(CELL_HOST, path, deviceSecret(), out, 4200);
}
bool otaInstallBegin(size_t size) { return Update.begin(size); }
bool otaInstallWrite(const uint8_t *p, size_t n) { return Update.write((uint8_t *)p, n) == n; }
bool otaInstallEnd() { return Update.end(); }
void otaInstallAbort() { Update.abort(); }

/* Between readings: start an offered update, or fetch the next slice of one. */
static void otaService() {
  if (offerPending && !otaJob.active) {
    offerPending = false;
    bool battOk = lastBattV < 1.0f || lastBattV >= 12.0f;
    if (clockSynced && battOk && qCount < 100) otaStart(pendingOffer, FIRMWARE_VERSION, otaBad);
  }
  if (!otaJob.active || !cellReady) return;
  int r = otaStep();
  if (r == 1) {
    Preferences p;
    if (p.begin("agrisync", false)) {
      p.putString("ota_trial", otaJob.ver);
      p.putUInt("ota_tries", 0);
      p.end();
    }
    cursorSave();
    delay(500);
    ESP.restart();
  } else if (r == -1) {
    noteSave(otaReport);
    // A bad image or a refusal to install will not get better by trying
    // again; a failed download might, on the next offer.
    if (otaReport.indexOf("fail:fetch") != 0 && otaReport.indexOf("fail:chunk") != 0) {
      otaBad = otaJob.ver;
      Preferences p;
      if (p.begin("agrisync", false)) { p.putString("ota_bad", otaBad); p.end(); }
    }
  }
}
#endif

// ============================ helpers ============================

float dewPointC(float tempC, float humidityPct) {
  float rh = humidityPct;
  if (rh < 0.1f) rh = 0.1f;
  if (rh > 100.0f) rh = 100.0f;
  float gamma = logf(rh / 100.0f) + (MAGNUS_A * tempC) / (MAGNUS_B + tempC);
  return (MAGNUS_B * gamma) / (MAGNUS_A - gamma);
}

float leafWetnessIndex(float leafTempC, float dewPt) {
  float delta = leafTempC - dewPt;
  if (delta <= 0.0f) return 100.0f;
  if (delta >= DRY_MARGIN) return 0.0f;
  return 100.0f * (1.0f - delta / DRY_MARGIN);
}

/*
 * Correct the MLX90614 reading for leaf emissivity.
 *
 * The sensor reports a temperature computed as if the target were a perfect
 * blackbody. A real leaf emits less and reflects some of its surroundings:
 *
 *      T_meas^4 = e * T_leaf^4 + (1 - e) * T_reflected^4
 *
 * so, rearranged:
 *
 *      T_leaf = ((T_meas^4 - (1 - e) * T_refl^4) / e) ^ (1/4)
 *
 * We use the sensor's own ambient reading for T_reflected. That is an
 * approximation - a downward-looking sensor under an open sky is partly
 * reflecting cold sky, not the sensor body - so this reduces the error
 * rather than eliminating it. Honest and better than ignoring it.
 *
 * Both values are sent, so the server can re-derive either later.
 */
float correctForEmissivity(float measuredC, float ambientC) {
  const double e = (double)LEAF_EMISSIVITY;
  if (e >= 0.999 || e <= 0.5) return measuredC;   // nothing to do, or nonsense

  const double K = 273.15;
  double tMeas = measuredC + K;
  double tRefl = ambientC + K;

  double numerator = pow(tMeas, 4.0) - (1.0 - e) * pow(tRefl, 4.0);
  if (numerator <= 0) return measuredC;           // would take a root of a negative

  return (float)(pow(numerator / e, 0.25) - K);
}

/* Seconds since this power-up, from the 64-bit timer: no 49-day wrap. */
static uint32_t upSeconds() { return (uint32_t)(esp_timer_get_time() / 1000000ULL); }

static const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_BROWNOUT:  return "brownout";
    case ESP_RST_PANIC:     return "panic";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:       return "watchdog";
    case ESP_RST_SW:        return "software";
    case ESP_RST_DEEPSLEEP: return "deep-sleep";
    case ESP_RST_EXT:       return "external";
    default:                return "other";
  }
}

/* Count power-ups in NVS, so every reading can say which one it came from. */
static void countBoot() {
  Preferences p;
  if (p.begin("agrisync", false)) {
    bootCount = p.getUInt("boot", 0) + 1;
    p.putUInt("boot", bootCount);
    p.end();
  }
}

// ============================ status LED ============================

void serviceStatusLed() {
  static unsigned long lastChange = 0;
  static int step = 0;

  if (currentFault == FAULT_NONE) {
    digitalWrite(PIN_STATUS_LED, HIGH);
    step = 0;
    return;
  }

  // N short blinks, then a long gap, repeating.
  const int totalSteps = currentFault * 2 + 2;
  const unsigned long onMs = 150, offMs = 200, gapMs = 1200;

  unsigned long wait;
  if (step >= currentFault * 2) wait = gapMs;
  else wait = (step % 2 == 0) ? onMs : offMs;

  if (millis() - lastChange >= wait) {
    lastChange = millis();
    step = (step + 1) % totalSteps;
    digitalWrite(PIN_STATUS_LED,
                 (step < currentFault * 2 && step % 2 == 0) ? HIGH : LOW);
  }
}

void updateFault() {
#if USE_CELL
  if (!cellReady)                                 currentFault = FAULT_API;
  else if (!haveBME || !haveMLX || !haveADS)      currentFault = FAULT_SENSOR;
  else if (!clockSynced)                          currentFault = FAULT_CLOCK;
  else                                            currentFault = FAULT_NONE;
#elif USE_WIFI_POST
  if (WiFi.status() != WL_CONNECTED)              currentFault = FAULT_WIFI;
  else if (!haveBME || !haveMLX || !haveADS)      currentFault = FAULT_SENSOR;
  else if (!clockSynced)                          currentFault = FAULT_CLOCK;
  else                                            currentFault = FAULT_NONE;
#else
  currentFault = (!haveBME || !haveMLX || !haveADS) ? FAULT_SENSOR : FAULT_NONE;
#endif
}

/* Is there a way home right now? The queue drains only when there is. */
bool linkUp() {
#if USE_CELL
  return cellReady;
#elif USE_WIFI_POST
  return WiFi.status() == WL_CONNECTED;
#else
  return false;
#endif
}

// ============================ wifi + time ============================

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("[wifi] connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) {
    serviceStatusLed();
    delay(50);
  }

  if (WiFi.status() == WL_CONNECTED) {
    // Modem sleep parks the radio between transmissions to save power, but it
    // costs 20-25% packet loss and latency spikes to 400ms+. That showed up as
    // failed POSTs and NTP timeouts that looked like firewall problems.
    WiFi.setSleep(false);
    Serial.printf("[wifi] connected, ip %s rssi %d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("[wifi] FAILED - readings will be queued in flash");
  }
}

void syncClock() {
  if (WiFi.status() != WL_CONNECTED) return;

  // The Pi first. It is on the same LAN, needs no DNS, and works with the
  // internet down. Public servers are the fallback.
  configTime(0, 0, PI_HOST, "pool.ntp.org", "time.google.com");
  Serial.printf("[time] syncing via %s", PI_HOST);

  for (int i = 0; i < 40; i++) {
    time_t now = time(nullptr);
    if (now > 1700000000) {  // sometime after Nov 2023, so clearly real
      clockSynced = true;
      Serial.printf(" ok, epoch %lu\n", (unsigned long)now);
      return;
    }
    Serial.print(".");
    delay(500);
  }
  Serial.println(" FAILED - readings will carry no time until it works");
  Serial.println("  -> is chrony running on the Pi and allowing your subnet?");
}

#if USE_CELL
/*
 * Copy the modem's clock to the board.
 *
 * The modem keeps UTC from its own NTP sync (modem.syncTime), which is what
 * signs every upload. 1.4.0 never passed that time on, so the board stamped
 * nothing and a queued reading arrived days later with no idea when it was
 * taken. Only a clock the modem actually set from NTP this power-up is
 * trusted: a modem clock can be plausible and still hours out.
 */
void adoptModemClock() {
  static unsigned long lastTry = 0;
  if (!cellReady) return;
  unsigned long every = clockSynced ? CLOCK_COPY_MS : CLOCK_RETRY_MS;
  if (lastTry != 0 && millis() - lastTry < every) return;
  lastTry = millis();

  long t = modem.syncTime();          // NTP once per power-up, then a clock read
  if (t > 1700000000L && modem.timeSynced()) {
    struct timeval tv;
    tv.tv_sec = (time_t)t;
    tv.tv_usec = 0;
    settimeofday(&tv, nullptr);
    if (!clockSynced) Serial.printf("[time] board clock set from the modem, epoch %ld\n", t);
    clockSynced = true;
  } else if (!clockSynced) {
    Serial.println("[time] modem has no NTP time yet - readings carry boot and uptime only");
  }
}
#endif

// ============================ i2c ============================

void scanI2C() {
  Serial.println("[i2c] scanning bus...");
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[i2c] device at 0x%02X\n", addr);
      found++;
    }
  }
  if (found == 0) {
    Serial.println("[i2c] NOTHING FOUND - check SDA/SCL wiring and 3V3 power");
  }
}

void setupSensors() {
  Wire.begin(PIN_SDA, PIN_SCL);
  Wire.setClock(100000);   // MLX90614 is SMBus and often refuses 400kHz
  delay(300);              // it needs ~250ms after power before it answers
  scanI2C();

  if (bme.begin(0x76, &Wire)) {
    haveBME = true; Serial.println("BME280 OK @ 0x76");
  } else if (bme.begin(0x77, &Wire)) {
    haveBME = true; Serial.println("BME280 OK @ 0x77");
  } else {
    Serial.println("BME280 NOT FOUND - SDA/SCL swapped, or no power");
  }

  if (mlx.begin(0x5A, &Wire)) {
    haveMLX = true;
    Serial.printf("MLX90614 OK @ 0x5A (correcting for emissivity %.2f)\n",
                  (double)LEAF_EMISSIVITY);
  } else {
    Serial.println("MLX90614 not found - leaf temperature will be absent");
  }

  if (ads.begin(0x48, &Wire)) {
    haveADS = true;
    ads.setGain(GAIN_ONE);              // +-4.096 V, 0.125 mV a count
    Serial.println("ADS1115 OK @ 0x48");
  } else {
    Serial.println("ADS1115 not found - analog front end unavailable");
  }

#if LEAF_WET_SENSOR && LEAF_WET_POWER_PIN >= 0
  pinMode(LEAF_WET_POWER_PIN, OUTPUT);
  digitalWrite(LEAF_WET_POWER_PIN, LOW);
#endif
}

#if LEAF_WET_SENSOR
/*
 * The leaf wetness sensor on ADS1115 A0, as raw millivolts.
 *
 * Raw on purpose: what dry and wet read depends on the sensor, the cable and
 * the supply, so the conversion lives on the server (LW_DRY_MV, LW_WET_MV)
 * where it can be set and changed without touching the box.
 *
 * With LEAF_WET_POWER_PIN set the sensor is powered for about 60 ms per
 * reading. A grid that sits under constant voltage plates and corrodes,
 * which is the likely end of the rain plate: it has read wet since 27 Sep.
 */
float readLeafWetMv() {
  if (!haveADS) return NAN;
#if LEAF_WET_POWER_PIN >= 0
  digitalWrite(LEAF_WET_POWER_PIN, HIGH);
  delay(50);                                  // let the sensor settle
#endif
  long sum = 0;
  for (int i = 0; i < 4; i++) { sum += ads.readADC_SingleEnded(0); delay(2); }
#if LEAF_WET_POWER_PIN >= 0
  digitalWrite(LEAF_WET_POWER_PIN, LOW);
#endif
  return ads.computeVolts((int16_t)(sum / 4)) * 1000.0f;
}
#endif

// ============================ posting ============================

#if USE_CELL
int sendBody(const String &body, bool quiet) {
  if (!cellReady) return -1;

  String reply;
  int code = modem.postJson(CELL_HOST, "/api/v1/sensors/readings", body,
                            deviceSecret(), &reply);

  if (!quiet) Serial.printf("[reading] HTTP %d\n", code);
  if (code >= 200 && code < 300) { linkOk(); applyReply(reply); }

  if (code == 401) {
    Serial.println("  -> signature rejected. Either the device key does not match");
    Serial.println("     .env on the Pi, or the clock is more than 15 min out.");
  } else if (code == -2) {
    Serial.println("  -> no clock. NTP sync failed, cannot sign.");
  }
  return code;
}

/* Up to twelve queued readings in one request (queue.h). */
int sendBatch(const String &body) {
  if (!cellReady) return -1;
  String reply;
  int code = modem.postJson(CELL_HOST, "/api/v1/sensors/readings/batch", body,
                            deviceSecret(), &reply);
  Serial.printf("[batch] %u bytes, HTTP %d\n", (unsigned)body.length(), code);
  if (code >= 200 && code < 300) { linkOk(); applyReply(reply); }
  return code;
}

#else
int sendBody(const String &body, bool quiet) {
  if (WiFi.status() != WL_CONNECTED) return -1;

  HTTPClient http;
  String url = String("http://") + PI_HOST + ":5000/api/v1/sensors/readings";
  http.begin(url);
  http.setTimeout(8000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_API_KEY);

  int code = http.POST(body);
  if (!quiet) Serial.printf("[reading] HTTP %d\n", code);

  if (code == 401) {
    Serial.println("  -> 401: DEVICE_API_KEY here does not match .env on the Pi");
  } else if (code <= 0 && !quiet) {
    Serial.printf("  -> transport error: %s\n", http.errorToString(code).c_str());
    Serial.println("  -> is PI_HOST right? is Flask bound to 0.0.0.0 not 127.0.0.1?");
    Serial.println("  -> is the API running?  systemctl status agrisync-api");
  }

  http.end();
  return code;
}

int sendBatch(const String &body) {
  if (WiFi.status() != WL_CONNECTED) return -1;
  HTTPClient http;
  String url = String("http://") + PI_HOST + ":5000/api/v1/sensors/readings/batch";
  http.begin(url);
  http.setTimeout(15000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", DEVICE_API_KEY);
  int code = http.POST(body);
  Serial.printf("[batch] HTTP %d\n", code);
  http.end();
  return code;
}
#endif

// ============================ serial commands ============================

/* Retrieval when there is no network.
 *
 * D prints every queued reading as an ordinary [payload] line, which
 * scripts/serial_bridge.py can ingest because 1.5.0 readings carry their
 * own time. 1.4.0 readings come out as [legacy] lines, which the bridge
 * ignores; scripts/recover_buffer.py puts those back in time.
 *
 * Nothing is deleted by D. Clearing takes C and then Y within ten seconds:
 * a single keystroke that wipes a fortnight of field data is not a feature.
 */
/* Upload size sweep, triggered by 'T'.

   The same bytes at increasing sizes, each through postBinary. The point is
   to find the largest body this modem transmits without altering it.

       400 -> arrived intact, refused only for not being a JPEG
       401 -> arrived altered, signature no longer matches
*/
static void sigSweep() {
#if USE_CELL
  const size_t sizes[] = { 512, 2048, 4096, 8192, 12288, 16384, 24576 };
  Serial.println("[sweep] finding the largest body that survives");

  for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
    size_t n = sizes[i];
    uint8_t *buf = (uint8_t *)malloc(n);
    if (!buf) { Serial.printf("[sweep] %6u  no memory\n", (unsigned)n); continue; }
    for (size_t k = 0; k < n; k++) buf[k] = (uint8_t)(k & 0xFF);

    int code = modem.postBinary(CELL_HOST, "/api/v1/photos?name=sweep.jpg",
                                buf, n, deviceSecret());
    free(buf);

    Serial.printf("[sweep] %6u bytes -> HTTP %d  %s\n",
                  (unsigned)n, code,
                  code == 400 ? "INTACT"
                : code == 401 ? "ALTERED"
                              : "other");
    delay(1500);
  }
  Serial.println("[sweep] done - largest INTACT is your usable payload");
#else
  Serial.println("[sweep] cellular build only");
#endif
}

void serviceSerialCommands() {
  static unsigned long clearAsked = 0;
  if (!Serial.available()) return;
  int c = Serial.read();
  if (c == 'T' || c == 't') sigSweep();
  else if (c == 'D' || c == 'd') dumpBuffer();
  else if (c == 'C' || c == 'c') {
    clearAsked = millis();
    Serial.printf("[clear] this deletes %ld queued and %ld legacy readings. Send Y within 10 s to confirm.\n",
                  (long)qCount, (long)legacyCount);
  } else if ((c == 'Y' || c == 'y') && clearAsked && millis() - clearAsked < 10000UL) {
    clearAsked = 0;
    queueClear();
  } else if (c == '?') {
    Serial.printf("[status] fw %s  boot %lu  up %lus  clock %s  queued %ld  legacy %ld  flash free %u KB\n",
                  FIRMWARE_VERSION, (unsigned long)bootCount, (unsigned long)upSeconds(),
                  clockSynced ? "set" : "NOT SET", (long)qCount, (long)legacyCount,
                  (unsigned)(freeBytes() / 1024));
    Serial.printf("[status] interval %lus  key %s  heap %u (low %u)\n", currentIntervalMs() / 1000UL,
                  g_secret.length() ? "stored" : "MISSING", (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap());
#if USE_CELL
    Serial.printf("[status] batch %d  config v%d  update %s%s%s\n", qBatch, cfgV,
                  otaJob.active ? "downloading " : (otaTrialPending ? "on trial" : "none"),
                  otaJob.active ? otaJob.ver.c_str() : "", otaNote.length() ? (String("  last: ") + otaNote).c_str() : "");
#endif
  }
}

// ============================ main ============================

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== AgriSync Board A  fw " FIRMWARE_VERSION " ===");
  Serial.printf("device: %s -> pi: %s\n", DEVICE_ID, PI_HOST);
  Serial.println("commands: D dump, C then Y clear, ? status");

#if USE_CELL
  otaBootCheck();             // first: a new image on trial may need to step back
#endif
  countBoot();
  Serial.printf("[boot] %lu, reset: %s\n", (unsigned long)bootCount, resetReason());
  secretBegin();

  pinMode(PIN_STATUS_LED, OUTPUT);
  pinMode(PIN_RAIN_DO, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_RAIN_DO), onRainTip, FALLING);

  if (!LittleFS.begin(true)) {
    Serial.println("[fs] LittleFS mount failed - queueing disabled");
  } else {
    queueBegin();
  }
#if USE_CELL
  linkLoad();                 // the Pi's last interval and batch size
#endif

  setupSensors();

#if USE_WIFI_POST
  connectWiFi();
  syncClock();
  updateFault();
  // Let the IP stack settle before loop() fires its first POST.
  delay(BOOT_SETTLE_MS);
#elif USE_CELL
  WiFi.mode(WIFI_OFF);
  btStop();
  Serial.println("[mode] CELLULAR");
  Serial.printf("[cell] %s -> http://%s\n", CELL_APN, CELL_HOST);
  modem.begin();
  photoReceiverBegin();

  if (!modem.sync()) {
    Serial.println("[cell] no reply on UART2.");
    Serial.println("  -> TX/RX crossed? GPIO26 to modem RXD, GPIO27 to modem TXD");
    Serial.println("  -> modem powered from its OWN supply, not the ESP32 3V3?");
  } else if (!modem.simReady()) {
    Serial.println("[cell] SIM not ready - seated? PIN locked?");
  } else {
    int q = modem.signal();
    Serial.printf("[cell] signal %d/31%s\n", q, q == 99 ? "  (none - check the antenna)" : "");
    for (int i = 0; i < 20 && !modem.registered(); i++) { delay(2000); Serial.print("."); }
    Serial.println();
    if (!modem.attached()) Serial.println("[cell] not attached to packet data");
    cellReady = modem.dataUp();
    adoptModemClock();
    Serial.printf("[cell] data %s  ip %s\n", cellReady ? "up" : "FAILED",
                  modem.localIP().c_str());
  }
  updateFault();
  delay(BOOT_SETTLE_MS);
#else
  Serial.println("[mode] SERIAL ONLY - radio off, Pi stamps the timestamps");
  WiFi.mode(WIFI_OFF);
  btStop();
  currentFault = (haveBME && haveADS) ? FAULT_NONE : FAULT_SENSOR;
#endif
}

void loop() {
#if USE_CELL
  photoReceiverService(modem, CELL_HOST, deviceSecret(), cellReady);
#endif
  serviceStatusLed();
  sampleRainPlate();
  serviceSerialCommands();   // D dump, C then Y clear, ? status

  if (lastPost != 0 && millis() - lastPost < currentIntervalMs()) {
    delay(20);
    return;
  }
  lastPost = millis();

#if USE_WIFI_POST
  connectWiFi();
  if (!clockSynced) syncClock();
#elif USE_CELL
  if (!cellReady) {
    Serial.println("[cell] retrying data context");
    cellReady = modem.dataUp();
  }
  adoptModemClock();
#endif

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["firmware"]  = FIRMWARE_VERSION;
  doc["boot"]      = bootCount;
  doc["up_s"]      = upSeconds();
  if (firstReading) { doc["reset"] = resetReason(); firstReading = false; }

  if (clockSynced) doc["sampled_at"] = (unsigned long)time(nullptr);

  // Tell the Pi which sensors answered, so it can alert on one dropping out.
  JsonObject sensors = doc["sensors"].to<JsonObject>();
  sensors["bme280"]   = haveBME;
  sensors["mlx90614"] = haveMLX;
  sensors["ads1115"]  = haveADS;

  float airT = NAN, rh = NAN;

  if (haveBME) {
    airT = bme.readTemperature();
    rh   = bme.readHumidity();
    doc["air_temp_c"]   = airT;
    doc["humidity_pct"] = rh;
    doc["pressure_hpa"] = bme.readPressure() / 100.0f;
  }

  if (haveMLX) {
    float rawLeaf = mlx.readObjectTempC();
    float ambient = mlx.readAmbientTempC();

    // Send the RAW value and the sensor ambient. The Pi applies the
    // emissivity correction, because doing it properly needs a sky
    // temperature estimate the board cannot make.
    doc["leaf_temp_raw_c"] = rawLeaf;
    doc["mlx_ambient_c"]   = ambient;

    // Local approximation, used only for this board's own logging.
    float leafT = correctForEmissivity(rawLeaf, ambient);
    doc["leaf_temp_c"] = leafT;

    if (!isnan(airT) && !isnan(rh)) {
      float dp = dewPointC(airT, rh);
      doc["dew_point_c"]  = dp;
      doc["leaf_wetness"] = leafWetnessIndex(leafT, dp);
    }
  }

#if LEAF_WET_SENSOR
  float lw = readLeafWetMv();
  if (!isnan(lw)) doc["leaf_wet_mv"] = lw;
#endif

  doc["rain_analog"] = analogRead(PIN_RAIN_AO);
  doc["rain_tips"]   = rainTips;
  if (rainSampN) {
    doc["rain_wet_pct"] = 100.0f * rainWetN / rainSampN;
    rainWetN = rainSampN = 0;
  }

#ifdef USE_BATTERY_ADC
  int raw = analogRead(PIN_BATTERY);
  lastBattV = (raw / 4095.0f) * 3.3f * ((47.0f + 10.0f) / 10.0f);
  doc["battery_v"] = lastBattV;
#endif

  // 1.6.0 diagnostics: what the box is running at, and how its memory holds up.
  doc["free_heap"]  = ESP.getFreeHeap();
  doc["min_heap"]   = ESP.getMinFreeHeap();
  doc["interval_s"] = currentIntervalMs() / 1000UL;
#if NETWORKED
  doc["flash_free_kb"] = (unsigned)(freeBytes() / 1024);
#endif
#if USE_CELL
  if (cfgV) doc["cfg_v"] = cfgV;
  String sentNote = otaNote;            // said once, in a reading that got through, is enough
  if (sentNote.length()) doc["ota_state"] = sentNote;
  int otaPct = otaPercent();
  if (otaPct >= 0) doc["ota_pct"] = otaPct;
#endif

#if USE_WIFI_POST
  doc["rssi"] = WiFi.RSSI();
#elif USE_CELL
  int csq = modem.signal();
  if (csq < 99) doc["rssi"] = -113 + 2 * csq;  // CSQ -> dBm, matches WiFi
#endif
#if NETWORKED
  doc["buffered_count"] = qCount;
  if (legacyCount > 0) doc["legacy"] = legacyCount;
#endif

  String body;
  serializeJson(doc, body);
  Serial.printf("[payload] %s\n", body.c_str());

#if TRANSPORT == TRANSPORT_SERIAL
  // Tethered. The line above is the delivery mechanism - the bridge on the Pi
  // reads it. Nothing to retry, nothing to queue.
  updateFault();
  return;
#endif

  // Retry before giving up. The first POST after boot often fails while the
  // radio wakes; queueing it works, but retrying saves a needless re-send.
  int code = sendBody(body, false);
  bool ok = code >= 200 && code < 300;
  for (int attempt = 1; !ok && !permanentRefusal(code) && attempt <= POST_RETRIES; attempt++) {
    Serial.printf("[post] retry %d of %d in %d ms\n",
                  attempt, POST_RETRIES, POST_RETRY_DELAY_MS);
    delay(POST_RETRY_DELAY_MS);
    code = sendBody(body, false);
    ok = code >= 200 && code < 300;
  }

  if (ok) {
#if USE_CELL
    if (sentNote.length() && otaNote == sentNote) noteSave(String());
#endif
    queueFlush();   // link is up, drain anything queued
    updateFault();
  } else if (permanentRefusal(code)) {
    Serial.printf("[post] server refused this reading (HTTP %d) - not queued\n", code);
    updateFault();
  } else {
    queuePush(body);
    currentFault = FAULT_API;
  }

#if USE_CELL
  otaService();     // an update on offer comes down between readings
#endif
}
