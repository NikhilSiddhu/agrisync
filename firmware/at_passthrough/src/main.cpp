/*
 * AgriSync - AT passthrough
 *
 * Pipes the ESP32's USB serial straight through to the A7670C on UART2.
 * Type AT commands in the monitor, they go to the modem, replies come back.
 *
 * Purpose: verify the ESP32-to-modem wiring AND run the manual AT session,
 * without unplugging anything or needing the FTDI.
 *
 * Goes at:  ~/agrisync/firmware/at_passthrough/src/main.cpp
 *
 * WIRING (already done)
 *   ESP32 GPIO26 -> modem RXD
 *   ESP32 GPIO27 <- modem TXD
 *   common ground
 *   modem 5V from the buck, 1000uF at its pins, antenna on, SIM in
 *
 * USE
 *   pio run -t upload
 *   pio device monitor -b 115200 --echo
 *
 *   Then type:  AT   <Enter>
 *   Want:       OK
 */

#include <Arduino.h>

#define MODEM_TX_PIN 26   // ESP32 transmits -> modem RXD
#define MODEM_RX_PIN 27   // ESP32 receives  <- modem TXD
#define MODEM_BAUD   115200

String line;

void setup() {
  Serial.begin(115200);
  Serial2.begin(MODEM_BAUD, SERIAL_8N1, MODEM_RX_PIN, MODEM_TX_PIN);
  delay(600);

  Serial.println();
  Serial.println("=== AgriSync AT passthrough ===");
  Serial.printf("TX=GPIO%d -> modem RXD\n", MODEM_TX_PIN);
  Serial.printf("RX=GPIO%d <- modem TXD\n", MODEM_RX_PIN);
  Serial.printf("baud %d\n\n", MODEM_BAUD);
  Serial.println("Type AT and press Enter. You want OK.");
  Serial.println("Silence means: TX/RX not crossed, no common ground,");
  Serial.println("or the modem has no power.\n");
}

void loop() {
  // keyboard -> modem, assembled into whole lines so the terminator is right
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r' || c == '\n') {
      if (line.length()) {
        Serial2.print(line);
        Serial2.print("\r\n");
        line = "";
      }
    } else if (line.length() < 200) {
      line += c;
    }
  }

  // modem -> screen
  while (Serial2.available()) {
    Serial.write(Serial2.read());
  }
}
