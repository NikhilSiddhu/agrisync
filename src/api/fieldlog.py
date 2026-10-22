"""Field log: what the plants actually did. Admin only.

The readings say what the weather was. This says what happened in the crop:
one row per walk through the field, with what was looked at and what was
found. It is the other half of the dataset. Without it the disease scores
can never be checked, let alone calibrated, so every visit is worth a row,
including the ones where nothing was wrong.
"""
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request

from src import access, whoami
from src.db import get_db

bp = Blueprint("fieldlog", __name__)

DISEASES = [
    ("none", "Nothing seen"),
    ("alternaria", "Alternaria leaf spot"),
    ("downy_mildew", "Downy mildew"),
    ("black_rot", "Black rot"),
    ("other", "Something else (say what in the notes)"),
]
STAGES = ["nursery", "transplanted", "vegetative", "head forming", "harvest"]


def _admin():
    ident = whoami.current(request)
    if not access.is_admin(ident):
        return None, (jsonify({"error": "forbidden",
                               "detail": "the field log is for the station's admins"}), 403)
    return ident, None


def _int(value, lo, hi):
    try:
        v = int(str(value).strip() or 0)
    except ValueError:
        return None
    return v if lo <= v <= hi else None


@bp.get("/admin/field-log")
def page():
    ident, err = _admin()
    if err:
        return err
    rows = list(get_db().field_log.find({}, {"_id": 0}).sort([("date", -1), ("recorded_at", -1)]).limit(300))
    return render_template("field_log.html", rows=rows, diseases=DISEASES, stages=STAGES,
                           labels=dict(DISEASES), today=date.today().isoformat(),
                           identity=ident, saved=request.args.get("saved") == "1",
                           error=request.args.get("error"))


@bp.post("/admin/field-log")
def add():
    ident, err = _admin()
    if err:
        return err
    f = request.form
    try:
        day = date.fromisoformat((f.get("date") or "").strip())
    except ValueError:
        return redirect("/admin/field-log?error=date", code=303)
    if day > date.today():
        return redirect("/admin/field-log?error=future", code=303)

    disease = f.get("disease", "none")
    stage = f.get("stage", "")
    checked = _int(f.get("plants_checked"), 0, 100000)
    affected = _int(f.get("plants_affected"), 0, 100000)
    severity = _int(f.get("severity"), 0, 5)
    if disease not in dict(DISEASES) or stage not in STAGES or None in (checked, affected, severity):
        return redirect("/admin/field-log?error=values", code=303)
    if affected > checked:
        return redirect("/admin/field-log?error=affected", code=303)
    if disease == "none":
        affected, severity = 0, 0

    get_db().field_log.insert_one({
        "date": day.isoformat(),
        "stage": stage,
        "disease": disease,
        "plants_checked": checked,
        "plants_affected": affected,
        "severity": severity,
        "notes": (f.get("notes") or "").strip()[:500],
        "recorded_by": (ident or {}).get("email"),
        "recorded_at": datetime.now(timezone.utc),
    })
    return redirect("/admin/field-log?saved=1", code=303)
