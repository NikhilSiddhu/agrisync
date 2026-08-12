#!/usr/bin/env python3
"""Fix the final-partial-chunk corruption in postBinary.

    cd ~/agrisync && python3 fix_postbinary.py

THE BUG, MEASURED
    A sweep of payload sizes showed uploads arrive intact when they are a
    single chunk or an exact multiple of B64_CHUNK_IN (3072), and altered
    otherwise. Comparing a known payload byte for byte:

        21848 bytes sent, 21848 received, exactly ONE differing byte,
        at offset 20480 - which is 5 x 4096, the first character of the
        final partial chunk. 'A' arrived as 'O'.

    Deterministic, always the same offset, always the first byte after the
    last full chunk. Camera images are ~20KB, so they are essentially always
    "several full chunks plus a partial one" - which is why every photo failed
    while an 8-byte test passed.

THE CAUSE
    The old code encoded the image twice - once to hash, once to send - and
    began a fresh write burst for each chunk. The first byte of a short final
    burst is what gets lost. Rather than work out which layer eats it (the
    ESP32 UART driver, the modem's receive path, or the timing between them),
    the boundary itself is removed.

THE FIX
    Encode once into one buffer. Hash that buffer. Send that buffer as a
    single uniform stream. No per-chunk restarts, so no boundary to corrupt.

WHAT IT COSTS
    About 4/3 the image size in heap - roughly 32KB for a 24KB photo, on top
    of the image itself. The original comment argued against this to protect
    the heap, and that instinct was reasonable; it just happened to trade a
    real correctness bug for memory this board can spare. The allocation is
    checked and fails cleanly if the heap is ever too tight.
"""
import shutil, sys
from pathlib import Path

p = Path.home() / "agrisync/firmware/synthetic_wifi/src/modem.h"
if not p.is_file():
    sys.exit(f"cannot find {p}")

t = p.read_text()
if "single buffer, one hash, one write" in t:
    sys.exit("already applied.")

start = t.find("  int postBinary(")
if start < 0:
    sys.exit("could not find postBinary")
# End of the function: the closing brace at two-space indent.
end = t.find("\n  }\n", start)
if end < 0:
    sys.exit("could not find the end of postBinary")
end += len("\n  }\n")

NEW = r'''  int postBinary(const char *host, const char *path,
                 const uint8_t *data, size_t len, const char *hmacSecret) {
    long ts = syncTime();
    if (!ts) { trace("--", "no clock, cannot sign"); return -2; }

    size_t b64len = ((len + 2) / 3) * 4;

    if (b64len > 60000) {
      /* AT+HTTPDATA has a body ceiling on this firmware, and its <time>
         argument tops out at 60000 - 90000 is rejected outright and the
         DOWNLOAD prompt never arrives. Measured the hard way. */
      trace("--", "encoded body too large for AT+HTTPDATA");
      return -3;
    }

    /* ---- single buffer, one hash, one write --------------------------------
       The previous version encoded the image twice - once to feed the HMAC,
       once to feed the modem - and started a separate write burst per chunk.
       A size sweep showed that any payload spanning more than one chunk with a
       partial last chunk arrived with exactly one byte wrong, at the first
       character of that final chunk. 21848 bytes sent, one byte altered at
       offset 20480. Always the same place.

       Encoding once and writing one continuous stream removes the boundary,
       and with it the bug. It costs roughly 4/3 of the image in heap, which
       is a fair price for uploads that actually verify. */
    uint8_t *b64 = (uint8_t *)malloc(b64len + 1);
    if (!b64) { trace("--", "no heap for encode buffer"); return -1; }

    size_t olen = 0;
    if (mbedtls_base64_encode(b64, b64len + 1, &olen, data, len) != 0 ||
        olen != b64len) {
      free(b64);
      trace("--", "base64 encode failed");
      return -1;
    }

    char pfx[24];
    int pfxLen = snprintf(pfx, sizeof(pfx), "%ld.", ts);

    /* Signs what actually goes on the wire - the base64 text, not the raw
       image - so any alteration in flight is caught. hmacHex2 hashes the
       prefix and the body without joining them, keeping one more copy of a
       30KB payload off the heap. */
    String hdr = String(ts) + "." +
                 hmacHex2(hmacSecret, (const uint8_t *)pfx, pfxLen, b64, olen);

    char buf[256];

    at("AT+HTTPTERM", 3000);
    if (!at("AT+HTTPINIT", 8000)) {
      delay(1000);
      if (!at("AT+HTTPINIT", 8000)) { free(b64); return -1; }
    }

    snprintf(buf, sizeof(buf),
             "AT+HTTPPARA=\"URL\",\"http://%s%s&enc=b64\"", host, path);
    if (!at(buf, 5000)) { at("AT+HTTPTERM", 3000); free(b64); return -1; }

    at("AT+HTTPPARA=\"CONTENT\",\"text/plain\"", 5000);

    String h = "AT+HTTPPARA=\"USERDATA\",\"X-Sig: " + hdr + "\"";
    at(h.c_str(), 5000);

    snprintf(buf, sizeof(buf), "AT+HTTPDATA=%u,60000", (unsigned)b64len);
    trace(">>", buf);
    _s.print(buf); _s.print("\r\n");
    if (!waitFor("DOWNLOAD", 15000)) {
      trace("--", "no DOWNLOAD prompt");
      at("AT+HTTPTERM", 3000);
      free(b64);
      return -1;
    }

    // One uniform stream. Every piece is the same size except the last, and
    // there are no restarts partway through.
    size_t sent = 0;
    while (sent < olen) {
      size_t w = (olen - sent > 512) ? 512 : (olen - sent);
      _s.write(b64 + sent, w);
      sent += w;
      delay(2);
    }
    _s.flush();
    free(b64);

    String tmp;
    if (!readUntil("OK", tmp, 30000)) {
      trace("--", "body not accepted");
      at("AT+HTTPTERM", 3000);
      return -1;
    }
    settle();

    trace(">>", "AT+HTTPACTION=1");
    String resp;
    _s.print("AT+HTTPACTION=1"); _s.print("\r\n");
    if (!readUntil("+HTTPACTION:", resp, 90000)) {
      at("AT+HTTPTERM", 3000);
      return -1;
    }
    readRestOfLine(resp, 5000);
    trace("<<", resp);

    int status = -1;
    int i  = resp.indexOf("+HTTPACTION:");
    int c1 = resp.indexOf(',', i);
    int c2 = resp.indexOf(',', c1 + 1);
    if (c1 > 0 && c2 > c1) status = resp.substring(c1 + 1, c2).toInt();

    at("AT+HTTPTERM", 5000);
    return status;
  }
'''

shutil.copy(p, p.with_suffix(".h.bak_fix"))
p.write_text(t[:start] + NEW + t[end:])
print("postBinary replaced. Backup at modem.h.bak_fix")
print("Debug [sig] prints are gone with the old function - that is intended.")
