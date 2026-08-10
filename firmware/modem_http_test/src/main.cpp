/*
 * AgriSync - A7670C HTTPS host characterisation   v7
 *
 * WHERE WE ARE
 *   howsmyssl.com      200   modem TLS genuinely works
 *   example.com        715
 *   api.agrisync.tech  715
 *
 *   AT+CSSLCFG=? confirms enableSNI IS supported. Enabling it changed nothing,
 *   so SNI is not the cause. There is no ciphersuite parameter on this
 *   firmware, so that was never available either.
 *
 * WHAT THIS RUN ANSWERS
 *   Which kinds of HTTPS servers can this modem reach? If simple single-cert
 *   hosts work and big CDN endpoints do not, then the fix is architectural:
 *   put an ingress in front that this modem can actually talk to, instead of
 *   trying to make Cloudflare's edge speak to a 2019 TLS stack.
 *
 * It also tries ignorecertCN, the last untested SSL flag.
 *
 * Goes at:  ~/agrisync/firmware/modem_http_test/src/main.cpp
 */

#include <Arduino.h>

#define MODEM_TX_PIN 26
#define MODEM_RX_PIN 27
#define MODEM_BAUD   115200
#define QUIET_MS 500

// ---------------------------------------------------------------------------

void drain() {
  uint32_t last = millis();
  while (millis() - last < QUIET_MS) {
    while (Serial2.available()) { Serial.write(Serial2.read()); last = millis(); }
    delay(10);
  }
}

String cmd(const char *c, const char *want = "OK", uint32_t t = 5000) {
  while (Serial2.available()) Serial2.read();
  Serial.printf("\n>> %s\n", c);
  Serial2.print(c); Serial2.print("\r\n");

  String buf; buf.reserve(512);
  uint32_t start = millis();
  while (millis() - start < t) {
    while (Serial2.available()) {
      char ch = (char)Serial2.read();
      Serial.write(ch);
      if (buf.length() < 3000) buf += ch;
    }
    if (want && buf.indexOf(want) >= 0) break;
    if (buf.indexOf("ERROR") >= 0) break;
    delay(5);
  }
  drain();
  return buf;
}

bool ok(const char *c, uint32_t t = 5000) { return cmd(c, "OK", t).indexOf("OK") >= 0; }
bool ok(const String &c, uint32_t t = 5000) { return ok(c.c_str(), t); }

int actionStatus(const String &r) {
  int i = r.indexOf("+HTTPACTION:");
  if (i < 0) return -1;
  int c1 = r.indexOf(',', i), c2 = r.indexOf(',', c1 + 1);
  if (c1 < 0 || c2 < 0) return -1;
  return r.substring(c1 + 1, c2).toInt();
}

int get(const char *url) {
  ok("AT+HTTPTERM", 3000);
  if (!ok("AT+HTTPINIT", 8000)) { delay(800); if (!ok("AT+HTTPINIT", 8000)) return -2; }
  ok(String("AT+HTTPPARA=\"URL\",\"") + url + "\"");
  ok("AT+HTTPPARA=\"SSLCFG\",0");
  int s = actionStatus(cmd("AT+HTTPACTION=0", "+HTTPACTION:", 60000));
  ok("AT+HTTPTERM", 5000);
  delay(1200);
  return s;
}

// ---------------------------------------------------------------------------

struct Host { const char *url; const char *note; int result; };

Host hosts[] = {
  { "https://nikhil.tail934d76.ts.net", "tailscale",  0 },
  { "https://httpbin.org/get",           "simple, no big CDN",       0 },
  { "https://postman-echo.com/get",      "simple API host",          0 },
  { "https://ntfy.sh/",                  "you already use this",     0 },
  { "https://api.thingspeak.com/",       "IoT host, built for this", 0 },
  { "https://example.com",               "known fail, big CDN",      0 },
  { "https://api.agrisync.tech/health",  "yours, Cloudflare",        0 },
};
const int NHOSTS = sizeof(hosts) / sizeof(hosts[0]);

void setup() {
  Serial.begin(115200);
  Serial2.begin(MODEM_BAUD, SERIAL_8N1, MODEM_RX_PIN, MODEM_TX_PIN);
  delay(1500);

  Serial.println("\n\n=== AgriSync HTTPS host characterisation v7 ===");

  bool alive = false;
  for (int i = 0; i < 15; i++) { if (ok("AT", 1000)) { alive = true; break; } delay(1000); }
  if (!alive) { Serial.println("[FAIL] modem silent"); return; }

  ok("ATE0");
  ok("AT+CMEE=2");
  ok("AT+HTTPTERM", 3000);

  bool att = false;
  for (int i = 0; i < 20; i++) {
    if (cmd("AT+CGATT?", "OK", 5000).indexOf("+CGATT: 1") >= 0) { att = true; break; }
    delay(3000);
  }
  if (!att) { Serial.println("[FAIL] not attached"); return; }

  // Everything the firmware actually offers, all switched to most permissive.
  Serial.println("\n\n===== TLS CONFIG (maximally permissive) =====");
  ok("AT+CSSLCFG=\"sslversion\",0,4");
  ok("AT+CSSLCFG=\"authmode\",0,0");
  ok("AT+CSSLCFG=\"ignorelocaltime\",0,1");
  ok("AT+CSSLCFG=\"enableSNI\",0,1");
  ok("AT+CSSLCFG=\"ignorecertCN\",0,1");     // last untested flag
  ok("AT+CSSLCFG=\"negotiatetime\",0,300");

  Serial.println("\n\n===== TESTING HOSTS =====");
  for (int i = 0; i < NHOSTS; i++) {
    Serial.printf("\n\n----- %d/%d  %s -----\n%s\n",
                  i + 1, NHOSTS, hosts[i].note, hosts[i].url);
    hosts[i].result = get(hosts[i].url);
    Serial.printf("\n>>> %d\n", hosts[i].result);
  }

  Serial.println("\n\n============ SUMMARY ============");
  int worked = 0;
  for (int i = 0; i < NHOSTS; i++) {
    const char *tag = (hosts[i].result >= 200 && hosts[i].result < 400) ? "PASS" : "fail";
    if (hosts[i].result >= 200 && hosts[i].result < 400) worked++;
    Serial.printf("%-4s  %-4d  %-38s  %s\n",
                  tag, hosts[i].result, hosts[i].url, hosts[i].note);
  }

  Serial.printf("\n%d of %d reachable over HTTPS.\n", worked, NHOSTS);
  Serial.println("\nIf several simple hosts pass and only the CDN ones fail,");
  Serial.println("the modem is fine and the answer is to give it an ingress");
  Serial.println("it can actually talk to, rather than Cloudflare's edge.");

  Serial.println("\n=== done ===");
}

void loop() {}
