"""Private pairing rooms, lesson records, recordings, and chat."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from account_store import read_accounts

learning = Blueprint("learning", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    mentor_id INTEGER NOT NULL,
    mentee_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    scheduled_at INTEGER NOT NULL,
    planned_minutes INTEGER NOT NULL,
    zoom_url TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    cancelled_at INTEGER,
    recording_filename TEXT,
    recording_original TEXT,
    recording_mimetype TEXT,
    recording_seconds REAL,
    recorded_at INTEGER,
    uploaded_by INTEGER,
    reviewed_at INTEGER,
    reviewed_by INTEGER,
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS lessons_pair ON lessons(mentor_id, mentee_id, scheduled_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mentor_id INTEGER NOT NULL,
    mentee_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_pair ON messages(mentor_id, mentee_id, id);
"""


def get_db():
    if "learning_db" not in g:
        path = Path(current_app.config["LEARNING_DATABASE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        g.learning_db = db
    return g.learning_db


@learning.teardown_app_request
def close_db(error=None):
    db = g.pop("learning_db", None)
    if db is not None:
        db.close()


@learning.before_request
def require_login():
    if g.current_user is None:
        if request.endpoint == "learning.messages":
            return jsonify(error="Please log in again to use chat."), 401
        return redirect(url_for("login"))


@learning.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def users_by_id():
    return {user["id"]: user for user in read_accounts(current_app.config["USERS_FILE"])}


def active_pair(mentor, mentee):
    return bool(mentor and mentee and mentor.get("role") == "mentor" and mentee.get("role") == "mentee"
                and str(mentee["id"]) in {str(id) for id in mentor.get("partners", [])}
                and str(mentor["id"]) in {str(id) for id in mentee.get("partners", [])})


def room_access(mentor_id, mentee_id, manage=False, chat=False):
    users = users_by_id()
    mentor, mentee = users.get(mentor_id), users.get(mentee_id)
    if not mentor or not mentee or mentor.get("role") != "mentor" or mentee.get("role") != "mentee":
        abort(404)
    coordinator = g.current_user.get("is_coordinator") is True
    participant = g.current_user["id"] in {mentor_id, mentee_id}
    active = active_pair(mentor, mentee)
    if not coordinator and (not active or not participant):
        abort(403, description="This pairing room is only available to its linked participants and coordinators.")
    if (manage or chat) and not active:
        abort(403, description="This pairing is inactive. Ask a coordinator to link the accounts again.")
    can_manage = active and (coordinator or g.current_user["id"] == mentor_id or (
        current_app.config["MENTEES_MANAGE_LESSONS"] and participant))
    if manage and not can_manage:
        abort(403, description="Only the mentor or a coordinator can manage this lesson.")
    if chat and not participant:
        abort(403, description="Coordinators can review this chat. Only the paired participants can send messages.")
    return {"mentor": mentor, "mentee": mentee, "active": active,
            "can_manage": can_manage, "can_chat": active and participant}


def lesson_access(lesson_id, manage=False):
    row = get_db().execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if row is None:
        abort(404)
    return dict(row), room_access(row["mentor_id"], row["mentee_id"], manage=manage)


def local_datetime(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo(current_app.config["LESSON_TIMEZONE"]))


def present_lesson(row, users=None):
    lesson = dict(row)
    users = users or users_by_id()
    lesson["mentor"] = users.get(lesson["mentor_id"], {"name": ["Former mentor"]})
    lesson["mentee"] = users.get(lesson["mentee_id"], {"name": ["Former mentee"]})
    lesson["active_pair"] = active_pair(users.get(lesson["mentor_id"]), users.get(lesson["mentee_id"]))
    lesson["start_label"] = local_datetime(lesson["scheduled_at"]).strftime("%a, %d %b %Y · %H:%M")
    lesson["datetime_input"] = local_datetime(lesson["scheduled_at"]).strftime("%Y-%m-%dT%H:%M")
    lesson["duration_label"] = f'{(lesson["recording_seconds"] or 0) / 60:.1f} min'
    now = time.time()
    if lesson["cancelled_at"]:
        lesson["status"] = "Cancelled"
    elif lesson["recorded_at"]:
        lesson["status"] = "Attendance recorded"
    elif lesson["scheduled_at"] > now:
        lesson["status"] = "Upcoming"
    elif lesson["scheduled_at"] + lesson["planned_minutes"] * 60 > now:
        lesson["status"] = "In progress"
    else:
        lesson["status"] = "Awaiting recording"
    lesson["can_upload"] = not lesson["cancelled_at"] and lesson["scheduled_at"] <= now
    return lesson


def activity_for_user(user_id, users=None):
    users = users or users_by_id()
    rows = get_db().execute("SELECT * FROM lessons WHERE mentor_id = ? OR mentee_id = ? ORDER BY scheduled_at",
                            (user_id, user_id)).fetchall()
    completed = [row for row in rows if row["recorded_at"] and not row["cancelled_at"]]
    upcoming = [present_lesson(row, users) for row in rows if not row["cancelled_at"] and not row["recorded_at"]
                and row["scheduled_at"] + row["planned_minutes"] * 60 > time.time()
                and active_pair(users.get(row["mentor_id"]), users.get(row["mentee_id"]))]
    return {"hours": sum(row["recording_seconds"] or 0 for row in completed) / 3600,
            "completed": len(completed), "upcoming": upcoming}


def room_summary(mentor_id, mentee_id):
    row = get_db().execute("""SELECT COUNT(*) AS completed, COALESCE(SUM(recording_seconds), 0) AS seconds
        FROM lessons WHERE mentor_id = ? AND mentee_id = ? AND recorded_at IS NOT NULL AND cancelled_at IS NULL""",
        (mentor_id, mentee_id)).fetchone()
    return {"completed": row["completed"], "hours": row["seconds"] / 3600}


@learning.get('/pairings/<int:mentor_id>/<int:mentee_id>')
def room(mentor_id, mentee_id):
    pair = room_access(mentor_id, mentee_id)
    users = users_by_id()
    lessons = [present_lesson(row, users) for row in get_db().execute(
        "SELECT * FROM lessons WHERE mentor_id = ? AND mentee_id = ? ORDER BY scheduled_at DESC", (mentor_id, mentee_id))]
    recordings = [lesson for lesson in lessons if lesson["recorded_at"]]
    pending = sorted([lesson for lesson in lessons if not lesson["recorded_at"] and not lesson["cancelled_at"]],
                     key=lambda lesson: lesson["scheduled_at"])
    cancelled = [lesson for lesson in lessons if lesson["cancelled_at"]]
    return render_template("room.html", pair=pair, summary=room_summary(mentor_id, mentee_id),
                           recordings=recordings, pending=pending, cancelled=cancelled,
                           messages=message_rows(mentor_id, mentee_id), timezone=current_app.config["LESSON_TIMEZONE"],
                           max_upload_mb=current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
                           now_input=local_datetime(time.time()).strftime("%Y-%m-%dT%H:%M"))


def parse_lesson_form():
    title = request.form.get("title", "").strip()
    if not title or len(title) > 120:
        abort(400, description="Enter a lesson title of up to 120 characters.")
    try:
        date = datetime.fromisoformat(request.form.get("scheduled_at", ""))
        if date.tzinfo is not None or not 2000 <= date.year <= 2100:
            raise ValueError()
        scheduled_at = int(date.replace(tzinfo=ZoneInfo(current_app.config["LESSON_TIMEZONE"])).timestamp())
        duration = int(request.form.get("planned_minutes", ""))
        if not 15 <= duration <= 240:
            raise ValueError()
    except (ValueError, OverflowError):
        abort(400, description="Choose a valid lesson date and a duration between 15 and 240 minutes.")
    zoom_url = request.form.get("zoom_url", "").strip()
    try:
        parsed = urlsplit(zoom_url)
        host = (parsed.hostname or "").lower()
        valid_host = any(host == domain or host.endswith("." + domain) for domain in ["zoom.us", "zoom.com"])
        if (parsed.scheme != "https" or not valid_host or parsed.username or parsed.password
                or parsed.port not in {None, 443} or len(zoom_url) > 2000 or any(c.isspace() for c in zoom_url)):
            raise ValueError()
    except ValueError:
        abort(400, description="Paste a valid HTTPS Zoom meeting link, such as https://zoom.us/j/123456789.")
    return title, scheduled_at, duration, zoom_url


def reject_conflicts(db, mentor_id, mentee_id, start, minutes, exclude_id=""):
    users = users_by_id()
    conflicts = db.execute("""SELECT * FROM lessons WHERE cancelled_at IS NULL AND id != ?
        AND (mentor_id IN (?, ?) OR mentee_id IN (?, ?))
        AND scheduled_at < ? AND scheduled_at + planned_minutes * 60 > ?""",
        (exclude_id, mentor_id, mentee_id, mentor_id, mentee_id, start + minutes * 60, start))
    if any(active_pair(users.get(row["mentor_id"]), users.get(row["mentee_id"])) for row in conflicts):
        abort(409, description="One of these participants already has a lesson at this time. Choose another time.")


@learning.post('/pairings/<int:mentor_id>/<int:mentee_id>/lessons')
def create_lesson(mentor_id, mentee_id):
    room_access(mentor_id, mentee_id, manage=True)
    title, start, minutes, zoom_url = parse_lesson_form()
    db = get_db()
    with db:
        db.execute("BEGIN IMMEDIATE")
        reject_conflicts(db, mentor_id, mentee_id, start, minutes)
        db.execute("""INSERT INTO lessons(id, mentor_id, mentee_id, title, scheduled_at, planned_minutes,
                   zoom_url, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (uuid4().hex, mentor_id, mentee_id, title, start, minutes, zoom_url, g.current_user["id"], int(time.time())))
    flash("Lesson added. Both participants can see it on their dashboards.", "success")
    return redirect(url_for("learning.room", mentor_id=mentor_id, mentee_id=mentee_id, _anchor="lessons"))


@learning.post('/lessons/<lesson_id>/edit')
def edit_lesson(lesson_id):
    lesson, _ = lesson_access(lesson_id, manage=True)
    values = parse_lesson_form()
    db = get_db()
    with db:
        db.execute("BEGIN IMMEDIATE")
        fresh = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        if fresh["recorded_at"] or fresh["cancelled_at"]:
            abort(409, description="Completed or cancelled lessons cannot be rescheduled.")
        reject_conflicts(db, lesson["mentor_id"], lesson["mentee_id"], values[1], values[2], lesson_id)
        db.execute("UPDATE lessons SET title = ?, scheduled_at = ?, planned_minutes = ?, zoom_url = ? WHERE id = ?",
                   (*values, lesson_id))
    flash("Lesson updated for both participants.", "success")
    return redirect(url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="lessons"))


@learning.post('/lessons/<lesson_id>/cancel')
def cancel_lesson(lesson_id):
    lesson, _ = lesson_access(lesson_id, manage=True)
    with get_db() as db:
        result = db.execute("UPDATE lessons SET cancelled_at = ? WHERE id = ? AND recorded_at IS NULL",
                            (int(time.time()), lesson_id))
        if not result.rowcount:
            abort(409, description="A lesson with recorded attendance cannot be cancelled.")
    flash("Lesson cancelled for both participants.", "success")
    return redirect(url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="lessons"))


def probe_video(path, extension):
    with path.open("rb") as source:
        header = source.read(12)
    if (extension == ".mp4" and header[4:8] != b"ftyp") or (extension == ".webm" and header[:4] != b"\x1aE\xdf\xa3"):
        abort(400, description="This file is not a supported MP4 or WebM video.")
    try:
        process = subprocess.run([current_app.config["FFPROBE_BIN"], "-v", "error", "-protocol_whitelist", "file,pipe",
                                  "-show_entries", "stream=codec_type,codec_name:format=duration,format_name", "-of", "json", str(path)],
                                 capture_output=True, text=True, timeout=30, check=True)
        metadata = json.loads(process.stdout)
        duration = float(metadata["format"]["duration"])
        codecs = {stream.get("codec_name") for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"}
        allowed = {"h264", "av1"} if extension == ".mp4" else {"vp8", "vp9", "av1"}
        if not codecs or not codecs.issubset(allowed) or not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return duration
    except FileNotFoundError:
        abort(503, description="Video uploads are temporarily unavailable: the server needs FFmpeg's ffprobe installed.")
    except (ValueError, KeyError, subprocess.SubprocessError):
        abort(400, description="We could not read this video. Upload an MP4 with H.264 video or a WebM recording.")


@learning.post('/lessons/<lesson_id>/recording')
def upload_recording(lesson_id):
    lesson, _ = lesson_access(lesson_id, manage=True)
    if lesson["cancelled_at"] or lesson["scheduled_at"] > time.time():
        abort(409, description="Upload a recording after the lesson starts. Cancelled lessons cannot receive recordings.")
    upload = request.files.get("recording")
    if upload is None or not upload.filename:
        abort(400, description="Choose a recording to upload.")
    original = secure_filename(upload.filename)
    extension = Path(original).suffix.lower()
    if extension not in {".mp4", ".webm"}:
        abort(400, description="Choose an MP4 or WebM video recording.")
    directory = Path(current_app.config["RECORDINGS_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    filename = uuid4().hex + extension
    path = directory / filename
    committed = False
    previous = None
    try:
        upload.save(path)
        duration = probe_video(path, extension)
        db = get_db()
        with db:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
            room_access(fresh["mentor_id"], fresh["mentee_id"], manage=True)
            if fresh["cancelled_at"] or fresh["scheduled_at"] > time.time():
                abort(409, description="This lesson changed while you were uploading. Reload the room and try again.")
            previous = fresh["recording_filename"]
            db.execute("""UPDATE lessons SET recording_filename = ?, recording_original = ?, recording_mimetype = ?,
                recording_seconds = ?, recorded_at = ?, uploaded_by = ?, reviewed_at = NULL, reviewed_by = NULL,
                review_note = NULL WHERE id = ?""",
                (filename, original, "video/mp4" if extension == ".mp4" else "video/webm", duration,
                 int(time.time()), g.current_user["id"], lesson_id))
        committed = True
    finally:
        if not committed:
            path.unlink(missing_ok=True)
    if previous:
        (directory / previous).unlink(missing_ok=True)
    flash("Recording uploaded. Attendance and learning hours have been updated for both participants.", "success")
    destination = url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="recordings")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(redirect=destination)
    return redirect(destination)


@learning.get('/lessons/<lesson_id>/recording')
def recording(lesson_id):
    lesson, _ = lesson_access(lesson_id)
    if not lesson["recording_filename"]:
        abort(404)
    path = Path(current_app.config["RECORDINGS_DIR"]) / lesson["recording_filename"]
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype=lesson["recording_mimetype"], download_name=lesson["recording_original"],
                     conditional=True, as_attachment=request.args.get("download") == "1")


def message_rows(mentor_id, mentee_id, after=None, before=None):
    params = [mentor_id, mentee_id]
    query = "SELECT * FROM messages WHERE mentor_id = ? AND mentee_id = ?"
    if after is not None:
        query += " AND id > ?"
        params.append(after)
    if before is not None:
        query += " AND id < ?"
        params.append(before)
    query += " ORDER BY id " + ("ASC" if after is not None else "DESC") + " LIMIT 50"
    rows = list(get_db().execute(query, params))
    if after is None:
        rows.reverse()
    users = users_by_id()
    return [{"id": row["id"], "sender_id": row["sender_id"], "body": row["body"],
             "sender": " ".join(users.get(row["sender_id"], {}).get("name", [])) or "Former participant",
             "time": local_datetime(row["created_at"]).strftime("%d %b %Y, %H:%M")}
            for row in rows]


@learning.route('/pairings/<int:mentor_id>/<int:mentee_id>/messages', methods=['GET', 'POST'])
def messages(mentor_id, mentee_id):
    room_access(mentor_id, mentee_id, chat=request.method == "POST")
    if request.method == "POST":
        body = request.form.get("message", "").strip()
        if not body or len(body) > 2000:
            return jsonify(error="Write a message between 1 and 2,000 characters."), 400
        with get_db() as db:
            cursor = db.execute("INSERT INTO messages(mentor_id, mentee_id, sender_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
                                (mentor_id, mentee_id, g.current_user["id"], body, int(time.time())))
            id = cursor.lastrowid
        return jsonify(messages=message_rows(mentor_id, mentee_id, after=id - 1))
    try:
        after = int(request.args["after"]) if "after" in request.args else None
        before = int(request.args["before"]) if "before" in request.args else None
        if any(value is not None and (value < 0 or value > 2**63 - 1) for value in [after, before]):
            raise ValueError()
    except ValueError:
        return jsonify(error="Invalid message cursor."), 400
    return jsonify(messages=message_rows(mentor_id, mentee_id, after=after, before=before))


@learning.get('/coordinator/lessons')
def monitor():
    if g.current_user.get("is_coordinator") is not True:
        abort(403, description="This page is available to coordinators only.")
    users = users_by_id()
    lessons = [present_lesson(row, users) for row in get_db().execute("SELECT * FROM lessons ORDER BY scheduled_at DESC")]
    people = [{"user": user, **activity_for_user(user["id"], users)} for user in users.values()
              if user.get("role") in {"mentor", "mentee"}]
    return render_template("lesson_monitor.html", lessons=lessons, people=people,
                           pending_reviews=sum(bool(row["recorded_at"] and not row["reviewed_at"]) for row in lessons),
                           timezone=current_app.config["LESSON_TIMEZONE"])


@learning.post('/lessons/<lesson_id>/review')
def review_recording(lesson_id):
    if g.current_user.get("is_coordinator") is not True:
        abort(403, description="Only coordinators can review recordings.")
    lesson, _ = lesson_access(lesson_id)
    note = request.form.get("review_note", "").strip()
    if len(note) > 2000:
        abort(400, description="Keep review notes within 2,000 characters.")
    with get_db() as db:
        result = db.execute("""UPDATE lessons SET reviewed_at = ?, reviewed_by = ?, review_note = ?
            WHERE id = ? AND recording_filename = ?""", (int(time.time()), g.current_user["id"], note,
                                                        lesson_id, request.form.get("recording_version")))
        if not result.rowcount:
            abort(409, description="The recording has changed or is missing. Reload it before saving a review.")
    flash("Recording review saved. Review notes are visible only to coordinators.", "success")
    return redirect(url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="recordings"))
