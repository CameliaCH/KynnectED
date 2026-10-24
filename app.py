from flask import Flask, render_template, request, redirect, send_file, abort, session, g, url_for, flash
import json, argon2, csv, io, os, secrets, hmac, hashlib
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo
import click
from account_store import read_accounts, edit_accounts, session_secret, mentor_is_approved, valid_contact_email, normalize_contact_number
from account_store import account_email, account_whatsapp, next_account_id
from database import uses_postgres
from learning import learning, activity_for_user, room_summary, recording_report_summaries, MISSING_RECORDING_REASONS
from materials import materials, approved_materials, approved_counts, submission_folders
from recordings import recordings
from password_reset import password_reset, init_password_reset
from matcher import matching_results
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'), override=False)

ph = argon2.PasswordHasher()

app = Flask(__name__)

MENTOR_CONDUCT_VERSION = "1.0"
LEARNER_GUIDELINES_VERSION = "1.1"
MENTOR_CONDUCT_SIGNUP_FILE = Path(app.static_folder) / "documents/KynnectED_Mentor_Code_of_Conduct_Signup.pdf"
MENTOR_CONDUCT_DOCUMENT_SHA256 = hashlib.sha256(MENTOR_CONDUCT_SIGNUP_FILE.read_bytes()).hexdigest()
CONDUCT_DOCUMENTS = json.loads((Path(__file__).parent / "content/mentor-conduct.json").read_text())

app.config.update(
    DATA_BACKEND=os.environ.get('KYNNECTED_DATA_BACKEND', 'local'),
    SUPABASE_DATABASE_URL=os.environ.get('SUPABASE_DATABASE_URL', ''),
    TEMPLATES_AUTO_RELOAD=True,
    SECRET_KEY=session_secret(app.instance_path),
    USERS_FILE=os.environ.get("KYNNECTED_USERS_FILE", str(Path(__file__).with_name("users.json"))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("KYNNECTED_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    LEARNING_DATABASE=os.environ.get("KYNNECTED_LEARNING_DATABASE", str(Path(app.instance_path) / "learning.sqlite3")),
    RECORDINGS_DIR=os.environ.get("KYNNECTED_RECORDINGS_DIR", str(Path(app.instance_path) / "recordings")),
    MATERIALS_DIR=os.environ.get("KYNNECTED_MATERIALS_DIR", str(Path(app.instance_path) / "materials")),
    MAX_MATERIAL_BYTES=25 * 1024 * 1024,
    LESSON_TIMEZONE="Asia/Kuala_Lumpur",
    MENTEES_MANAGE_LESSONS=False,
    MAX_CONTENT_LENGTH=26 * 1024 * 1024,
    MAX_RECORDING_BYTES=2 * 1024 * 1024 * 1024,
    R2_ACCOUNT_ID=os.environ.get('R2_ACCOUNT_ID', ''),
    R2_ACCESS_KEY_ID=os.environ.get('R2_ACCESS_KEY_ID', ''),
    R2_SECRET_ACCESS_KEY=os.environ.get('R2_SECRET_ACCESS_KEY', ''),
    R2_BUCKET_NAME=os.environ.get('R2_BUCKET_NAME', ''),
    R2_ENDPOINT=os.environ.get('R2_ENDPOINT', ''),
    FFPROBE_BIN=os.environ.get("FFPROBE_BIN", "ffprobe"),
)
if app.config['DATA_BACKEND'] not in {'local', 'supabase'}:
    raise RuntimeError('KYNNECTED_DATA_BACKEND must be local or supabase.')
if app.config['DATA_BACKEND'] == 'supabase' and not app.config['SUPABASE_DATABASE_URL']:
    raise RuntimeError('SUPABASE_DATABASE_URL is required for the Supabase backend.')
app.register_blueprint(learning)
app.register_blueprint(materials)
app.register_blueprint(recordings)
app.register_blueprint(password_reset)
init_password_reset(app)


def read_users():
    return read_accounts(app.config["USERS_FILE"])


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def load_current_user():
    g.current_user = None
    if 'user_id' in session and session.get('data_backend', 'local') != ('supabase' if uses_postgres() else 'local'):
        session.clear()
    if "user_id" in session:
        g.current_user = next((user for user in read_users() if user["id"] == session["user_id"]), None)
        if g.current_user is None or session.get("auth_version", 0) != g.current_user.get("auth_version", 0):
            g.current_user = None
            session.clear()
    if request.method == "POST":
        if request.endpoint == 'learning.upload_recording':
            abort(410, description='Video uploads now go directly to private storage. Reload the lesson page.')
        if request.blueprint == 'recordings' and g.current_user is None:
            return {'error': 'Please log in to access recordings.'}, 401
        if request.endpoint == 'materials.submit' and request.content_length is not None and request.content_length > app.config['MAX_MATERIAL_BYTES'] + 64 * 1024:
            abort(413)
        expected = session.get("csrf_token", "")
        if not expected:
            abort(400, description="This form has expired. Reload the page and try again.")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
            abort(400, description="This form has expired. Reload the page and try again.")
    if (g.current_user and session.get('require_email') and not account_email(g.current_user)
            and request.endpoint not in {'complete_email', 'logout', 'static'}
            and request.blueprint != 'password_reset'):
        return redirect(url_for('complete_email'))


@app.context_processor
def account_context():
    return {"current_user": g.current_user, "csrf_token": csrf_token,
            "account_email": account_email, "account_whatsapp": account_whatsapp,
            "can_submit_materials": bool(g.current_user and (mentor_is_approved(g.current_user) or g.current_user.get('is_coordinator') is True))}


@app.after_request
def protect_account_responses(response):
    if request.endpoint in {"dashboard", "coordinator", "pairings", "export", "export_csv", "mentor_approvals", "approve_mentor", "complete_email"}:
        response.headers["Cache-Control"] = "no-store"
    return response


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.current_user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def coordinator_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.current_user.get("is_coordinator") is not True:
            abort(403, description="This page is available to coordinators only.")
        return view(*args, **kwargs)
    return wrapped


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(409)
@app.errorhandler(413)
@app.errorhandler(503)
def account_error(error):
    if error.code == 413:
        if request.endpoint == 'materials.submit':
            limit = app.config['MAX_MATERIAL_BYTES'] // (1024 * 1024)
            return render_template('error.html', error=f'This upload is too large. The slide material limit is {limit} MB.'), 413
        return render_template("error.html", error="This request is too large. Use the direct recording upload for videos up to 2 GB."), 413
    return render_template("error.html", error=error.description), error.code


@app.cli.command("set-coordinator")
@click.argument("username")
@click.option("--revoke", is_flag=True, help="Remove coordinator access.")
def set_coordinator(username, revoke):
    """Grant coordinator access to an existing account; signup cannot grant it."""
    with edit_accounts(app.config["USERS_FILE"]) as users:
        user = next((user for user in users if user["username"].lower() == username.lower()), None)
        if user is None:
            raise click.ClickException("No account has that username.")
        user["is_coordinator"] = not revoke
    click.echo(f"Coordinator access {'removed from' if revoke else 'granted to'} {username}.")

@app.route('/signup')
def signup():
    return render_template("signup.html", conduct_document=CONDUCT_DOCUMENTS["signup"],
                           learner_guidelines_version=LEARNER_GUIDELINES_VERSION,
                           mentor_conduct_version=MENTOR_CONDUCT_VERSION,
                           mentor_conduct_document_sha256=MENTOR_CONDUCT_DOCUMENT_SHA256)

def build_users_csv(users, role=None):
	output = io.StringIO()

	fieldnames = [
		"id",
		"username",
		"email",
		"whatsapp",
		"contact_method",
		"contact_value",
		"firstname",
		"lastname",
		"subjects",
		"age",
		"gender",
		"gender_preferred",
		"age_preferred",
		"role",
		"partners",
		"sunday",
		"monday",
		"tuesday",
		"wednesday",
		"thursday",
		"friday",
		"saturday",
	]

	if role in {"mentor", "pending-mentor"}:
		fieldnames += ["school", "mentor_approval_status", "mentor_applied_at", "mentor_approved_at", "mentor_approved_by"]
	if role == "mentor":
		fieldnames += ["missing_recording_count", "recording_flagged"]
		fieldnames += ["missing_recording_" + reason for reason in MISSING_RECORDING_REASONS]
		fieldnames += ["missing_recording_responses"]
		reports = recording_report_summaries()
	else:
		reports = {}
	writer = csv.DictWriter(output, fieldnames=fieldnames)
	writer.writeheader()

	days = [
		"sunday",
		"monday",
		"tuesday",
		"wednesday",
		"thursday",
		"friday",
		"saturday",
	]

	for user in users:
		name = user.get("name", [])
		availability = user.get("availability", {})

		row = {
			"id": user.get("id"),
			"username": user.get("username"),
			"email": account_email(user),
			"whatsapp": account_whatsapp(user),
			"contact_method": user.get("contact_method", "email" if user.get("email") else ""),
			"contact_value": user.get("contact_value", user.get("email", "")),
			"firstname": name[0] if len(name) > 0 else "",
			"lastname": name[1] if len(name) > 1 else "",
			"subjects": " ".join(user.get("subjects", [])),
			"age": user.get("age"),
			"gender": user.get("gender"),
			"gender_preferred": " ".join(user.get("gender_preferred", [])),
			"age_preferred": " ".join(user.get("age_preferred", [])),
			"role": user.get("role"),
			"partners": " ".join(str(partner) for partner in user.get("partners", [])),
		}

		for day in days:
			row[day] = " ".join(availability.get(day, []))

		if role in {"mentor", "pending-mentor"}:
			row["school"] = user.get("school", "")
			row["mentor_approval_status"] = user.get("mentor_approval_status", "approved")
			for field in ("mentor_applied_at", "mentor_approved_at", "mentor_approved_by"):
				row[field] = user.get(field, "")
		if role == "mentor":
			summary = reports.get(user["id"], {})
			row["missing_recording_count"] = summary.get("count", 0)
			row["recording_flagged"] = "yes" if summary.get("flagged") else "no"
			for reason in MISSING_RECORDING_REASONS:
				row["missing_recording_" + reason] = summary.get("reason_counts", {}).get(reason, 0)
			row["missing_recording_responses"] = json.dumps(summary.get("reports", []), ensure_ascii=False)

		writer.writerow(row)

	return output.getvalue()


EXPORT_ROLES = {"mentees": "mentee", "mentors": "mentor"}


def users_for_role(users, role):
	return [user for user in users if str(user.get("role", "")).strip().lower() == role
			and (role != "mentor" or mentor_is_approved(user))]


def pending_mentor_rows(users):
    rows = []
    for user in users:
        if user.get('role') == 'mentor' and user.get('mentor_approval_status') == 'pending':
            row = dict(user)
            applied = user.get('mentor_applied_at')
            row['applied_label'] = datetime.fromisoformat(applied).astimezone(ZoneInfo(app.config['LESSON_TIMEZONE'])).strftime('%d %b %Y') if applied else 'Not recorded'
            rows.append(row)
    return rows


@app.route('/export')
@coordinator_required
def export():
	users = read_users()

	groups = []
	for slug, role in EXPORT_ROLES.items():
		raw = build_users_csv(users_for_role(users, role), role=role)
		rows = list(csv.reader(io.StringIO(raw)))
		groups.append({"slug": slug, "label": slug.title(), "headers": rows[0], "rows": rows[1:], "raw": raw})

	return render_template("table.html", groups=groups, pending_mentors=pending_mentor_rows(users))


@app.route('/export/<group>.csv')
@coordinator_required
def export_csv(group):
	if group not in EXPORT_ROLES and group != 'pending-mentors':
		abort(404)
	users = read_users()
	raw = build_users_csv(pending_mentor_rows(users), role='pending-mentor') if group == 'pending-mentors' else build_users_csv(users_for_role(users, EXPORT_ROLES[group]), role=EXPORT_ROLES[group])
	return send_file(
		io.BytesIO(raw.encode("utf-8-sig")),
		mimetype="text/csv",
		as_attachment=True,
		download_name=f"{group}.csv",
	)


@app.route('/signup-process', methods=['POST'])
def signup_process():
    username = request.form.get("username", "").lower()
    if not username or any(letter not in "abcdefghijklmnopqrstuvwxyz0123456789_" for letter in username):
        return {"error": "1"}
    if request.form.get("role") not in {"mentor", "mentee"}:
        abort(400, description="Choose either mentor or mentee when signing up.")
    if request.form["role"] == "mentor" and (
        request.form.get("mentor_conduct_accepted") != "yes"
        or request.form.get("mentor_conduct_version") != MENTOR_CONDUCT_VERSION
        or request.form.get("mentor_conduct_document_sha256") != MENTOR_CONDUCT_DOCUMENT_SHA256
    ):
        return {"error": "conduct", "message": "Please read and accept the current Mentor Code of Conduct before signing up."}, 400
    if request.form["role"] == "mentee" and (
        request.form.get("learner_guidelines_accepted") != "yes"
        or request.form.get("learner_guidelines_version") != LEARNER_GUIDELINES_VERSION
    ):
        return {"error": "guidelines", "message": "Please read and agree to the learner guide before signing up."}, 400
    email = request.form.get('email', '').strip()
    whatsapp = request.form.get('whatsapp', '').strip()
    if not valid_contact_email(email):
        return {'error': 'contact', 'message': 'Enter a valid email address. Email is required for password recovery and account updates.'}, 400
    if whatsapp:
        whatsapp = normalize_contact_number(whatsapp)
        if not whatsapp:
            return {'error': 'contact', 'message': 'Enter a WhatsApp number with 7–15 digits, or leave it blank.'}, 400
    school = request.form.get('school', '').strip()
    if request.form['role'] == 'mentor' and not 1 <= len(school) <= 150:
        return {'error': 'school', 'message': 'Enter your college or high school name (up to 150 characters).'}, 400
    centre = request.form.get('centre', '').strip()
    if request.form['role'] == 'mentee' and len(centre) > 150:
        return {'error': 'centre', 'message': 'Enter a centre name of up to 150 characters.'}, 400
    try:
        availability = json.loads(request.form["days"])
        subjects = json.loads(request.form["selectedSubjects"])
        gender_preferred = json.loads(request.form["gender_preferred"])
        age_preferred = json.loads(request.form["age_preferred"])
        age = int(request.form["age"])
        if not isinstance(availability, list) or len(availability) != 7:
            raise ValueError()
        if any(not isinstance(day, list) or not all(isinstance(time, str) for time in day) for day in availability):
            raise ValueError()
        for values in [subjects, gender_preferred, age_preferred]:
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError()
        if not request.form["password"]:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        abort(400, description="Please check your signup details and try again.")

    if not subjects or any(subject not in {"maths", "english", "general homework support", "other"} for subject in subjects):
        return {"error": "subjects", "message": "Choose at least one subject from the list."}, 400
    if "other" in subjects:
        other_subjects = request.form.get("other_subjects", "").strip()
        custom_subjects = [" ".join(subject.split()).lower()
                           for subject in other_subjects.replace("\n", ",").split(",") if subject.strip()]
        if not custom_subjects or len(other_subjects) > 200 or "other" in custom_subjects:
            return {"error": "subjects", "message": "List your other subjects (up to 200 characters), separated by commas."}, 400
        # Store actual subject names so profiles, exports, and matching can use them.
        subjects = [subject for subject in subjects if subject != "other"] + custom_subjects
    subjects = list(dict.fromkeys("maths" if subject == "math" else subject for subject in subjects))

    password_hash = ph.hash(request.form["password"])
    with edit_accounts(app.config["USERS_FILE"]) as users:
        if any(user["username"].lower() == username for user in users):
            return {"error": "2"}
        users.append({
            "id": next_account_id(users),
            "username": username, "password": password_hash,
            "name": [request.form["firstname"], request.form["surname"]],
            "subjects": subjects, "age": age, "gender": request.form["gender_own"],
            "gender_preferred": gender_preferred, "age_preferred": age_preferred,
            "role": request.form["role"], "partners": [],
            "email": email, "whatsapp": whatsapp,
            "contact_method": "email", "contact_value": email,
            "availability": dict(zip(
                ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
                availability,
            )),
        })
        if request.form["role"] == "mentor":
            users[-1].update(school=school, mentor_approval_status='pending',
                             mentor_applied_at=datetime.now(timezone.utc).isoformat())
            users[-1]["mentor_code_of_conduct"] = {
                "version": MENTOR_CONDUCT_VERSION,
                "document_sha256": MENTOR_CONDUCT_DOCUMENT_SHA256,
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            users[-1]['centre'] = centre
            users[-1]["learner_guidelines"] = {
                "version": LEARNER_GUIDELINES_VERSION,
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            }
    return {"error": "none"}


@app.get('/coordinator/mentor-approvals')
@coordinator_required
def mentor_approvals():
    return render_template('mentor_approvals.html', pending_mentors=pending_mentor_rows(read_users()))


@app.post('/coordinator/mentors/<int:mentor_id>/approve')
@coordinator_required
def approve_mentor(mentor_id):
    with edit_accounts(app.config['USERS_FILE']) as users:
        mentor = next((user for user in users if user['id'] == mentor_id and user.get('role') == 'mentor'), None)
        if mentor is None:
            abort(404)
        if mentor.get('mentor_approval_status') != 'pending':
            abort(409, description='This mentor is already approved or is not awaiting approval.')
        mentor.update(mentor_approval_status='approved', mentor_approved_by=g.current_user['id'],
                      mentor_approved_at=datetime.now(timezone.utc).isoformat())
    flash('Mentor approved. They now appear in the mentor list and can be paired with a student.', 'success')
    return redirect(url_for('mentor_approvals'))

@app.route('/error1')
def error1():
	return render_template("error.html", error="Your username must consist of English letters, numbers, and underscores (_) only.")

@app.route('/error2')
def error2():
	return render_template("error.html", error="Your chosen username is already taken. Please choose another username.")

@app.route('/login')
def login():
    if g.current_user:
        if not account_email(g.current_user):
            return redirect(url_for('complete_email'))
        return redirect(url_for("coordinator" if g.current_user.get("is_coordinator") else "dashboard"))
    return render_template("login.html")


@app.route('/login-process', methods=['POST'])
def login_process():
    username = request.form.get("username", "").lower()
    user = next((user for user in read_users() if user["username"].lower() == username), None)
    if user:
        try:
            ph.verify(user["password"], request.form.get("password", ""))
        except (argon2.exceptions.VerificationError, argon2.exceptions.InvalidHashError):
            pass
        else:
            session.clear()
            session["user_id"] = user["id"]
            session['data_backend'] = 'supabase' if uses_postgres() else 'local'
            session["auth_version"] = user.get("auth_version", 0)
            session.permanent = True
            if not account_email(user):
                session['require_email'] = True
                return redirect(url_for('complete_email'))
            return redirect(url_for("coordinator" if user.get("is_coordinator") is True else "dashboard"))
    return render_template("error.html", error="The login credentials you entered are incorrect."), 401


@app.route('/account/email', methods=['GET', 'POST'])
@login_required
def complete_email():
    destination = 'coordinator' if g.current_user.get('is_coordinator') is True else 'dashboard'
    if account_email(g.current_user):
        session.pop('require_email', None)
        return redirect(url_for(destination))
    email = request.form.get('email', '').strip()
    whatsapp = request.form.get('whatsapp', account_whatsapp(g.current_user)).strip()
    error = None
    if request.method == 'POST':
        if not valid_contact_email(email):
            error = 'Enter a valid email address for password recovery.'
        elif whatsapp and not normalize_contact_number(whatsapp):
            error = 'Enter a WhatsApp number with 7–15 digits, or leave it blank.'
        else:
            with edit_accounts(app.config['USERS_FILE']) as users:
                user = next(u for u in users if u['id'] == g.current_user['id'])
                try:
                    ph.verify(user['password'], request.form.get('password', ''))
                except (argon2.exceptions.VerificationError, argon2.exceptions.InvalidHashError):
                    error = 'Your current password is incorrect.'
                else:
                    if not account_email(user):
                        user.update(email=email, whatsapp=normalize_contact_number(whatsapp) if whatsapp else '',
                                    contact_method='email', contact_value=email)
                        user.pop('password_reset', None)
            if not error:
                session.pop('require_email', None)
                flash('Email saved. Password reset links will be sent to this address.', 'success')
                return redirect(url_for(destination))
    return render_template('complete_email.html', email=email, whatsapp=whatsapp, error=error), 400 if error else 200


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    session.clear()
    return redirect(url_for("home"))


def linked_ids(user):
    return {str(partner) for partner in user.get("partners", [])}


def pairing_rows(users):
    mentors = users_for_role(users, "mentor")
    mentees = users_for_role(users, "mentee")
    return [
        {"mentor": mentor, "mentee": mentee}
        for mentor in mentors for mentee in mentees
        if str(mentee["id"]) in linked_ids(mentor) or str(mentor["id"]) in linked_ids(mentee)
    ]


@app.route('/dashboard')
@login_required
def dashboard():
    if g.current_user.get('role') == 'mentor' and not mentor_is_approved(g.current_user) and not g.current_user.get('is_coordinator'):
        return render_template('mentor_pending.html')
    users = read_users()
    own_id = str(g.current_user["id"])
    partners = [user for user in users if (user.get('role') != 'mentor' or mentor_is_approved(user)) and str(user["id"]) != own_id and (
        str(user["id"]) in linked_ids(g.current_user) or own_id in linked_ids(user)
    )]
    connections = []
    for partner in partners:
        mentor_id = g.current_user["id"] if g.current_user["role"] == "mentor" else partner["id"]
        mentee_id = partner["id"] if g.current_user["role"] == "mentor" else g.current_user["id"]
        connections.append({"user": partner, "mentor_id": mentor_id, "mentee_id": mentee_id,
                            **room_summary(mentor_id, mentee_id)})
    return render_template("dashboard.html", connections=connections,
                           activity=activity_for_user(g.current_user["id"], {user["id"]: user for user in users}),
                           timezone=app.config["LESSON_TIMEZONE"])


@app.route('/coordinator')
@coordinator_required
def coordinator():
    users = read_users()
    pairs = pairing_rows(users)
    mentors = users_for_role(users, "mentor")
    mentees = users_for_role(users, "mentee")
    paired_ids = {person["id"] for pair in pairs for person in pair.values()}
    return render_template("coordinator.html", mentors=mentors, mentees=mentees, pairs=pairs,
                           pending_mentors=pending_mentor_rows(users),
                           unpaired=sum(user["id"] not in paired_ids for user in mentors + mentees))


@app.route('/coordinator/pairings')
@coordinator_required
def pairings():
    users = read_users()
    # Never serialize password hashes or permissions into browser-side profile data.
    fields = {"id", "username", "name", "role", "subjects", "age", "gender",
              "gender_preferred", "age_preferred", "availability", "partners"}
    profiles = [{key: value for key, value in user.items() if key in fields}
                for user in users if user.get("role") == "mentee" or mentor_is_approved(user)]
    return render_template("pairings.html", mentors=users_for_role(users, "mentor"),
                           mentees=users_for_role(users, "mentee"), pairs=pairing_rows(users), profiles=profiles,
                           matching_output=matching_results(profiles, include_scores=False))


@app.route('/coordinator/pairings', methods=['POST'])
@coordinator_required
def save_pairing():
    action = request.form.get("action")
    if action not in {"link", "unlink"}:
        abort(400, description="Choose a valid pairing action.")
    with edit_accounts(app.config["USERS_FILE"]) as users:
        mentor = next((user for user in users if str(user["id"]) == request.form.get("mentor_id")
                       and user.get("role") == "mentor"), None)
        mentee = next((user for user in users if str(user["id"]) == request.form.get("mentee_id")
                       and user.get("role") == "mentee"), None)
        if mentor is None or mentee is None or mentor["id"] == mentee["id"]:
            abort(400, description="Select an existing mentor and an existing mentee.")
        if action == 'link' and not mentor_is_approved(mentor):
            abort(409, description='Approve this mentor before linking them to a student.')
        already_linked = str(mentee["id"]) in linked_ids(mentor) and str(mentor["id"]) in linked_ids(mentee)
        for person, partner in [(mentor, mentee), (mentee, mentor)]:
            remaining = [value for value in person.get("partners", []) if str(value) != str(partner["id"])]
            person["partners"] = remaining + ([partner["id"]] if action == "link" else [])
    message = "Pairing removed from both accounts." if action == "unlink" else (
        "These accounts are already linked." if already_linked else "Pairing saved. Both accounts are now linked."
    )
    flash(message, "success")
    return redirect(url_for("pairings"))


@app.route("/")
def home():
	return render_template("index.html")


@app.route("/contact")
def contact():
	return render_template("contact.html")


@app.route("/learning-resources")
def learning_resources():
    counts = approved_counts()
    counts['safeguarding-guidance'] = counts.get('safeguarding-guidance', 0) + 1
    return render_template("learning_resources.html", resource_groups=RESOURCE_GROUPS, material_counts=counts)


# Public information pages and the placeholder library use the existing account system.
RESOURCE_GROUPS = {
    "mentors": [("teaching-guides", "Teaching guides"), ("lesson-templates", "Lesson templates"),
                ("safeguarding-guidance", "Safeguarding guidance"), ("mentor-handbook", "Mentor handbook")],
    "students": [("english", "English"), ("maths", "Maths"), ("science", "Science"),
                 ("languages", "Languages"), ("study-skills", "Study skills")],
}
app.config['RESOURCE_FOLDERS'] = {slug: title for folders in RESOURCE_GROUPS.values() for slug, title in folders}
app.config['STUDENT_RESOURCE_FOLDERS'] = tuple(slug for slug, _ in RESOURCE_GROUPS['students'])


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/code-of-conduct")
def code_of_conduct():
    return render_template("code_of_conduct.html", conduct_document=CONDUCT_DOCUMENTS["website"],
                           mentor_conduct_version=MENTOR_CONDUCT_VERSION)


@app.route("/students-and-centres")
def students_centres():
    return render_template("students_centres.html")


@app.route("/volunteer")
def volunteer():
    return render_template("volunteer.html")


@app.route("/learning-resources/<folder>")
@login_required
def resource_folder(folder):
    for audience, folders in RESOURCE_GROUPS.items():
        for slug, title in folders:
            if slug == folder:
                response = app.make_response(render_template("resource_folder.html", folder_title=title, audience=audience,
                                                            folder_slug=slug, materials=approved_materials(slug),
                                                            can_submit_materials=slug in submission_folders(g.current_user)))
                response.headers["Cache-Control"] = "no-store"
                return response
    abort(404)


if __name__ == "__main__":
	app.run(debug=True, port=5001)

#python3 app.py
