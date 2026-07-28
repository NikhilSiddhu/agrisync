/*
 * AgriSync Board B - ESP32-CAM canopy imaging
 *
 * Captures a JPEG every CAPTURE_INTERVAL_MIN minutes to /canopy/ on the
 * microSD card. No upload in this build - images are collected off the card
 * by hand. On-device upload is on the "still to build" list in the manual.
 *
 * microSD MUST be 32 GB or smaller and formatted FAT32. Bigger cards fail
 * silently: the camera reports OK and no files ever appear.
 */

#include <Arduino.h>
#include <esp_camera.h>
#include <FS.h>
#include <SD_MMC.h>

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

unsigned long lastCapture = 0;
unsigned long imageCount = 0;

bool startCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;   config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;   config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;   config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;   config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href  = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn  = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_UXGA;  // 1600x1200
    config.jpeg_quality = 10;
    config.fb_count     = 2;
  } else {
    Serial.println("[cam] no PSRAM - dropping to SVGA");
    config.frame_size   = FRAMESIZE_SVGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[cam] init failed 0x%x\n", err);
    return false;
  }
  Serial.println("Camera OK");
  return true;
}

bool startSD() {
  // 1-bit mode frees up GPIO and is plenty fast for a JPEG every 15 minutes.
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
  return true;
}

void capture() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("[img] capture failed");
    return;
  }

  // No RTC on this board and no WiFi in this build, so filenames are a
  // simple counter plus uptime. TODO: pull time from the Pi once the
  // upload path is built, so images can be matched to sensor readings.
  char path[64];
  snprintf(path, sizeof(path), "/canopy/img_%06lu_%lus.jpg",
           imageCount, millis() / 1000);

  File file = SD_MMC.open(path, FILE_WRITE);
  if (!file) {
    Serial.printf("[img] could not open %s for write\n", path);
    esp_camera_fb_return(fb);
    return;
  }

  file.write(fb->buf, fb->len);
  file.close();
  Serial.printf("[img] %s  %u bytes\n", path, fb->len);

  esp_camera_fb_return(fb);
  imageCount++;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== AgriSync Board B (ESP32-CAM) ===");

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, HIGH);  // active LOW, so HIGH is off

  bool camOk = startCamera();
  bool sdOk  = startSD();

  if (camOk && sdOk) {
    digitalWrite(PIN_STATUS_LED, LOW);  // on = healthy
    capture();                          // prove it works immediately
    lastCapture = millis();
  } else {
    Serial.println("[fatal] check the wiring and the card, then press RST");
  }
}

void loop() {
  const unsigned long interval = CAPTURE_INTERVAL_MIN * 60UL * 1000UL;
  if (millis() - lastCapture >= interval) {
    capture();
    lastCapture = millis();
  }
  delay(1000);
}
