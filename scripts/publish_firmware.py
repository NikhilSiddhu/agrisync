"""Offer a firmware image to the box over 4G (firmware 1.6.0 and later).

    python3 scripts/publish_firmware.py firmware.bin --version 1.6.1
    python3 scripts/publish_firmware.py --withdraw
    python3 scripts/publish_firmware.py --status

Then set OTA_ENABLED=1 in .env and restart agrisync-api. The next reply to a
reading from the box carries the offer, signed; the box downloads the image
between readings, checks its SHA-256, installs it and reboots into it. If the
new image cannot get a reading through within three boots, the box goes back
to the old one by itself and reports it.

BUILD THE IMAGE WITHOUT SECRETS. From 1.6.0 the box keeps its key in flash,
written there by the cable-flashed build. An image for the air is built with
the key empty:

    cd firmware/synthetic_wifi
    DEVICE_HMAC_SECRET= pio run
    python3 ../../scripts/publish_firmware.py .pio/build/esp32dev/firmware.bin --version 1.6.1

The download crosses the network in plain text, so this script refuses any
image that contains the device secret (or the Wi-Fi password, if .env has
one). It also refuses anything that is not an ESP32 app image, or is too big
for the box's update slot.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from src import device_link  # noqa: E402

# The default esp32dev partition table: two 1.25 MB app slots.
SLOT_BYTES = 0x140000
ESP_IMAGE_MAGIC = 0xE9


def secrets_in(data: bytes):
    """Names of the secrets found verbatim in the image."""
    found = []
    for name in ("DEVICE_HMAC_SECRET", "WIFI_PASSWORD", "DEVICE_API_KEY"):
        value = os.getenv(name, "")
        if len(value) >= 8 and value.encode() in data:
            found.append(name)
    return found


def check_image(data: bytes):
    """Problems that make the image unsafe or unusable. Empty when fine."""
    problems = []
    if not data or data[0] != ESP_IMAGE_MAGIC:
        problems.append("not an ESP32 app image (first byte is not 0xE9)")
    if len(data) > SLOT_BYTES:
        problems.append(f"{len(data)} bytes does not fit the {SLOT_BYTES}-byte update slot")
    leaked = secrets_in(data)
    if leaked:
        problems.append("contains " + ", ".join(leaked) + ": rebuild with the secret empty")
    return problems


def publish(src: Path, version: str, out_dir: Path):
    if device_link.parse_version(version) is None:
        raise SystemExit(f"publish_firmware: {version!r} is not a version like 1.6.1")
    data = src.read_bytes()
    problems = check_image(data)
    if problems:
        raise SystemExit("publish_firmware: refused:\n  - " + "\n  - ".join(problems))
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"agrisync-{version}.bin"
    shutil.copyfile(src, out_dir / name)
    manifest = {
        "version": version,
        "file": name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    tmp = out_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.replace(out_dir / "manifest.json")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", type=Path)
    ap.add_argument("--version")
    ap.add_argument("--withdraw", action="store_true", help="stop offering any image")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    out_dir = device_link.firmware_dir()

    if args.status:
        m = device_link.manifest()
        print(json.dumps(m, indent=2) if m else "nothing on offer")
        print("OTA_ENABLED =", os.getenv("OTA_ENABLED", "0"))
        return 0
    if args.withdraw:
        (out_dir / "manifest.json").unlink(missing_ok=True)
        print("withdrawn: the box will not be offered an update")
        return 0
    if not args.image or not args.version:
        ap.error("give the image and --version, or --withdraw, or --status")
    m = publish(args.image, args.version, out_dir)
    print(f"on offer: {m['version']}, {m['size']} bytes, sha256 {m['sha256'][:16]}...")
    if os.getenv("OTA_ENABLED", "0") != "1":
        print("OTA_ENABLED is not 1 in .env: set it and restart agrisync-api to start offering it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
