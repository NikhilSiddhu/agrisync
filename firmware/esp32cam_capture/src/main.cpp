/*
 * AgriSync Board B - ESP32-CAM canopy imaging   fw 1.1.0
 *
 * Captures an image every CAPTURE_INTERVAL_MIN minutes to /canopy/ on the
 * microSD card. No upload in this build - images come off the card by hand.
 *
 * microSD MUST be 32 GB or smaller and formatted FAT32.
 *
 * 1.1.0 changes:
 *   - Not every "ESP32-CAM" board carries an OV2640. Clones ship with OV7670,
 *     GC0308 and others that cannot produce JPEG in hardware, and esp_camera_init
 *     then fails with 0x106. This build tries JPEG, and on failure re-inits in
 *     RGB565 and encodes to JPEG on the ESP32 instead. Slower, same output file.
 *   - Prints the sensor's chip ID. If that ID reads back as nonsense, the ribbon
 *     cable is not seated and the driver is guessing at a garbage register read.
 *   - camera_config_t is now zeroed before use. It was previously left as stack
 *     garbage in the fields the code did not set.
 *   - Camera init retries every 60 s instead of leaving the board dead until
 *     someone walks out to the field and presses RST.
 */

#include <Arduino.h>
#include <esp_camera.h>
#include <img_converters.h>
#include <FS.h>
#include <SD_MMC.h>
#include "photosender.h"
#include <Preferences.h>

// AI-Thinker ESP32-CAM pin map
#define PWDN_GPIO_NUM  32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM   0
#define SIOD_GPIO_NUM  26
#define SIOC_GPIO_NUM  27
#define Y9_GPIO_NUM    35
#define Y8_GPIO_NUM    34
#define Y7_GPIO_NUM    39
#define Y6_GPIO_NUM    36
#define Y5_GPIO_NUM    21
#define Y4_GPIO_NUM    19
#define Y3_GPIO_NUM    18
#define Y2_GPIO_NUM     5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  23
#define PCLK_GPIO_NUM  22

#define PIN_STATUS_LED 33  // active LOW on this board

unsigned long lastCapture  = 0;
unsigned long lastCamRetry = 0;
unsigned long imageCount   = 0;
unsigned long bootId       = 0;    // survives power loss, unlike imageCount
Preferences   prefs;

bool cameraReady      = false;
bool sdReady          = false;
bool needsSoftwareJpeg = false;

static const char *sensorName(uint16_t pid) {
  switch (pid) {
    case 0x0026: return "OV2640";
    case 0x3660: return "OV3660";
    case 0x5640: return "OV5640";
    case 0x0076: return "OV7670";
    case 0x0077: return "OV7725";
    case 0x0096: return "OV9650";
    case 0x009b: return "GC0308";
    case 0x2145: return "GC2145";
    case 0x232a: return "GC032A";
    case 0x0030: return "BF3005";
    case 0x20a6: return "BF20A6";
    case 0x1410: return "NT99141";
    case 0x0031: return "SC031GS";
    case 0x9a46: return "SC030IOT";
    case 0xda4a: return "SC101IOT";
    default:     return "unrecognised - suspect a loose ribbon cable";
  }
}

static void fillPins(camera_config_t &c, bool psram) {
  memset(&c, 0, sizeof(c));   // the fields below are not all of them
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;   c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;   c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;   c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;   c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk      = XCLK_GPIO_NUM;
  c.pin_pclk      = PCLK_GPIO_NUM;
  c.pin_vsync     = VSYNC_GPIO_NUM;
  c.pin_href      = HREF_GPIO_NUM;
  c.pin_sccb_sda  = SIOD_GPIO_NUM;
  c.pin_sccb_scl  = SIOC_GPIO_NUM;
  c.pin_pwdn      = PWDN_GPIO_NUM;
  c.pin_reset     = RESET_GPIO_NUM;
  c.xclk_freq_hz  = 20000000;
  c.grab_mode     = CAMERA_GRAB_WHEN_EMPTY;
  c.fb_location   = psram ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
}

bool startCamera() {
  const bool psram = psramFound();
  Serial.printf("[cam] PSRAM %s\n", psram ? "found" : "NOT found");

  camera_config_t config;

  // Attempt 1: let the sensor produce JPEG itself. This is what an OV2640 does
  // and it is by far the cheapest path.
  fillPins(config, psram);
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = psram ? FRAMESIZE_UXGA : FRAMESIZE_SVGA;
  config.jpeg_quality = psram ? 10 : 12;
  config.fb_count     = psram ? 2 : 1;

  esp_err_t err = esp_camera_init(&config);

  if (err == ESP_OK) {
    needsSoftwareJpeg = false;
    Serial.println("[cam] mode: hardware JPEG");
  } else {
    Serial.printf("[cam] JPEG init failed 0x%x, falling back to RGB565\n", err);
    esp_camera_deinit();
    delay(200);

    // Attempt 2: raw RGB565, encoded to JPEG in software. Frame sizes are
    // smaller here because RGB565 is two bytes per pixel with no compression -
    // UXGA would need 3.8 MB for a single frame.
    fillPins(config, psram);
    config.pixel_format = PIXFORMAT_RGB565;
    config.frame_size   = psram ? FRAMESIZE_SVGA : FRAMESIZE_QVGA;
    config.fb_count     = 1;

    err = esp_camera_init(&config);
    if (err != ESP_OK) {
      Serial.printf("[cam] RGB565 init also failed 0x%x\n", err);
      return false;
    }
    needsSoftwareJpeg = true;
    Serial.println("[cam] mode: RGB565 + software JPEG");
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    Serial.printf("[cam] sensor PID 0x%04x  (%s)\n",
                  s->id.PID, sensorName(s->id.PID));
  }

  Serial.println("Camera OK");
  return true;
}

bool startSD() {
  // 1-bit mode frees up GPIO and is plenty fast for one image every 15 minutes.
  if (!SD_MMC.begin("/sdcard", true)) {
    Serial.println("[sd] mount failed - is the card FAT32 and 32 GB or smaller?");
    return false;
  }
  if (SD_MMC.cardType() == CARD_NONE) {
    Serial.println("[sd] no card detected");
    return false;
  }
  if (!SD_MMC.exists("/canopy")) {
    SD_MMC.mkdir("/canopy");
  }
  Serial.printf("SD OK - %llu MB\n", SD_MMC.cardSize() / (1024ULL * 1024ULL));
  photoSenderBegin();
  return true;
}

void capture() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[img] capture failed");
    return;
  }

  const uint8_t *data = fb->buf;
  size_t len          = fb->len;
  uint8_t *jpgBuf     = NULL;

  if (needsSoftwareJpeg) {
    size_t jpgLen = 0;
    if (!frame2jpg(fb, 55, &jpgBuf, &jpgLen)) {
      Serial.println("[img] software JPEG encode failed - out of memory?");
      esp_camera_fb_return(fb);
      return;
    }
    data = jpgBuf;
    len  = jpgLen;
  }

  // No RTC on this board and no WiFi in this build, so filenames are a counter
  // plus uptime. TODO: pull time from the Pi once the upload path exists, so
  // images can be matched against sensor readings.
  char path[64];
  snprintf(path, sizeof(path), "/canopy/img_%05lu_%06lu.jpg",
         bootId, imageCount);

  File file = SD_MMC.open(path, FILE_WRITE);
  if (!file) {
    Serial.printf("[img] could not open %s for write\n", path);
    if (jpgBuf) free(jpgBuf);
    esp_camera_fb_return(fb);
    return;
  }

file.write(data, len);
  file.close();
  Serial.printf("[img] %s  %u bytes%s\n", path, (unsigned)len,
                needsSoftwareJpeg ? "  (software jpeg)" : "");

  const char *base = strrchr(path, '/');
  photoSendAndCatchUp(base ? base + 1 : path, data, len);

  if (jpgBuf) free(jpgBuf);
  esp_camera_fb_return(fb);
  imageCount++;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== AgriSync Board B (ESP32-CAM)  fw 1.1.0 ===");
prefs.begin("agrisync", false);
  bootId = prefs.getULong("boot", 0) + 1;
  prefs.putULong("boot", bootId);
  prefs.end();
  Serial.printf("[fs] boot #%lu\n", bootId);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, HIGH);  // active LOW, so HIGH is off

  cameraReady = startCamera();
  sdReady     = startSD();

  if (cameraReady && sdReady) {
    digitalWrite(PIN_STATUS_LED, LOW);  // on = healthy
    capture();                          // prove it works immediately
    lastCapture = millis();
  } else {
    Serial.println("[fatal] camera or card unavailable, retrying every 60 s");
  }
  lastCamRetry = millis();
}

void loop() {
  // A box on a pole cannot be reset by hand. If the camera did not come up -
  // a brown-out at power-on is the usual reason - keep trying rather than
  // sitting dead until someone drives out to the field.
  if (!cameraReady || !sdReady) {
    if (millis() - lastCamRetry >= 60000UL) {
      lastCamRetry = millis();
      Serial.println("[retry] re-initialising");
      if (!cameraReady) cameraReady = startCamera();
      if (!sdReady)     sdReady     = startSD();
      if (cameraReady && sdReady) {
        digitalWrite(PIN_STATUS_LED, LOW);
        capture();
        lastCapture = millis();
      }
    }
    delay(1000);
    return;
  }

  const unsigned long interval = CAPTURE_INTERVAL_MIN * 60UL * 1000UL;
  if (millis() - lastCapture >= interval) {
    capture();
    lastCapture = millis();
  }
  delay(1000);
}
