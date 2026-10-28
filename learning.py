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
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from account_store import read_accounts, mentor_is_approved

learning = Blueprint("learning", __name__)

MISSING_RECORDING_REASONS = {
    "forgot_to_record": "Forgot to record",
    "lesson_did_not_take_place": "Lesson did not take place",
    "recording_failed": "Recording failed due to a technical issue",
    "recording_lost": "Recording was lost or corrupted",
    "upload_problem": "Unable to upload due to an internet or upload issue",
    "other": "Other",
}

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
    review_submitted_by INTEGER,
    reviewer_name TEXT,
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS lessons_pair ON lessons(mentor_id, mentee_id, scheduled_at);
CREATE TABLE IF NOT EXISTS missing_recording_reports (
    lesson_id TEXT PRIMARY KEY REFERENCES lessons(id),
    mentor_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    submitted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS missing_recording_reports_mentor ON missing_recording_reports(mentor_id);
CREATE TABLE IF NOT EXISTS resource_materials (
    id TEXT PRIMARY KEY,
    folder TEXT NOT NULL,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    uploaded_by INTEGER NOT NULL,
    uploaded_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
    reviewed_by INTEGER,
    reviewed_at INTEGER,
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS resource_materials_folder ON resource_materials(folder, status, uploaded_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mentor_id INTEGER NOT NULL,
    mentee_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_pair ON messages(mentor_id, mentee_id, id);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES lessons(id),
    mentor_id INTEGER NOT NULL,
    mentee_id INTEGER NOT NULL,
    r2_object_key TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    uploaded_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'awaiting_review'
        CHECK(review_status IN ('awaiting_review', 'reviewed', 'flagged')),
    reviewed_by INTEGER,
    reviewed_at INTEGER,
    review_submitted_by INTEGER,
    reviewer_name TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    safeguarding_flag INTEGER NOT NULL DEFAULT 0,
    flagged_at INTEGER,
    flagged_by INTEGER,
    safeguarding_note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS recordings_active ON recordings(session_id) WHERE active = 1;
CREATE TABLE IF NOT EXISTS recording_uploads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES lessons(id),
    mentor_id INTEGER NOT NULL,
    r2_object_key TEXT NOT NULL UNIQUE,
    multipart_id TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    baseline TEXT NOT NULL,
    consent_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'completed', 'aborted')),
    processing_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS recording_uploads_active ON recording_uploads(session_id)
    WHERE status IN ('pending', 'processing');
"""

LESSON_SELECT = """SELECT lessons.*, reports.reason AS missing_recording_reason,
    reports.note AS missing_recording_note, reports.submitted_at AS missing_recording_submitted_at
    FROM lessons LEFT JOIN missing_recording_reports AS reports ON reports.lesson_id = lessons.id"""


def get_db():
    if "learning_db" not in g:
        path = Path(current_app.config["LEARNING_DATABASE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        # Upgrade existing databases without changing saved reviews or lesson records.
        review_columns = {'review_submitted_by': 'INTEGER', 'reviewer_name': 'TEXT'}
        tables = ('lessons', 'recordings')
        if any(not review_columns.keys() <= {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
               for table in tables):
            with db:
                db.execute('BEGIN IMMEDIATE')
                for table in tables:
                    columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                    for column, kind in review_columns.items():
                        if column not in columns:
                            db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
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
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def users_by_id():
    return {user["id"]: user for user in read_accounts(current_app.config["USERS_FILE"])}


def active_pair(mentor, mentee):
    return bool(mentor_is_approved(mentor) and mentee and mentee.get("role") == "mentee"
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
    if manage and coordinator:
        abort(403, description="Admins have read-only access to lesson management.")
    can_manage = active and not coordinator and (g.current_user["id"] == mentor_id or (
        current_app.config["MENTEES_MANAGE_LESSONS"] and participant))
    if manage and not can_manage:
        abort(403, description="Only the assigned mentor can manage this lesson.")
    if chat and not participant:
        abort(403, description="Coordinators can review this chat. Only the paired participants can send messages.")
    return {"mentor": mentor, "mentee": mentee, "active": active,
            "can_manage": can_manage, "can_chat": active and participant,
            "read_only": coordinator and not (active and participant)}


def lesson_access(lesson_id, manage=False):
    row = get_db().execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if row is None:
        abort(404)
    return dict(row), room_access(row["mentor_id"], row["mentee_id"], manage=manage)


def local_datetime(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo(current_app.config["LESSON_TIMEZONE"]))


def admin_reviewers(users=None):
    users = users_by_id() if users is None else users
    return sorted((user for user in users.values() if user.get('is_coordinator') is True),
                  key=lambda user: (' '.join(user.get('name', [])).casefold(), user['username']))


def selected_reviewer():
    """An admin may attribute the final review to another current admin."""
    try:
        reviewer_id = int(request.form.get('reviewed_by', g.current_user['id']))
    except (ValueError, TypeError):
        abort(400, description='Choose an admin reviewer.')
    reviewer = users_by_id().get(reviewer_id)
    if not reviewer or reviewer.get('is_coordinator') is not True:
        abort(400, description='Choose an admin reviewer.')
    return reviewer_id, ' '.join(reviewer.get('name', [])) or reviewer['username']


def public_review(row, users=None):
    row = dict(row)
    reviewed_at = row.get('reviewed_at')
    name = row.get('reviewer_name')
    if reviewed_at and not name:
        users = users_by_id() if users is None else users
        reviewer = users.get(row.get('reviewed_by'), {})
        name = ' '.join(reviewer.get('name', [])) or reviewer.get('username', 'Former admin')
    return {'reviewed_at': reviewed_at, 'reviewed_by': row.get('reviewed_by'),
            'reviewer_name': name or '',
            'reviewed_label': local_datetime(reviewed_at).strftime('%d %b %Y · %H:%M') if reviewed_at else '',
            'review_note': (row.get('review_note') or '') if reviewed_at else ''}


def present_lesson(row, users=None, now=None):
    lesson = dict(row)
    users = users or users_by_id()
    lesson["mentor"] = users.get(lesson["mentor_id"], {"name": ["Former mentor"]})
    lesson["mentee"] = users.get(lesson["mentee_id"], {"name": ["Former mentee"]})
    lesson["active_pair"] = active_pair(users.get(lesson["mentor_id"]), users.get(lesson["mentee_id"]))
    lesson["start_label"] = local_datetime(lesson["scheduled_at"]).strftime("%a, %d %b %Y · %H:%M")
    lesson["datetime_input"] = local_datetime(lesson["scheduled_at"]).strftime("%Y-%m-%dT%H:%M")
    lesson["duration_label"] = f'{(lesson["recording_seconds"] or 0) / 60:.1f} min'
    now = time.time() if now is None else now
    lesson["finished"] = not lesson["cancelled_at"] and bool(
        lesson["recorded_at"] or lesson["scheduled_at"] + lesson["planned_minutes"] * 60 <= now)
    if lesson["cancelled_at"]:
        lesson["status"] = "Cancelled"
    elif lesson["recorded_at"]:
        lesson["status"] = "Attendance recorded"
    elif lesson["scheduled_at"] > now:
        lesson["status"] = "Upcoming"
    elif lesson["scheduled_at"] + lesson["planned_minutes"] * 60 > now:
        lesson["status"] = "In progress"
    else:
        lesson["status"] = "Awaiting upload"
    lesson["can_upload"] = not lesson["cancelled_at"] and not lesson['reviewed_at'] and lesson["scheduled_at"] <= now
    lesson["can_edit"] = not lesson["cancelled_at"] and not lesson["recorded_at"] and lesson["scheduled_at"] > now
    deadline = lesson["scheduled_at"] + lesson["planned_minutes"] * 60 + 7 * 24 * 3600
    lesson["upload_deadline_label"] = local_datetime(deadline).strftime("%d %b %Y · %H:%M")
    lesson["upload_overdue"] = not lesson["recorded_at"] and not lesson["cancelled_at"] and now > deadline
    lesson["missing_recording_label"] = MISSING_RECORDING_REASONS.get(lesson.get("missing_recording_reason"), "")
    from recordings import recording_for_lesson, present_recording
    recording = recording_for_lesson(lesson['id'])
    lesson['cloud_recording'] = present_recording(recording, now=now, users=users) if recording else None
    lesson['recording_available'] = bool(lesson['cloud_recording']['available'] if recording else lesson['recording_filename'])
    lesson.update(public_review(recording or lesson, users))
    return lesson

def recording_report_summaries():
    """One durable report per lesson, attributed to its mentor across all pairings."""
    summaries = {}
    rows = get_db().execute("""SELECT reports.*, lessons.title, lessons.scheduled_at, lessons.recorded_at
        FROM missing_recording_reports AS reports JOIN lessons ON lessons.id = reports.lesson_id
        ORDER BY reports.submitted_at DESC, reports.rowid DESC""")
    for row in rows:
        summary = summaries.setdefault(row["mentor_id"], {
            "count": 0, "flagged": False,
            "reason_counts": dict.fromkeys(MISSING_RECORDING_REASONS, 0), "reports": []})
        summary["count"] += 1
        summary["reason_counts"][row["reason"]] += 1
        summary["reports"].append({
            "lesson_id": row["lesson_id"], "title": row["title"],
            "lesson_date": local_datetime(row["scheduled_at"]).isoformat(),
            "reason": row["reason"], "reason_label": MISSING_RECORDING_REASONS[row["reason"]],
            "note": row["note"],
            "submitted_at": datetime.fromtimestamp(row["submitted_at"], timezone.utc).isoformat(),
            "recording_uploaded": bool(row["recorded_at"]),
        })
        summary["flagged"] = summary["count"] > 3
    return summaries


def activity_for_user(user_id, users=None):
    users = users or users_by_id()
    rows = get_db().execute(LESSON_SELECT + " WHERE lessons.mentor_id = ? OR lessons.mentee_id = ? ORDER BY lessons.scheduled_at",
                            (user_id, user_id)).fetchall()
    completed = [row for row in rows if row["recorded_at"] and not row["cancelled_at"]]
    now = time.time()
    lessons = [present_lesson(row, users, now=now) for row in rows]
    upcoming = [lesson for lesson in lessons if not lesson["cancelled_at"] and not lesson["finished"]
                and lesson["active_pair"]]
    finished = [lesson for lesson in reversed(lessons) if lesson["finished"]]
    return {"hours": sum(row["recording_seconds"] or 0 for row in completed) / 3600,
            "completed": len(completed), "upcoming": upcoming, "finished": finished}


def room_summary(mentor_id, mentee_id):
    row = get_db().execute("""SELECT COUNT(*) AS completed, COALESCE(SUM(recording_seconds), 0) AS seconds
        FROM lessons WHERE mentor_id = ? AND mentee_id = ? AND recorded_at IS NOT NULL AND cancelled_at IS NULL""",
        (mentor_id, mentee_id)).fetchone()
    return {"completed": row["completed"], "hours": row["seconds"] / 3600}


@learning.get('/pairings/<int:mentor_id>/<int:mentee_id>')
def room(mentor_id, mentee_id):
    pair = room_access(mentor_id, mentee_id)
    users = users_by_id()
    now = time.time()
    lessons = [present_lesson(row, users, now=now) for row in get_db().execute(
        LESSON_SELECT + " WHERE lessons.mentor_id = ? AND lessons.mentee_id = ? ORDER BY lessons.scheduled_at DESC", (mentor_id, mentee_id))]
    recordings = [lesson for lesson in lessons if lesson["recorded_at"]]
    finished = [lesson for lesson in lessons if lesson["finished"]]
    pending = sorted([lesson for lesson in lessons if not lesson["finished"] and not lesson["cancelled_at"]],
                     key=lambda lesson: lesson["scheduled_at"])
    cancelled = [lesson for lesson in lessons if lesson["cancelled_at"]]
    return render_template("room.html", pair=pair, summary=room_summary(mentor_id, mentee_id),
                           recordings=recordings, pending=pending, finished=finished, cancelled=cancelled,
                           missing_recording_reasons=MISSING_RECORDING_REASONS,
                           messages=message_rows(mentor_id, mentee_id), timezone=current_app.config["LESSON_TIMEZONE"],
                           max_upload_mb=current_app.config['MAX_RECORDING_BYTES'] // (1024 * 1024),
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
        if fresh["recorded_at"] or fresh["cancelled_at"] or fresh["scheduled_at"] <= time.time():
            abort(409, description="Lessons cannot be edited once their scheduled start time has passed, or after recording or cancellation.")
        reject_conflicts(db, lesson["mentor_id"], lesson["mentee_id"], values[1], values[2], lesson_id)
        db.execute("UPDATE lessons SET title = ?, scheduled_at = ?, planned_minutes = ?, zoom_url = ? WHERE id = ?",
                   (*values, lesson_id))
    flash("Lesson updated for both participants.", "success")
    return redirect(url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="lessons"))


@learning.post('/lessons/<lesson_id>/cancel')
def cancel_lesson(lesson_id):
    lesson, _ = lesson_access(lesson_id, manage=True)
    with get_db() as db:
        now = int(time.time())
        result = db.execute("""UPDATE lessons SET cancelled_at = ?
            WHERE id = ? AND recorded_at IS NULL AND cancelled_at IS NULL AND scheduled_at > ?""",
                            (now, lesson_id, now))
        if not result.rowcount:
            abort(409, description="Lessons cannot be cancelled once their scheduled start time has passed, or after recording or cancellation.")
    flash("Lesson cancelled for both participants.", "success")
    return redirect(url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="lessons"))

@learning.post('/lessons/<lesson_id>/missing-recording')
def report_missing_recording(lesson_id):
    lesson, _ = lesson_access(lesson_id)
    if g.current_user.get('is_coordinator') is True or g.current_user["id"] != lesson["mentor_id"]:
        abort(403, description="Only this lesson's mentor can submit a missing-recording explanation.")
    reason = request.form.get("reason", "")
    note = request.form.get("note", "").strip()
    if reason not in MISSING_RECORDING_REASONS:
        abort(400, description="Choose a reason for the missing recording.")
    if len(note) > 1000 or (reason == "other" and not note):
        abort(400, description="For Other, provide an explanation. Keep explanations within 1,000 characters.")
    if reason != "other":
        note = ""
    db = get_db()
    with db:
        db.execute("BEGIN IMMEDIATE")
        fresh = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        room_access(fresh["mentor_id"], fresh["mentee_id"])
        now = int(time.time())
        if fresh["cancelled_at"] or fresh["recorded_at"] or fresh["scheduled_at"] + fresh["planned_minutes"] * 60 > now:
            abort(409, description="Explanations can only be submitted for finished lessons without a recording.")
        if db.execute("SELECT 1 FROM missing_recording_reports WHERE lesson_id = ?", (lesson_id,)).fetchone():
            abort(409, description="An explanation has already been saved for this lesson. It is counted once.")
        db.execute("INSERT INTO missing_recording_reports(lesson_id, mentor_id, reason, note, submitted_at) VALUES (?, ?, ?, ?, ?)",
                   (lesson_id, lesson["mentor_id"], reason, note, now))
    flash("Explanation saved for coordinator review. Attendance is only marked when a recording is uploaded.", "success")
    return redirect(url_for("learning.room", mentor_id=lesson["mentor_id"], mentee_id=lesson["mentee_id"], _anchor="lesson-" + lesson_id))


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
    abort(410, description='Use Upload Session Recording to upload directly to private storage.')


@learning.get('/lessons/<lesson_id>/recording')
def recording(lesson_id):
    lesson, _ = lesson_access(lesson_id)
    from recordings import recording_for_lesson, watch
    cloud_recording = recording_for_lesson(lesson_id)
    if cloud_recording:
        return watch(cloud_recording['id'])
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
            cursor = db.execute("INSERT INTO messages(mentor_id, mentee_id, sender_id, body, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                                (mentor_id, mentee_id, g.current_user["id"], body, int(time.time())))
            id = cursor.fetchone()['id']
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
    from recordings import coordinator_rows
    users = users_by_id()
    lessons = [present_lesson(row, users) for row in get_db().execute(LESSON_SELECT + " ORDER BY lessons.scheduled_at DESC")]
    people = [{"user": user, **activity_for_user(user["id"], users)} for user in users.values()
              if user.get("role") == "mentee" or mentor_is_approved(user)]
    return render_template("lesson_monitor.html", lessons=lessons, people=people,
                           reviewers=admin_reviewers(users),
                           cloud_recordings=coordinator_rows(),
                           recording_reports=recording_report_summaries(), missing_recording_reasons=MISSING_RECORDING_REASONS,
                           pending_reviews=sum(bool(row["recorded_at"] and not row["reviewed_at"]) for row in lessons),
                           timezone=current_app.config["LESSON_TIMEZONE"])


@learning.post('/lessons/<lesson_id>/review')
def review_recording(lesson_id):
    if g.current_user.get("is_coordinator") is not True:
        abort(403, description="Only coordinators can review recordings.")
    lesson, _ = lesson_access(lesson_id)
    from recordings import recording_for_lesson
    note = request.form.get("review_note", "").strip()
    if len(note) > 2000:
        abort(400, description="Keep review notes within 2,000 characters.")
    reviewer_id, reviewer_name = selected_reviewer()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        fresh, _ = lesson_access(lesson_id)
        if fresh['reviewed_at'] is not None:
            abort(409, description='This review is final and cannot be edited.')
        if recording_for_lesson(lesson_id):
            abort(409, description='Use the current recording review form. Reload this page.')
        result = db.execute("""UPDATE lessons SET reviewed_at = ?, reviewed_by = ?, review_note = ?,
            review_submitted_by = ?, reviewer_name = ?
            WHERE id = ? AND recording_filename = ? AND reviewed_at IS NULL""",
            (int(time.time()), reviewer_id, note, g.current_user['id'], reviewer_name,
             lesson_id, request.form.get("recording_version")))
        if not result.rowcount:
            abort(409, description="The recording has changed or is missing. Reload it before saving a review.")
    flash("Review finalised. The reviewer and comments are visible to the mentor and student.", "success")
    return redirect(url_for('learning.monitor', _anchor='lesson-review-' + lesson_id))
