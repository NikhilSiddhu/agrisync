#!/usr/bin/env python3
"""Add a 'T' serial command that tests the signed-upload path without the camera.

    cd ~/agrisync && python3 add_t.py

WHY
    Two faults are tangled: the camera browns out mid-transfer, and signed
    photo uploads come back 401. Neither can be diagnosed while the other is
    active, because every attempt needs a complete frame from a board that
    keeps resetting.

    This sends a tiny fixed payload through the exact same postBinary path.
    No camera, no UART, no SD card - just the signing and the modem.

READING THE RESULT
    401  signature is genuinely wrong. A real bug in signing or transport,
         and now reproducible on demand in two seconds.

    400  signature VERIFIED. The server got past auth and rejected the body
         for not being a JPEG, which is exactly right for 8 bytes of "AGRISYNC".
         That would mean signing works and the 401s were the camera corrupting
         the buffer after Board A had already hashed it.

    413/201  also fine - anything other than 401 means auth passed.
"""
import ast, shutil, sys
from pathlib import Path

p = Path.home() / "agrisync/firmware/synthetic_wifi/src/main.cpp"
if not p.is_file():
    sys.exit(f"cannot find {p}")

t = p.read_text()
if "'T' ||" in t or "sigSelfTest" in t:
    sys.exit("already applied.")

anchor = "  if (c == 'D' || c == 'd') dumpBuffer();"
if anchor not in t:
    sys.exit("ANCHOR NOT FOUND - send me lines 525-545 of main.cpp")

new = """  if (c == 'T' || c == 't') sigSelfTest();
  else if (c == 'D' || c == 'd') dumpBuffer();"""
t = t.replace(anchor, new, 1)

# The function goes immediately above whatever contains the dispatcher.
fn = '''
/* Signed-upload self test, triggered by 'T' on the serial console.

   Sends eight bytes through postBinary - the same signing, the same base64,
   the same modem path as a photograph, minus the camera and the UART.

   The server checks the signature BEFORE it checks the JPEG header, so the
   response tells you which half works:
       401 -> signature rejected, the bug is real and now reproducible
       400 -> signature accepted, body rejected for not being a JPEG. Signing
              is fine and the fault is upstream, in the camera or the link.
*/
static void sigSelfTest() {
#if USE_CELL
  const uint8_t probe[] = "AGRISYNC";
  Serial.println("[test] signed upload, 8 bytes");
  int code = modem.postBinary(CELL_HOST,
                              "/api/v1/photos?name=selftest.jpg",
                              probe, sizeof(probe) - 1, HMAC_SECRET);
  Serial.printf("[test] HTTP %d  %s\\n", code,
                code == 401 ? "signature REJECTED - the bug is real"
              : code == 400 ? "signature OK (body correctly refused as not a JPEG)"
              : code == 201 ? "accepted"
                            : "see code");
#else
  Serial.println("[test] cellular build only");
#endif
}

'''

# Insert before the function that owns the dispatcher.
idx = t.find(new)
start = t.rfind("\nstatic void ", 0, idx)
if start < 0:
    start = t.rfind("\nvoid ", 0, idx)
if start < 0:
    sys.exit("could not find a place to insert - send me lines 500-545")
t = t[:start + 1] + fn + t[start + 1:]

shutil.copy(p, p.with_suffix(".cpp.bak_t"))
p.write_text(t)
print("added 'T' command, backup at main.cpp.bak_t")
