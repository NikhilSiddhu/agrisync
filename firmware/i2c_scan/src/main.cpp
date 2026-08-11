/*
 * AgriSync - continuous I2C scanner
 *
 * The full firmware takes ~40 seconds per attempt because it brings the
 * modem up first. This does nothing but scan, every 2 seconds, at three bus
 * speeds. Swap wires with it running and watch the result change live.
 *
 * Goes at:  ~/agrisync/firmware/i2c_scan/src/main.cpp
 *
 * WHAT TO TRY, in this order. Leave it running throughout.
 *   1. MLX board 1, SCL->22  SDA->21      (silkscreen order)
 *   2. MLX board 1, SCL->21  SDA->22      (crossed)
 *   3. MLX board 2, SCL->22  SDA->21
 *   4. MLX board 2, SCL->21  SDA->22
 *
 * All four silent, with VIN at 5V and both lines idling at 3.3V, means the
 * parts are not speaking I2C at all - either dead, or shipped in PWM mode.
 *
 * KNOWN ADDRESSES
 *   0x5A  MLX90614      the one we are hunting
 *   0x76  BME280
 *   0x48  ADS1115
 */

#include <Arduino.h>
#include <Wire.h>

static const int PIN_SDA = 21;
static const int PIN_SCL = 22;

const char *known(uint8_t a) {
  switch (a) {
    case 0x5A: return "MLX90614   <<< THE ONE WE WANT";
    case 0x76: return "BME280";
    case 0x77: return "BME280 (alt address)";
    case 0x48: return "ADS1115";
    case 0x68: return "RTC or IMU";
    default:   return "unknown";
  }
}

int scanAt(uint32_t hz) {
  Wire.setClock(hz);
  delay(50);

  int found = 0;
  bool mlx = false;

  Serial.printf("\n--- %lu Hz ---\n", (unsigned long)hz);
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  0x%02X  %s\n", addr, known(addr));
      found++;
      if (addr == 0x5A) mlx = true;
    }
    delay(2);
  }

  if (!found) Serial.println("  nothing at all - check SDA/SCL and power");
  else if (!mlx) Serial.printf("  %d device(s), no MLX at 0x5A\n", found);

  return mlx ? 1 : 0;
}

void setup() {
  Serial.begin(115200);
  delay(400);

  Serial.println("\n\n=== AgriSync I2C scanner ===");
  Serial.printf("SDA=GPIO%d  SCL=GPIO%d\n", PIN_SDA, PIN_SCL);
  Serial.println("Scanning every 2s. Swap wires while it runs.\n");

  Wire.begin(PIN_SDA, PIN_SCL);
  delay(300);            // MLX90614 needs ~250ms after power before it answers
}

void loop() {
  static uint32_t pass = 0;
  Serial.printf("\n================ pass %lu ================",
                (unsigned long)++pass);

  // The MLX is an SMBus part. Some modules refuse 400kHz outright, and a few
  // only answer well below 100kHz.
  int hits = 0;
  hits += scanAt(400000);
  hits += scanAt(100000);
  hits += scanAt(50000);

  if (hits) {
    Serial.println("\n**********************************");
    Serial.println("  MLX90614 FOUND at 0x5A");
    Serial.println("  Note which speed and wiring worked.");
    Serial.println("**********************************");
  }

  delay(2000);
}
