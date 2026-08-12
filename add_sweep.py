#!/usr/bin/env python3
"""Replace the 'T' self test with a size sweep, to find where uploads break.

    cd ~/agrisync && python3 add_sweep.py

WHY
    8 bytes signs and uploads correctly; ~20 KB does not. The signing code is
    identical for both, so the fault is size-dependent - the modem alters the
    body somewhere above a threshold we have not measured.

    This sends the same payload at increasing sizes and reports each result.
    401 means the body arrived altered. 400 means it arrived intact and was
    only refused for not being a JPEG.

    The largest size that returns 400 is the safe payload for this modem, and
    that number decides the fix: shrink the images below it, or split each
    upload into pieces under it and reassemble on the Pi.

    Costs about a minute to run and replaces a great deal of guessing.
"""
import shutil, sys
from pathlib import Path

p = Path.home() / "agrisync/firmware/synthetic_wifi/src/main.cpp"
if not p.is_file():
    sys.exit(f"cannot find {p}")

t = p.read_text()
if "sigSweep" in t:
    sys.exit("already applied.")
if "sigSelfTest" not in t:
    sys.exit("run add_t.py first.")

old_call = "  if (c == 'T' || c == 't') sigSelfTest();"
if old_call not in t:
    sys.exit("ANCHOR NOT FOUND - send me the command dispatcher lines")

t = t.replace(old_call, "  if (c == 'T' || c == 't') sigSweep();", 1)

fn = '''
/* Upload size sweep, triggered by 'T'.

   The same bytes at increasing sizes, each through postBinary. The point is
   to find the largest body this modem transmits without altering it.

       400 -> arrived intact, refused only for not being a JPEG
       401 -> arrived altered, signature no longer matches

   A non-random pattern is used deliberately: if the Pi ever needs to show
   WHERE corruption happens, a predictable payload makes the damaged offset
   obvious, which random bytes would not.
*/
static void sigSweep() {
#if USE_CELL
  const size_t sizes[] = { 512, 2048, 4096, 8192, 12288, 16384, 24576 };
  Serial.println("[sweep] finding the largest body that survives");

  for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
    size_t n = sizes[i];
    uint8_t *buf = (uint8_t *)malloc(n);
    if (!buf) { Serial.printf("[sweep] %6u  no memory\\n", (unsigned)n); continue; }
    for (size_t k = 0; k < n; k++) buf[k] = (uint8_t)(k & 0xFF);

    int code = modem.postBinary(CELL_HOST, "/api/v1/photos?name=sweep.jpg",
                                buf, n, HMAC_SECRET);
    free(buf);

    Serial.printf("[sweep] %6u bytes -> HTTP %d  %s\\n",
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

'''

idx = t.find("static void sigSelfTest()")
if idx < 0:
    sys.exit("could not find sigSelfTest to insert beside")
t = t[:idx] + fn.lstrip("\n") + t[idx:]

shutil.copy(p, p.with_suffix(".cpp.bak_sweep"))
p.write_text(t)
print("added sigSweep, backup at main.cpp.bak_sweep")
