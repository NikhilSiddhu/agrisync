#pragma once
/*
 * SIMCOM A7670C over UART2, talking HTTPS to api.agrisync.tech.
 *
 * WRITTEN BEFORE THE HARDWARE ARRIVED. Every AT command here comes from the
 * A7670 application notes rather than from a board on a bench, so treat the
 * first bring-up as a debugging session, not a formality. Set MODEM_TRACE to 1
 * and you will see every byte in both directions.
 *
 * WIRING
 *   ESP32 GPIO26 (TX2) -> modem RXD
 *   ESP32 GPIO27 (RX2) -> modem TXD
 *   common ground, and the modem on its OWN supply, not the ESP32's 3V3
 *
 * POWER, AND WHY IT MATTERS MORE THAN THE CODE
 *   The A7670C draws up to 2 A in bursts when the radio transmits. If the
 *   supply sags below about 3.4 V it resets mid-request, which looks exactly
 *   like a firmware bug and is not one. At least 470 uF of bulk capacitance
 *   at the module, short thick power leads, and never share the ESP32's
 *   regulator.
 *
 * ANTENNA
 *   Attached before power, every single time. Transmitting into an open
 *   connector reflects RF back into the amplifier and can destroy it.
 */

#include <Arduino.h>

#ifndef MODEM_TRACE
#define MODEM_TRACE 0
#endif

class Modem {
public:
  Modem(HardwareSerial &port, int rxPin, int txPin, const char *apn)
      : _s(port), _rx(rxPin), _tx(txPin), _apn(apn) {}

  void begin(uint32_t baud = 115200) {
    _s.begin(baud, SERIAL_8N1, _rx, _tx);
    delay(200);
    _s.setTimeout(1000);
  }

  /* Wake the module and confirm it answers. The A7670 ignores the first few
     bytes after power-up, so this is deliberately patient rather than clever. */
  bool sync(uint8_t attempts = 12) {
    for (uint8_t i = 0; i < attempts; i++) {
      if (at("AT", 1000)) {
        at("ATE0", 1000);          // stop it echoing back everything we send
        at("AT+CMEE=2", 1000);     // verbose errors, worth having on a first build
        return true;
      }
      delay(700);
    }
    return false;
  }

  bool simReady()   { return atContains("AT+CPIN?", "READY", 5000); }
  bool registered() { return atContains("AT+CREG?", ",1", 3000)
                          || atContains("AT+CREG?", ",5", 3000); }
  bool attached()   { return atContains("AT+CGATT?", "+CGATT: 1", 10000); }

  int signal() {
    String r;
    if (!atRead("AT+CSQ", r, 3000)) return 99;
    int i = r.indexOf("+CSQ:");
    if (i < 0) return 99;
    return r.substring(i + 6, r.indexOf(',', i)).toInt();   // 0-31, 99 = none
  }

  /* Bring up the packet data context. Safe to call repeatedly. */
  bool dataUp() {
    char buf[96];
    snprintf(buf, sizeof(buf), "AT+CGDCONT=1,\"IP\",\"%s\"", _apn);
    at(buf, 3000);
    at("AT+CNACT=0,1", 8000);          // activate
    String r;
    atRead("AT+CNACT?", r, 3000);
    return r.indexOf("+CNACT: 0,1") >= 0;
  }

  String localIP() {
    String r;
    atRead("AT+CNACT?", r, 3000);
    int q = r.indexOf('"');
    if (q < 0) return "";
    int e = r.indexOf('"', q + 1);
    return e > q ? r.substring(q + 1, e) : "";
  }

  /*
   * POST one JSON body over HTTPS.
   *
   * Three headers, and the two Cloudflare ones are what let a device through
   * Access without a browser login:
   *     X-API-Key                 our own device key
   *     CF-Access-Client-Id       the service token
   *     CF-Access-Client-Secret
   *
   * Returns the HTTP status, or -1 if the exchange never completed.
   */
  int postJson(const char *host, const char *path, const String &body,
               const char *apiKey, const char *cfId, const char *cfSecret) {
    char buf[192];

    at("AT+SHDISC", 2000);                       // drop anything half-open
    snprintf(buf, sizeof(buf), "AT+SHCONF=\"URL\",\"https://%s\"", host);
    if (!at(buf, 3000)) return -1;

    at("AT+SHCONF=\"BODYLEN\",1024", 2000);
    at("AT+SHCONF=\"HEADERLEN\",512", 2000);
    at("AT+SHSSL=1,\"\"", 2000);                 // TLS, default CA bundle

    if (!at("AT+SHCONN", 30000)) return -1;      // handshake can be slow on 4G

    at("AT+SHCHEAD", 2000);
    at("AT+SHAHEAD=\"Content-Type\",\"application/json\"", 2000);

    snprintf(buf, sizeof(buf), "AT+SHAHEAD=\"X-API-Key\",\"%s\"", apiKey);
    at(buf, 2000);
    if (cfId && *cfId) {
      snprintf(buf, sizeof(buf), "AT+SHAHEAD=\"CF-Access-Client-Id\",\"%s\"", cfId);
      at(buf, 2000);
    }
    if (cfSecret && *cfSecret) {
      snprintf(buf, sizeof(buf), "AT+SHAHEAD=\"CF-Access-Client-Secret\",\"%s\"", cfSecret);
      at(buf, 2000);
    }

    snprintf(buf, sizeof(buf), "AT+SHBOD=%d,10000", body.length());
    _s.print(buf); _s.print("\r\n");
    if (!waitFor(">", 5000)) { at("AT+SHDISC", 2000); return -1; }
    _s.print(body);

    String resp;
    snprintf(buf, sizeof(buf), "AT+SHREQ=\"%s\",3", path);   // 3 = POST
    _s.print(buf); _s.print("\r\n");
    if (!readUntil("+SHREQ:", resp, 30000)) { at("AT+SHDISC", 2000); return -1; }

    int status = -1;
    int c = resp.lastIndexOf(',');
    if (c > 0) {
      int p = resp.lastIndexOf(',', c - 1);
      if (p > 0) status = resp.substring(p + 1, c).toInt();
    }
    at("AT+SHDISC", 3000);
    return status;
  }

  void sleep()  { at("AT+CSCLK=1", 2000); }
  void wake()   { at("AT+CSCLK=0", 2000); }

private:
  HardwareSerial &_s;
  int _rx, _tx;
  const char *_apn;

  void trace(const char *dir, const String &s) {
#if MODEM_TRACE
    Serial.printf("  %s %s\n", dir, s.c_str());
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
    bool ok = readUntil("OK", out, timeout) || out.indexOf("ERROR") >= 0;
    trace("<<", out);
    return ok;
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
        if (out.length() > 1200) out.remove(0, 600);   // never grow unbounded
      }
      delay(5);
    }
    return false;
  }
};
