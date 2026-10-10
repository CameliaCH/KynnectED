"""Private R2 multipart recordings, integrated with the existing lessons/auth store."""
import json
import math
from pathlib import Path
import subprocess
import time
from uuid import uuid4
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from account_store import mentor_is_approved
from learning import get_db, lesson_access, local_datetime, present_lesson, room_access, users_by_id, public_review, selected_reviewer

recordings = Blueprint('recordings', __name__)
PART_BYTES = 16 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600
UPLOAD_LIFETIME = 24 * 3600
PROCESSING_LIFETIME = 10 * 60
VIDEO_TYPES = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime'}
SUCCESS_MESSAGE = ('Recording uploaded successfully. This recording will be retained for 7 days '
                   'unless it is required for a safeguarding concern.')


@recordings.before_request
def require_login():
    if g.current_user is None:
        abort(401, description='Please log in to access recordings.')
    if request.path.startswith('/api/') and request.content_length and request.content_length > 32 * 1024:
        abort(413, description='This request is too large.')


@recordings.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@recordings.errorhandler(HTTPException)
def recording_error(error):
    if request.path.startswith('/api/'):
        return jsonify(error=error.description), error.code
    return render_template('error.html', error=error.description), error.code


# Explicit codes take precedence over the app's existing HTML-only error handlers.
for error_code in (400, 401, 403, 404, 409, 410, 413, 503):
    recordings.register_error_handler(error_code, recording_error)


def s3_client():
    """Explicit credentials only; never fall back to instance profiles or log URLs."""
    if 'r2_client' not in g:
        config = current_app.config
        if not all(config.get(key) for key in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID',
                                               'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME')):
            abort(503, description='Recording storage is not configured yet. Please contact a coordinator.')
        endpoint = config.get('R2_ENDPOINT') or f"https://{config['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.r2.cloudflarestorage.com')
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            abort(503, description='Recording storage needs a valid Cloudflare S3 endpoint.')
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            abort(503, description='Recording storage needs the boto3 package installed on the server.')
        g.r2_client = boto3.client('s3', endpoint_url=endpoint,
            aws_access_key_id=config['R2_ACCESS_KEY_ID'], aws_secret_access_key=config['R2_SECRET_ACCESS_KEY'],
            region_name='auto', config=Config(signature_version='s3v4',
                s3={'addressing_style': 'path'}, connect_timeout=10, read_timeout=30,
                retries={'max_attempts': 2, 'mode': 'standard'},
                request_checksum_calculation='when_required', response_checksum_validation='when_required'))
    return g.r2_client


def storage_call(method, **kwargs):
    client = s3_client()
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        return getattr(client, method)(Bucket=current_app.config['R2_BUCKET_NAME'], **kwargs)
    except (BotoCoreError, ClientError):
        # Provider errors may include storage identifiers. Return only a safe message.
        abort(503, description='Recording storage could not complete this request. Please try again.')


def object_head(key):
    client = s3_client()
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        return client.head_object(Bucket=current_app.config['R2_BUCKET_NAME'], Key=key)
    except ClientError as error:
        if str(error.response.get('Error', {}).get('Code')) in ('404', 'NoSuchKey', 'NotFound'):
            return None
        abort(503, description='Recording storage is temporarily unavailable. Please try again.')
    except BotoCoreError:
        abort(503, description='Recording storage is temporarily unavailable. Please try again.')


def sign_url(operation, params, seconds):
    client = s3_client()
    from botocore.exceptions import BotoCoreError
    try:
        return client.generate_presigned_url(operation,
            Params={'Bucket': current_app.config['R2_BUCKET_NAME'], **params}, ExpiresIn=seconds)
    except BotoCoreError:
        abort(503, description='Recording storage is temporarily unavailable. Please try again.')


def recording_for_lesson(session_id):
    row = get_db().execute('SELECT * FROM recordings WHERE session_id = ? AND active = 1', (session_id,)).fetchone()
    return dict(row) if row else None


def present_recording(row, now=None, users=None):
    """Share final review comments; keep object keys and safeguarding notes private."""
    now = time.time() if now is None else now
    retained = bool(row['safeguarding_flag'])
    expired = not retained and now >= row['expires_at']
    return {**public_review(row, users), 'id': row['id'], 'available': not expired, 'expired': expired,
            'expiry_label': 'Retained' if retained else local_datetime(row['expires_at']).strftime('%d %b %Y · %H:%M'),
            'upload_label': local_datetime(row['uploaded_at']).strftime('%d %b %Y · %H:%M'),
            'original_filename': row['original_filename'], 'content_type': row['content_type'],
            'review_label': {'awaiting_review': 'Awaiting Review', 'reviewed': 'Reviewed', 'flagged': 'Flagged'}[row['review_status']]}


def coordinator_rows():
    users = users_by_id()
    rows = get_db().execute('SELECT recordings.*, lessons.title, lessons.scheduled_at FROM recordings '
                           'JOIN lessons ON lessons.id = recordings.session_id ORDER BY uploaded_at DESC, recordings.rowid DESC')
    return [{**present_recording(row, users=users), 'title': row['title'],
             'session_label': local_datetime(row['scheduled_at']).strftime('%d %b %Y · %H:%M'),
             'mentor': users.get(row['mentor_id'], {'name': ['Former mentor']}),
             'mentee': users.get(row['mentee_id'], {'name': ['Former mentee']}),
             'safeguarding_flag': row['safeguarding_flag'], 'active': row['active'],
             'review_note': row['review_note'], 'safeguarding_note': row['safeguarding_note']} for row in rows]


def assigned_mentor(session_id):
    lesson, pair = lesson_access(session_id)
    if (g.current_user.get('is_coordinator') is True or not mentor_is_approved(g.current_user)
            or g.current_user['id'] != lesson['mentor_id']):
        abort(403, description='Only the assigned, approved mentor can upload this session recording.')
    if not pair['active']:
        abort(403, description='This pairing is inactive.')
    if lesson['cancelled_at'] or lesson['scheduled_at'] > time.time():
        abort(409, description='Upload after the lesson starts. Cancelled lessons cannot receive recordings.')
    return lesson


def baseline(lesson):
    recording = recording_for_lesson(lesson['id'])
    return recording['id'] if recording else (lesson['recording_filename'] or '')


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description='Send a valid recording request.')
    return data


def upload_access(upload_id, allow_completed=False):
    row = get_db().execute('SELECT * FROM recording_uploads WHERE id = ?', (upload_id,)).fetchone()
    if not row:
        abort(404)
    assigned_mentor(row['session_id'])
    if row['mentor_id'] != g.current_user['id']:
        abort(403)
    if allow_completed and row['status'] == 'completed':
        return dict(row)
    if row['status'] == 'aborted' or row['created_at'] + UPLOAD_LIFETIME <= time.time():
        abort(410, description='This upload has expired or was cancelled. Start a new upload.')
    if row['status'] == 'completed':
        abort(409, description='This upload is already complete.')
    return dict(row)


@recordings.post('/api/lessons/<session_id>/recording-uploads')
def start_upload(session_id):
    lesson = assigned_mentor(session_id)
    if lesson['reviewed_at'] is not None:
        abort(409, description='This recording has a final review and cannot be replaced.')
    data = json_body()
    if data.get('confirmed') is not True:
        abort(400, description='Please tick the recording confirmation before uploading.')
    filename = secure_filename(str(data.get('filename', '')))[:180]
    content_type = data.get('content_type')
    size = data.get('file_size')
    if not filename or VIDEO_TYPES.get(Path(filename).suffix.lower()) != content_type:
        abort(400, description='Choose an MP4, WebM or MOV video.')
    if type(size) is not int or not 1 <= size <= current_app.config['MAX_RECORDING_BYTES']:
        abort(400, description='Recordings must be between 1 byte and 2 GB.')
    current = baseline(lesson)
    if current and (data.get('replace') is not True or data.get('recording_version') != current):
        abort(409, description='A recording already exists. Reload and explicitly choose Replace recording.')
    upload_id = uuid4().hex
    key = f'sessions/{session_id}/{uuid4().hex}{Path(filename).suffix.lower()}'
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        # Expired permissions must not occupy the mentor's limited upload slots forever.
        db.execute("UPDATE recording_uploads SET status = 'aborted' WHERE created_at <= ? "
                   "AND (status = 'pending' OR (status = 'processing' AND processing_at <= ?))",
                   (int(time.time()) - UPLOAD_LIFETIME, int(time.time()) - PROCESSING_LIFETIME))
        fresh = assigned_mentor(session_id)
        if fresh['reviewed_at'] is not None:
            abort(409, description='This recording has a final review and cannot be replaced.')
        if baseline(fresh) != current:
            abort(409, description='This recording changed. Reload the session before uploading.')
        old = db.execute("SELECT * FROM recording_uploads WHERE session_id = ? AND status IN ('pending', 'processing')", (session_id,)).fetchone()
        if old:
            if old['status'] == 'processing' and old['processing_at'] + PROCESSING_LIFETIME > time.time():
                abort(409, description='An upload is being checked. Please wait before trying again.')
            # A new attempt deliberately replaces the unfinished upload, never a saved recording.
            cancel_multipart(dict(old))
            db.execute("UPDATE recording_uploads SET status = 'aborted' WHERE id = ?", (old['id'],))
        pending = db.execute("SELECT COUNT(*) FROM recording_uploads WHERE mentor_id = ? AND status IN ('pending', 'processing')", (g.current_user['id'],)).fetchone()[0]
        if pending >= 3:
            abort(409, description='Finish or cancel your other uploads before starting another.')
        multipart = storage_call('create_multipart_upload', Key=key, ContentType=content_type, CacheControl='private, no-store',
                                 Metadata={'kynnected-upload': upload_id})
        try:
            now = int(time.time())
            db.execute('INSERT INTO recording_uploads(id, session_id, mentor_id, r2_object_key, multipart_id, '
                       'original_filename, content_type, file_size, baseline, consent_at, created_at) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (upload_id, session_id, g.current_user['id'], key, multipart['UploadId'], filename,
                        content_type, size, current, now, now))
        except Exception:
            storage_call('abort_multipart_upload', Key=key, UploadId=multipart['UploadId'])
            raise
    return jsonify(upload_id=upload_id, part_size=PART_BYTES,
        part_url=url_for('recordings.upload_part_url', upload_id=upload_id),
        complete_url=url_for('recordings.complete_upload', upload_id=upload_id),
        abort_url=url_for('recordings.abort_upload', upload_id=upload_id)), 201


@recordings.post('/api/recording-uploads/<upload_id>/part')
def upload_part_url(upload_id):
    upload = upload_access(upload_id)
    if upload['status'] != 'pending':
        abort(409, description='This recording is being checked. Please wait.')
    number = json_body().get('part_number')
    count = math.ceil(upload['file_size'] / PART_BYTES)
    if type(number) is not int or not 1 <= number <= count:
        abort(400, description='Invalid upload part.')
    size = min(PART_BYTES, upload['file_size'] - (number - 1) * PART_BYTES)
    url = sign_url('upload_part', {'Key': upload['r2_object_key'], 'UploadId': upload['multipart_id'],
                                 'PartNumber': number, 'ContentLength': size}, 900)
    return jsonify(url=url)


def cancel_multipart(upload):
    client = s3_client()
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        client.abort_multipart_upload(Bucket=current_app.config['R2_BUCKET_NAME'],
                                          Key=upload['r2_object_key'], UploadId=upload['multipart_id'])
    except ClientError as error:
        if error.response.get('Error', {}).get('Code') != 'NoSuchUpload':
            abort(503, description='The unfinished upload could not be cancelled. Please try again.')
    except BotoCoreError:
        abort(503, description='The unfinished upload could not be cancelled. Please try again.')


@recordings.post('/api/recording-uploads/<upload_id>/abort')
def abort_upload(upload_id):
    upload = upload_access(upload_id)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        fresh = db.execute('SELECT status, processing_at FROM recording_uploads WHERE id = ?', (upload_id,)).fetchone()
        if fresh['status'] == 'completed' or (fresh['status'] == 'processing' and fresh['processing_at'] + PROCESSING_LIFETIME > time.time()):
            abort(409, description='This recording is being checked or has already been saved.')
        cancel_multipart(upload)
        db.execute("UPDATE recording_uploads SET status = 'aborted' WHERE id = ?", (upload_id,))
    return jsonify(cancelled=True)


def verify_parts(upload):
    """Use provider-reported sizes and ETags, never a client-supplied completion list."""
    result = storage_call('list_parts', Key=upload['r2_object_key'], UploadId=upload['multipart_id'])
    parts = result.get('Parts', [])
    count = math.ceil(upload['file_size'] / PART_BYTES)
    if result.get('IsTruncated') or len(parts) != count:
        abort(400, description='Some recording chunks are missing. Retry the upload.')
    verified = []
    for number, part in enumerate(parts, start=1):
        expected = min(PART_BYTES, upload['file_size'] - (number - 1) * PART_BYTES)
        if part.get('PartNumber') != number or part.get('Size') != expected or not part.get('ETag'):
            abort(400, description='A recording chunk has an unexpected size. Retry the upload.')
        verified.append({'PartNumber': number, 'ETag': part['ETag']})
    return verified


def probe_cloud_video(upload):
    """Probe the trusted R2 object remotely; do not download/save the video on Flask."""
    url = sign_url('get_object', {'Key': upload['r2_object_key']}, 120)
    extension = Path(upload['original_filename']).suffix.lower()
    demuxer = 'matroska' if extension == '.webm' else 'mov'
    demuxer_options = ['-enable_drefs', '0'] if demuxer == 'mov' else []
    try:
        process = subprocess.run([current_app.config['FFPROBE_BIN'], '-v', 'error',
            '-protocol_whitelist', 'https,tls,tcp', '-rw_timeout', '15000000',
            '-probesize', '8388608', '-analyzeduration', '5000000', *demuxer_options, '-f', demuxer,
            '-show_entries', 'stream=codec_type,codec_name:format=duration', '-of', 'json', url],
            capture_output=True, text=True, timeout=45, check=True)
        metadata = json.loads(process.stdout)
        seconds = float(metadata['format']['duration'])
        codecs = {stream.get('codec_name') for stream in metadata.get('streams', []) if stream.get('codec_type') == 'video'}
        allowed = {'vp8', 'vp9', 'av1'} if extension == '.webm' else {'h264', 'av1'}
        if not math.isfinite(seconds) or not 0 < seconds <= 24 * 3600 or not codecs or not codecs.issubset(allowed):
            raise ValueError()
        return seconds
    except FileNotFoundError:
        abort(503, description="The server needs FFmpeg's ffprobe installed to verify recordings.")
    except (ValueError, KeyError, subprocess.SubprocessError):
        abort(400, description='Could not verify the video. Use H.264 MP4/MOV or VP8/VP9 WebM and try again.')


@recordings.post('/api/recording-uploads/<upload_id>/complete')
def complete_upload(upload_id):
    upload = upload_access(upload_id, allow_completed=True)
    if upload['status'] == 'completed':
        return jsonify(message=SUCCESS_MESSAGE, watch_url=url_for('recordings.watch', recording_id=upload_id))
    db = get_db()
    claimed_at = int(time.time())
    with db:
        result = db.execute("UPDATE recording_uploads SET status = 'processing', processing_at = ? WHERE id = ? "
                            "AND (status = 'pending' OR (status = 'processing' AND processing_at < ?))",
                            (claimed_at, upload_id, claimed_at - PROCESSING_LIFETIME))
        if not result.rowcount:
            abort(409, description='This upload is already being checked. Please wait.')
    try:
        # If the completion response was lost, recover the same immutable object on retry.
        head = object_head(upload['r2_object_key'])
        if head is None:
            parts = verify_parts(upload)
            storage_call('complete_multipart_upload', Key=upload['r2_object_key'], UploadId=upload['multipart_id'],
                         MultipartUpload={'Parts': parts})
            head = object_head(upload['r2_object_key'])
        if (not head or head.get('ContentLength') != upload['file_size'] or head.get('ContentType') != upload['content_type']
                or head.get('Metadata', {}).get('kynnected-upload') != upload_id):
            abort(400, description='The uploaded recording could not be verified. Start a new upload.')
        duration = probe_cloud_video(upload)
        with db:
            db.execute('BEGIN IMMEDIATE')
            fresh = assigned_mentor(upload['session_id'])
            if fresh['reviewed_at'] is not None:
                abort(409, description='This recording has a final review and cannot be replaced.')
            state = db.execute('SELECT status, processing_at FROM recording_uploads WHERE id = ?', (upload_id,)).fetchone()
            if state['status'] != 'processing' or state['processing_at'] != claimed_at or baseline(fresh) != upload['baseline']:
                abort(409, description='The session recording changed. Reload before trying again.')
            # Use R2's object creation time: lifecycle retention starts when multipart finishes.
            uploaded_at = int(head['LastModified'].timestamp())
            db.execute('UPDATE recordings SET active = 0 WHERE session_id = ? AND active = 1', (fresh['id'],))
            db.execute('INSERT INTO recordings(id, session_id, mentor_id, mentee_id, r2_object_key, original_filename, '
                       'content_type, file_size, uploaded_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (upload_id, fresh['id'], fresh['mentor_id'], fresh['mentee_id'], upload['r2_object_key'],
                        upload['original_filename'], upload['content_type'], upload['file_size'], uploaded_at,
                        uploaded_at + RETENTION_SECONDS))
            # Existing attendance/hour queries keep working; no object key is written into lessons.
            db.execute('UPDATE lessons SET recording_filename = NULL, recording_original = ?, recording_mimetype = ?, '
                       'recording_seconds = ?, recorded_at = ?, uploaded_by = ?, reviewed_at = NULL, reviewed_by = NULL, '
                       'review_note = NULL, review_submitted_by = NULL, reviewer_name = NULL WHERE id = ?', (upload['original_filename'], upload['content_type'], duration,
                       uploaded_at, g.current_user['id'], fresh['id']))
            db.execute("UPDATE recording_uploads SET status = 'completed' WHERE id = ?", (upload_id,))
    except Exception:
        with db:
            db.execute("UPDATE recording_uploads SET status = 'pending', processing_at = NULL WHERE id = ? AND status = 'processing' AND processing_at = ?", (upload_id, claimed_at))
        raise
    return jsonify(message=SUCCESS_MESSAGE,
        watch_url=url_for('recordings.watch', recording_id=upload_id))


def recording_access(recording_id, coordinator=False):
    if coordinator and g.current_user.get('is_coordinator') is not True:
        abort(403, description='Only coordinators can review or flag recordings.')
    row = get_db().execute('SELECT * FROM recordings WHERE id = ?', (recording_id,)).fetchone()
    if not row:
        abort(404)
    room_access(row['mentor_id'], row['mentee_id'])
    if not row['active'] and g.current_user.get('is_coordinator') is not True:
        abort(403, description='This recording has been replaced.')
    return dict(row)


def ensure_available(row):
    if not row['safeguarding_flag'] and row['expires_at'] <= time.time():
        abort(410, description='This recording has expired. The session attendance is still saved.')
    head = object_head(row['r2_object_key'])
    if not head:
        abort(410, description='This recording is no longer available. The session attendance is still saved.')
    return head


@recordings.get('/recordings/<recording_id>')
def watch(recording_id):
    row = recording_access(recording_id)
    lesson, _ = lesson_access(row['session_id'])
    return render_template('recording_watch.html', lesson=present_lesson(lesson),
                           recording=present_recording(row), review=row if g.current_user.get('is_coordinator') is True else None)


@recordings.get('/api/recordings/<recording_id>/playback')
def playback(recording_id):
    row = recording_access(recording_id)
    ensure_available(row)
    seconds = 1200 if row['safeguarding_flag'] else min(1200, row['expires_at'] - int(time.time()))
    if seconds <= 0:
        abort(410, description='This recording has expired.')
    url = sign_url('get_object', {'Key': row['r2_object_key']}, seconds)
    return jsonify(url=url, expires_in=seconds)


@recordings.post('/recordings/<recording_id>/review')
def mark_reviewed(recording_id):
    row = recording_access(recording_id, coordinator=True)
    note = request.form.get('review_note', '').strip()
    if len(note) > 2000:
        abort(400, description='Keep review notes within 2,000 characters.')
    reviewer_id, reviewer_name = selected_reviewer()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = recording_access(recording_id, coordinator=True)
        if row['reviewed_at'] is not None:
            abort(409, description='This review is final and cannot be edited.')
        now = int(time.time())
        db.execute('UPDATE recordings SET review_status = CASE WHEN safeguarding_flag = 1 THEN \'flagged\' ELSE \'reviewed\' END, '
                   'reviewed_by = ?, reviewed_at = ?, review_note = ?, review_submitted_by = ?, reviewer_name = ? '
                   'WHERE id = ? AND reviewed_at IS NULL',
                   (reviewer_id, now, note, g.current_user['id'], reviewer_name, recording_id))
        if row['active']:
            db.execute('UPDATE lessons SET reviewed_at = ?, reviewed_by = ?, review_note = ?, '
                       'review_submitted_by = ?, reviewer_name = ? WHERE id = ?',
                       (now, reviewer_id, note, g.current_user['id'], reviewer_name, row['session_id']))
    flash('Review finalised. The reviewer and comments are visible to the mentor and student.', 'success')
    return redirect(url_for('learning.monitor', _anchor='recording-' + recording_id))


@recordings.post('/recordings/<recording_id>/flag')
def flag_concern(recording_id):
    recording_access(recording_id, coordinator=True)
    if request.form.get('confirm_flag') != 'yes':
        abort(400, description='Confirm that this recording must be retained for a safeguarding concern.')
    note = request.form.get('safeguarding_note', '').strip()
    if len(note) > 2000:
        abort(400, description='Keep safeguarding notes within 2,000 characters.')
    # Serialize the copy and metadata change against replacements and other flag actions.
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = recording_access(recording_id, coordinator=True)
        if not row['safeguarding_flag']:
            head = ensure_available(row)
            retained_key = 'flagged/' + row['r2_object_key'].removeprefix('sessions/')
            storage_call('copy_object', Key=retained_key,
                CopySource={'Bucket': current_app.config['R2_BUCKET_NAME'], 'Key': row['r2_object_key']},
                CopySourceIfMatch=head['ETag'])
            retained = object_head(retained_key)
            if not retained or retained.get('ContentLength') != row['file_size']:
                abort(503, description='The retained copy could not be verified. Please retry; this recording has not been marked retained.')
            now = int(time.time())
            db.execute("UPDATE recordings SET r2_object_key = ?, safeguarding_flag = 1, review_status = 'flagged', "
                       'flagged_at = ?, flagged_by = ?, safeguarding_note = ? WHERE id = ?',
                       (retained_key, now, g.current_user['id'], note, recording_id))
            # The original sessions/ copy may expire normally; never delete flagged/ objects.
    flash('Recording retained for safeguarding review. It is exempt from the seven-day deletion rule.', 'success')
    return redirect(url_for('recordings.watch', recording_id=recording_id))
