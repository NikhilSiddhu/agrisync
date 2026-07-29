/*
 * AgriSync Board A - ESP32 WROOM-32D sensor node   (firmware 1.1.0)
 *
 * Reads the I2C bus, derives leaf wetness from dew point, POSTs JSON to the Pi.
 *
 * What 1.1.0 added, and why:
 *
 *  STORE AND FORWARD    A failed POST used to vanish. Now it is appended to
 *                       LittleFS and retried when the link returns. A router
 *                       outage during a wet night no longer deletes exactly
 *                       the hours you care about.
 *
 *  NTP TIME             Buffered readings are useless without knowing when
 *                       they were taken. The board syncs to NTP over WiFi and
 *                       stamps each reading, so a backfilled row lands at its
 *                       real time rather than the time it was flushed.
 *
 *  EMISSIVITY           The MLX90614 assumes emissivity 1.0. Leaves are around
 *                       0.96. Uncorrected, leaf temperature is biased by up to
 *                       a degree or so - and the entire wetness scale is only
 *                       2 C wide, so that bias moves wetness by up to 50 points
 *                       on every reading. Corrected here.
 *
 *  LED FAULT CODES      You can diagnose the box from the ground without a
 *                       laptop. Count the blinks.
 *
 *  SENSOR REPORTING     Each payload says which sensors answered on the bus,
 *                       so the Pi can alert you when one drops off instead of
 *                       the data quietly getting thinner.
 *
 * LED codes (GPIO2):
 *      solid on   everything healthy
 *      1 blink    WiFi down
 *      2 blinks   a core sensor is missing from the I2C bus
 *      3 blinks   cannot reach the API, readings are being buffered
 *      4 blinks   clock never synced, timestamps unreliable
 *
 * Pin map:
 *   SDA        GPIO21      BME280 + MLX90614 + ADS1115
 *   SCL        GPIO22
 *   Raindrop   AO GPIO35, DO GPIO32
 *   Battery    GPIO39 (47k/10k divider, needs -DUSE_BATTERY_ADC=1)
 *   Status LED GPIO2
 */

#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <time.h>

#include <Adafruit_BME280.h>
#include <Adafruit_MLX90614.h>
#include <Adafruit_ADS1X15.h>

// ---------- pins ----------
static const int PIN_SDA        = 21;
static const int PIN_SCL        = 22;
static const int PIN_RAIN_AO    = 35;
static const int PIN_RAIN_DO    = 32;
static const int PIN_BATTERY    = 39;
static const int PIN_STATUS_LED = 2;

// ---------- timing ----------
static const unsigned long POST_INTERVAL_MS = 60UL * 1000UL;
static const unsigned long WIFI_TIMEOUT_MS  = 20UL * 1000UL;

// ---------- buffer ----------
static const char *BUFFER_PATH = "/buf.jsonl";
static const char *TEMP_PATH   = "/buf.tmp";
static const size_t MAX_BUFFER_BYTES  = 220000;  // ~1100 readings
static const int    MAX_FLUSH_PER_LOOP = 20;     // do not stall the cycle

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
unsigned long lastPost = 0;
Fault currentFault = FAULT_NONE;

void IRAM_ATTR onRainTip() { rainTips++; }

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
 * The alternative is writing 0x96 to the sensor's emissivity register, which
 * is permanent and harder to undo. Doing it in software keeps the raw value
 * available: both are sent, so you can re-derive either later.
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
  if (WiFi.status() != WL_CONNECTED)              currentFault = FAULT_WIFI;
  else if (!haveBME || !haveMLX || !haveADS)      currentFault = FAULT_SENSOR;
  else if (!clockSynced)                          currentFault = FAULT_CLOCK;
  else                                            currentFault = FAULT_NONE;
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
    Serial.printf("[wifi] connected, ip %s rssi %d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println("[wifi] FAILED - readings will be buffered to flash");
  }
}

void syncClock() {
  if (WiFi.status() != WL_CONNECTED) return;

  configTime(0, 0, PI_HOST, "pool.ntp.org", "time.google.com");
  Serial.print("[time] syncing");

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
  Serial.println(" FAILED - timestamps will fall back to the server's clock");
}

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
    Serial.println("MLX90614 not found - leaf_wetness will be absent (Stage B)");
  }

  if (ads.begin(0x48, &Wire)) {
    haveADS = true;
    ads.setGain(GAIN_ONE);
    Serial.println("ADS1115 OK @ 0x48");
  } else {
    Serial.println("ADS1115 not found - analog front end unavailable (Stage B)");
  }
}

// ============================ buffer ============================

size_t bufferSize() {
  if (!LittleFS.exists(BUFFER_PATH)) return 0;
  File f = LittleFS.open(BUFFER_PATH, "r");
  if (!f) return 0;
  size_t n = f.size();
  f.close();
  return n;
}

/* Keep only the newest half of the buffer. Called when the file gets large.
   Dropping the oldest readings is the right trade: recent data is what you
   need to see the box is alive, and a full buffer means a long outage. */
void trimBuffer() {
  File in = LittleFS.open(BUFFER_PATH, "r");
  if (!in) return;

  size_t skipTo = in.size() / 2;
  in.seek(skipTo);
  in.readStringUntil('\n');            // discard the partial line

  File out = LittleFS.open(TEMP_PATH, "w");
  if (!out) { in.close(); return; }

  while (in.available()) {
    String line = in.readStringUntil('\n');
    if (line.length() > 2) { out.print(line); out.print('\n'); }
  }
  in.close(); out.close();

  LittleFS.remove(BUFFER_PATH);
  LittleFS.rename(TEMP_PATH, BUFFER_PATH);
  Serial.println("[buf] trimmed - oldest half dropped");
}

void bufferReading(const String &body) {
  if (bufferSize() > MAX_BUFFER_BYTES) trimBuffer();

  File f = LittleFS.open(BUFFER_PATH, "a");
  if (!f) { Serial.println("[buf] could not open buffer for append"); return; }
  f.print(body); f.print('\n');
  f.close();
  Serial.printf("[buf] stored, buffer now %u bytes\n", (unsigned)bufferSize());
}

int bufferedCount() {
  if (!LittleFS.exists(BUFFER_PATH)) return 0;
  File f = LittleFS.open(BUFFER_PATH, "r");
  if (!f) return 0;
  int n = 0;
  while (f.available()) { f.readStringUntil('\n'); n++; }
  f.close();
  return n;
}

bool postBody(const String &body, bool quiet = false);

/* Send what is queued. Stops at the first failure and keeps the rest, so a
   flaky link does not burn through the whole buffer losing rows. */
void flushBuffer() {
  if (!LittleFS.exists(BUFFER_PATH)) return;
  if (WiFi.status() != WL_CONNECTED) return;

  File in = LittleFS.open(BUFFER_PATH, "r");
  if (!in) return;

  File out = LittleFS.open(TEMP_PATH, "w");
  if (!out) { in.close(); return; }

  int sent = 0, kept = 0;
  bool networkDown = false;

  while (in.available()) {
    String line = in.readStringUntil('\n');
    line.trim();
    if (line.length() < 3) continue;

    if (networkDown || sent >= MAX_FLUSH_PER_LOOP) {
      out.print(line); out.print('\n'); kept++;
      continue;
    }

    if (postBody(line, true)) {
      sent++;
    } else {
      networkDown = true;
      out.print(line); out.print('\n'); kept++;
    }
  }

  in.close(); out.close();
  LittleFS.remove(BUFFER_PATH);
  if (kept > 0) LittleFS.rename(TEMP_PATH, BUFFER_PATH);
  else          LittleFS.remove(TEMP_PATH);

  if (sent > 0) Serial.printf("[buf] flushed %d, %d still queued\n", sent, kept);
}

// ============================ posting ============================

bool postBody(const String &body, bool quiet) {
  if (WiFi.status() != WL_CONNECTED) return false;

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
  return code >= 200 && code < 300;
}

// ============================ main ============================

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== AgriSync Board A  fw " FIRMWARE_VERSION " ===");
  Serial.printf("device: %s -> pi: %s\n", DEVICE_ID, PI_HOST);

  pinMode(PIN_STATUS_LED, OUTPUT);
  pinMode(PIN_RAIN_DO, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_RAIN_DO), onRainTip, FALLING);

  if (!LittleFS.begin(true)) {
    Serial.println("[fs] LittleFS mount failed - buffering disabled");
  } else {
    Serial.printf("[fs] mounted, %d readings already queued\n", bufferedCount());
  }

  setupSensors();
  connectWiFi();
  syncClock();
  updateFault();
}

void loop() {
  serviceStatusLed();

  if (lastPost != 0 && millis() - lastPost < POST_INTERVAL_MS) {
    delay(20);
    return;
  }
  lastPost = millis();

  connectWiFi();
  if (!clockSynced) syncClock();

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["firmware"]  = FIRMWARE_VERSION;

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
    // temperature estimate the board cannot make - and because a correction
    // that lives on the server can be improved and re-applied to the whole
    // dataset without reflashing a pole-mounted box.
    doc["leaf_temp_raw_c"] = rawLeaf;
    doc["mlx_ambient_c"]   = ambient;

    // Local approximation, used only for this board's own logging. The
    // server value is the authoritative one.
    float leafT = correctForEmissivity(rawLeaf, ambient);
    doc["leaf_temp_c"] = leafT;

    if (!isnan(airT) && !isnan(rh)) {
      float dp = dewPointC(airT, rh);
      doc["dew_point_c"]  = dp;
      doc["leaf_wetness"] = leafWetnessIndex(leafT, dp);
    }
  }

  doc["rain_analog"] = analogRead(PIN_RAIN_AO);
  doc["rain_tips"]   = rainTips;

#ifdef USE_BATTERY_ADC
  int raw = analogRead(PIN_BATTERY);
  doc["battery_v"] = (raw / 4095.0f) * 3.3f * ((47.0f + 10.0f) / 10.0f);
#endif

  doc["rssi"]            = WiFi.RSSI();
  doc["buffered_count"]  = bufferedCount();

  String body;
  serializeJson(doc, body);
  Serial.printf("[payload] %s\n", body.c_str());

  bool ok = postBody(body, false);
  if (!ok) {
    bufferReading(body);
    currentFault = FAULT_API;
  } else {
    flushBuffer();   // link is up, drain anything queued
    updateFault();
  }
}
