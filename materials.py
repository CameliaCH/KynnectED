"""Member resource submissions with coordinator approval before publication."""
from pathlib import Path
import time
from uuid import uuid4
import zipfile
import zlib

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from learning import get_db, users_by_id
from account_store import mentor_is_approved

materials = Blueprint('materials', __name__)
FILE_TYPES = {'.pdf': 'application/pdf',
              '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'}


@materials.before_request
def require_login():
    if g.current_user is None:
        return redirect(url_for('login'))


@materials.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def approved_materials(folder):
    return [dict(row) for row in get_db().execute(
        "SELECT * FROM resource_materials WHERE folder = ? AND status = 'approved' ORDER BY uploaded_at DESC, rowid DESC", (folder,))]


def approved_counts():
    return {row['folder']: row['count'] for row in get_db().execute(
        "SELECT folder, COUNT(*) AS count FROM resource_materials WHERE status = 'approved' GROUP BY folder")}


def submission_folders(user):
    """Admins upload anywhere; approved mentors contribute student resources only."""
    if not user:
        return {}
    folders = current_app.config['RESOURCE_FOLDERS']
    if user.get('is_coordinator') is True:
        return folders
    if mentor_is_approved(user):
        return {slug: folders[slug] for slug in current_app.config['STUDENT_RESOURCE_FOLDERS']}
    return {}


def validate_slides(path, extension):
    if extension == '.pdf':
        with path.open('rb') as source:
            if source.read(5) == b'%PDF-':
                return
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = {entry.filename: entry for entry in archive.infolist()}
                if not {'[Content_Types].xml', 'ppt/presentation.xml'}.issubset(entries):
                    raise ValueError()
                if any(name.lower().endswith('vbaproject.bin') for name in entries):
                    raise ValueError()
                if len(entries) > 5000 or sum(entry.file_size for entry in entries.values()) > 100 * 1024 * 1024:
                    raise ValueError()
                if entries['[Content_Types].xml'].file_size > 1024 * 1024:
                    raise ValueError()
                if b'presentationml.presentation.main+xml' in archive.read('[Content_Types].xml'):
                    return
        except (zipfile.BadZipFile, RuntimeError, ValueError, KeyError, OSError, zlib.error):
            pass
    abort(400, description='Choose a valid PDF or PowerPoint (.pptx) file.')


@materials.route('/learning-resources/submit', methods=['GET', 'POST'])
def submit():
    folders = submission_folders(g.current_user)
    if not folders:
        abort(403, description='Only approved mentors and coordinators can submit resource materials.')
    limit = current_app.config['MAX_MATERIAL_BYTES']
    if request.method == 'GET':
        submissions = [dict(row) for row in get_db().execute(
            'SELECT * FROM resource_materials WHERE uploaded_by = ? ORDER BY uploaded_at DESC, rowid DESC', (g.current_user['id'],))]
        return render_template('material_submit.html', folders=folders, submissions=submissions,
                               all_folders=current_app.config['RESOURCE_FOLDERS'],
                               max_material_mb=limit // (1024 * 1024))
    title = request.form.get('title', '').strip()
    folder = request.form.get('folder', '')
    if not title or len(title) > 120 or folder not in current_app.config['RESOURCE_FOLDERS']:
        abort(400, description='Enter a title of up to 120 characters and choose a resource folder.')
    if folder not in folders:
        abort(403, description='Only admins can upload materials to For mentors. Choose a For students folder.')
    upload = request.files.get('material')
    if not upload or not upload.filename:
        abort(400, description='Choose slide material to upload.')
    original = secure_filename(upload.filename)
    extension = Path(original).suffix.lower()
    if extension not in FILE_TYPES:
        abort(400, description='Upload a PDF or PowerPoint (.pptx) file.')
    directory = Path(current_app.config['MATERIALS_DIR'])
    directory.mkdir(parents=True, exist_ok=True)
    material_id = uuid4().hex
    filename = material_id + extension
    path = directory / filename
    committed = False
    size = 0
    try:
        with path.open('xb') as destination:
            while chunk := upload.stream.read(64 * 1024):
                size += len(chunk)
                if size > limit:
                    abort(413, description='This slide file is too large.')
                destination.write(chunk)
        validate_slides(path, extension)
        with get_db() as db:
            db.execute('''INSERT INTO resource_materials
                (id, folder, title, filename, original_filename, mimetype, size_bytes, uploaded_by, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (material_id, folder, title, filename, original, FILE_TYPES[extension], size,
                 g.current_user['id'], int(time.time())))
        committed = True
    finally:
        if not committed:
            path.unlink(missing_ok=True)
    flash('Slides submitted. They will appear in the resource folder after coordinator approval.', 'success')
    return redirect(url_for('materials.submit'))


@materials.get('/materials/<material_id>/download')
def download(material_id):
    row = get_db().execute('SELECT * FROM resource_materials WHERE id = ?', (material_id,)).fetchone()
    if row is None:
        abort(404)
    if row['status'] != 'approved' and row['uploaded_by'] != g.current_user['id'] and not g.current_user.get('is_coordinator'):
        abort(403, description='This material is awaiting approval and is only available to its author and coordinators.')
    path = Path(current_app.config['MATERIALS_DIR']) / row['filename']
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype=row['mimetype'], download_name=row['original_filename'], as_attachment=True)


@materials.get('/coordinator/materials')
def review_queue():
    if not g.current_user.get('is_coordinator'):
        abort(403, description='Only coordinators can review resource materials.')
    users = users_by_id()
    submissions = []
    for row in get_db().execute("SELECT * FROM resource_materials ORDER BY (status = 'pending') DESC, uploaded_at DESC, rowid DESC"):
        submission = dict(row)
        submission['author'] = ' '.join(users.get(row['uploaded_by'], {}).get('name', [])) or 'Former member'
        submissions.append(submission)
    return render_template('material_review.html', submissions=submissions, folders=current_app.config['RESOURCE_FOLDERS'])


@materials.post('/materials/<material_id>/review')
def review(material_id):
    if not g.current_user.get('is_coordinator'):
        abort(403, description='Only coordinators can approve or decline resource materials.')
    action = request.form.get('action', '')
    note = request.form.get('review_note', '').strip()
    if action not in {'approve', 'reject'} or len(note) > 1000:
        abort(400, description='Choose Approve or Decline and keep feedback within 1,000 characters.')
    db = get_db()
    with db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM resource_materials WHERE id = ?', (material_id,)).fetchone()
        if row is None:
            abort(404)
        if row['status'] != 'pending':
            abort(409, description='This submission has already been reviewed.')
        db.execute('''UPDATE resource_materials SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
            WHERE id = ?''', ('approved' if action == 'approve' else 'rejected', g.current_user['id'], int(time.time()), note, material_id))
    flash('Material approved and added to the resource folder.' if action == 'approve' else 'Submission declined. Feedback is available to the author.', 'success')
    return redirect(url_for('materials.review_queue'))
