"""Firmware updates over 4G (firmware 1.6.0 and later).

The box learns that an update is on offer from the signed config in the reply
to a reading (src/device_link.py): the version, its size and its SHA-256.
It then fetches the image here, a piece at a time, between readings:

    GET /api/v1/firmware/chunk?v=1.6.1&off=0&len=3072      -> base64 text

Base64 because this modem has been measured altering raw bytes in flight
(see modem.h); text arrives intact. Each request is signed over its path,
so only the box can fetch, and a captured signature cannot fetch anything
else. The image itself is checked on the box against the SHA-256 that came
in the signed config, so a piece altered on the way is caught before the box
ever boots it.

The images published here are built WITHOUT secrets (the box keeps its key in
flash from 1.6.0 on), and scripts/publish_firmware.py refuses a binary that
contains the device secret. Over plain HTTP anyone on the path can read what
is downloaded; that must never include the key.
"""
import base64

from flask import Blueprint, Response, jsonify, request

from src import device_link
from src.hmac_auth import require_signed_get

bp = Blueprint("firmware", __name__, url_prefix="/api/v1/firmware")

# Three thousand bytes of image is four thousand of base64: the most this
# modem has carried cleanly in one go (modem.h).
MAX_CHUNK = 3072


@bp.get("/manifest")
@require_signed_get
def get_manifest():
    m = device_link.manifest()
    if not m:
        return jsonify({"error": "no firmware published"}), 404
    return jsonify({k: m[k] for k in ("version", "size", "sha256")})


@bp.get("/chunk")
@require_signed_get
def get_chunk():
    version = request.args.get("v", "")
    try:
        off = int(request.args.get("off", "-1"))
        length = int(request.args.get("len", "0"))
    except ValueError:
        return jsonify({"error": "off and len must be whole numbers"}), 400
    path = device_link.firmware_path(version)
    if path is None:
        return jsonify({"error": "that version is not on offer"}), 404
    size = path.stat().st_size
    if off < 0 or off >= size or length <= 0 or length > MAX_CHUNK:
        return jsonify({"error": "range out of bounds"}), 416
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read(min(length, size - off))
    return Response(base64.b64encode(data), mimetype="text/plain",
                    headers={"Cache-Control": "no-store"})
