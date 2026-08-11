"""Canopy image gallery, the upload endpoint, and admin housekeeping.

Goes at:  ~/agrisync/src/api/photos.py

VIEWING
    Dashboard "See the field"  ->  api.agrisync.tech/photos
      -> Cloudflare Access shows its own login. Your app never sees an
         unauthenticated visitor and never handles a password.
      -> granted already?   gallery
      -> requested already? "waiting on approval"
      -> neither?           request form, and your phone gets a notification

    Anyone in ADMIN_EMAILS skips all of that.

UPLOADING
    Board B posts each JPEG over UART to Board A, which signs it and pushes it
    through the modem. The image is written to SD first, so a failed upload
    loses nothing.

    The cellular path sends base64. Measured: a raw JPEG through the A7670C's
    AT+HTTPDATA arrives with the same byte count and a valid header but a
    different hash, so the modem substitutes byte values in flight. Base64
    keeps it inside the ASCII range that JSON readings already survive.

ADMIN
    Download everything as a zip, or select and delete. Restricted to
    ADMIN_EMAILS, checked server side on every request - a hidden button is
    not a permission.

WHAT THE GATE CANNOT DO
    Stop someone saving an image they can see. Anything a browser renders can
    be screenshotted. What it buys you is that the folder is not listable, the
    files are not on a public path, and you know who asked and who you
    approved.
"""
import base64
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import (Blueprint, abort, current_app, jsonify, redirect,
                   render_template, request, send_file, send_from_directory,
                   url_for)

from src import notify, visitors, whoami
from src.hmac_auth import verify_signature

bp = Blueprint("photos", __name__)

# Camera filenames look like img_00062_000004.jpg. Anything outside this set is
# refused outright - rejecting beats sanitising a hostile path.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{5,60}$")
ALLOWED_EXT = {".jpg", ".jpeg", ".png"}
MAX_LISTED = 400
MAX_UPLOAD_BYTES = 1_000_000

# A zip of everything is built in memory. At ~30KB an image that is 30MB for a
# thousand images, which a Pi handles, but the cap stops a runaway folder from
# taking the API down with it.
MAX_ZIP_FILES = 1500


def photo_dir() -> Path:
    return Path(current_app.config.get("PHOTO_DIR") or "data/canopy").expanduser()


def _viewer():
    """Verified email, from a Google session or Cloudflare Access.

    whoami tries Google first, so once the Access application is removed this
    keeps working with no change here.
    """
    return whoami.email(request)


def _require_admin():
    """Server-side check on every admin route. Returns the email or aborts."""
    email = _viewer()
    if not email or not visitors.is_admin(email):
        abort(403)
    return email


def list_photos(limit: int = MAX_LISTED):
    """Newest first, by modification time - the camera has no clock and its
    filenames only order within a single boot."""
    d = photo_dir()
    if not d.is_dir():
        return []

    found = []
    for p in d.iterdir():
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
            continue
        if not SAFE_NAME.match(p.name):
            continue
        st = p.stat()
        found.append({
            "name": p.name,
            "bytes": st.st_size,
            "kb": round(st.st_size / 1024, 1),
            "captured_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "shown_at": datetime.fromtimestamp(st.st_mtime).strftime("%d %b %Y, %H:%M"),
        })

    found.sort(key=lambda x: x["captured_at"], reverse=True)
    return found[:limit]


# ---------------------------------------------------------------- viewing

@bp.get("/photos")
def gallery():
    email = _viewer()

    if not email:
        # Fail closed rather than guessing who this is.
        state = "anonymous" if whoami.configured() else "unconfigured"
        return render_template("photos.html", state=state)

    try:
        visitors.record(email, path="/photos")
    except Exception:            # logging must never block a page
        pass

    if visitors.is_granted(email):
        photos = list_photos()
        total_bytes = sum(p["bytes"] for p in photos)
        return render_template(
            "photos.html", state="granted", photos=photos,
            total=len(photos), email=email,
            is_admin=visitors.is_admin(email),
            total_mb=round(total_bytes / 1024 / 1024, 1))

    # Not approved. Send them back to the dashboard rather than showing a
    # bare form on a page with no header and no way back. The modal there
    # opens in the right state - "already on the list" if they have asked,
    # the form if they have not.
    return redirect("https://dashboard.agrisync.tech/?request=photos")


@bp.post("/photos/request")
def ask():
    email = _viewer()
    if not email:
        abort(403)

    note = (request.form.get("note") or "").strip()[:500]

    created = False
    try:
        # scope="photos" so the approval screen shows what they came for,
        # rather than every request looking like a dataset request.
        created, _ = visitors.record_request(email, note=note or None,
                                             scope="photos")
    except Exception:
        current_app.logger.exception("could not record photo access request")

    # Only ping the phone on something new. Someone reloading the page should
    # not buzz you again.
    if created:
        who = visitors.get(email) or {}
        name = who.get("name") or email
        phone = who.get("phone") or "no number"
        notify.push(
            title="AgriSync: field images requested",
            message=f"{name} ({phone}) asked to see the canopy photos.\n{email}"
                    + (f"\n\n{note}" if note else ""),
            tags="camera",
        )

    return redirect(url_for("photos.gallery"))


@bp.get("/photos/img/<name>")
def image(name):
    email = _viewer()
    if not email or not visitors.is_granted(email):
        abort(403)

    if not SAFE_NAME.match(name) or Path(name).suffix.lower() not in ALLOWED_EXT:
        abort(404)

    d = photo_dir()
    if not (d / name).is_file():
        abort(404)

    # send_from_directory refuses to escape the directory, so even if the regex
    # above were wrong this cannot reach into the filesystem.
    resp = send_from_directory(d, name, max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.get("/api/v1/photos")
def api_list():
    email = _viewer()
    if not email or not visitors.is_granted(email):
        return jsonify({"error": "forbidden"}), 403
    photos = list_photos()
    return jsonify({"count": len(photos), "photos": photos})


# ------------------------------------------------------------------ admin

@bp.get("/photos/download-all")
def download_all():
    """Every image as one zip. Admins only."""
    email = _require_admin()

    d = photo_dir()
    files = sorted(
        [p for p in d.iterdir()
         if p.is_file() and p.suffix.lower() in ALLOWED_EXT
         and SAFE_NAME.match(p.name)],
        key=lambda p: p.name,
    ) if d.is_dir() else []

    if not files:
        return jsonify({"error": "nothing to download"}), 404
    if len(files) > MAX_ZIP_FILES:
        return jsonify({"error": f"too many files ({len(files)}), "
                                 f"cap is {MAX_ZIP_FILES}"}), 413

    buf = io.BytesIO()
    # ZIP_STORED, not DEFLATED. JPEGs are already compressed - deflating them
    # again costs Pi CPU and saves a percent or two.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for p in files:
            z.write(p, arcname=p.name)
    buf.seek(0)

    current_app.logger.info("%s downloaded %d images as zip", email, len(files))

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"agrisync-canopy-{stamp}.zip")


@bp.post("/photos/delete")
def delete():
    """Delete selected images. Admins only, and irreversible.

    There is no bin and no undo. The camera's SD card is the other copy, and
    only until it wraps - so this genuinely removes data. The button asks for
    confirmation and every deletion is logged with who did it.
    """
    email = _require_admin()

    names = request.form.getlist("name")
    if not names:
        return redirect(url_for("photos.gallery"))

    d = photo_dir()
    removed, skipped = 0, 0

    for name in names:
        if not SAFE_NAME.match(name) or Path(name).suffix.lower() not in ALLOWED_EXT:
            skipped += 1
            continue
        target = d / name
        # resolve() then compare parents: even a name that slipped the regex
        # cannot point outside the folder.
        try:
            if target.resolve().parent != d.resolve():
                skipped += 1
                continue
            if target.is_file():
                target.unlink()
                removed += 1
        except Exception:
            skipped += 1

    current_app.logger.warning("%s deleted %d image(s), skipped %d",
                               email, removed, skipped)
    return redirect(url_for("photos.gallery"))


# ---------------------------------------------------------------- upload

@bp.post("/api/v1/photos")
def upload():
    """Board A posts a JPEG here.

        POST /api/v1/photos?name=img_00062_000004.jpg&enc=b64
        X-Sig: <unix_ts>.<hmac_sha256 of "<ts>." + raw body>
        body:  base64 of the JPEG (or raw bytes without enc=b64)

    Same signature scheme as sensor readings, so the secret never travels and
    nobody can drop images onto your disk by finding the URL.
    """
    ok, reason = verify_signature()
    if not ok:
        current_app.logger.warning(
            "rejected photo upload from %s: %s (received %d bytes, name=%s)",
            request.headers.get("CF-Connecting-IP", request.remote_addr), reason,
            len(request.get_data()), request.args.get("name"))
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_data()

    # The signature is verified above against the body AS RECEIVED, so any
    # tampering is caught. Decoding happens after that check, never before.
    if request.args.get("enc") == "b64":
        try:
            data = base64.b64decode(data, validate=True)
        except Exception:
            return jsonify({"error": "bad base64"}), 400

    if not data:
        return jsonify({"error": "empty body"}), 400
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "too large"}), 413

    # A JPEG starts FF D8 FF. Cheap check that stops the endpoint being used as
    # general file storage.
    if data[:3] != b"\xff\xd8\xff":
        return jsonify({"error": "not a jpeg"}), 400

    # Query string first: the cellular path can only carry one custom header
    # and the signature has it. The header form is kept for curl testing.
    name = (request.args.get("name")
            or request.headers.get("X-Filename") or "").strip()
    if not SAFE_NAME.match(name) or Path(name).suffix.lower() not in ALLOWED_EXT:
        return jsonify({"error": "bad filename"}), 400

    d = photo_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / name

    # Already have it. The camera retries after a failed upload and should not
    # be punished for it.
    if target.exists():
        return jsonify({"status": "already_here", "name": name}), 200

    # Write to a temporary file and rename. A rename is atomic, so the gallery
    # can never list a half-written image.
    tmp = d / (name + ".part")
    tmp.write_bytes(data)
    tmp.rename(target)

    current_app.logger.info("photo stored: %s (%d bytes)", name, len(data))
    return jsonify({"status": "stored", "name": name, "bytes": len(data)}), 201
