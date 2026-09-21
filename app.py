from flask import Flask, render_template, request, redirect, send_file, abort, session, g, url_for, flash
import json, argon2, csv, io, os, secrets, hmac
from datetime import timedelta
from functools import wraps
from pathlib import Path
import click
from account_store import read_accounts, edit_accounts, session_secret
from learning import learning, activity_for_user, room_summary

ph = argon2.PasswordHasher()

app = Flask(__name__)

app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SECRET_KEY=session_secret(app.instance_path),
    USERS_FILE=os.environ.get("KYNNECTED_USERS_FILE", str(Path(__file__).with_name("users.json"))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("KYNNECTED_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    LEARNING_DATABASE=os.environ.get("KYNNECTED_LEARNING_DATABASE", str(Path(app.instance_path) / "learning.sqlite3")),
    RECORDINGS_DIR=os.environ.get("KYNNECTED_RECORDINGS_DIR", str(Path(app.instance_path) / "recordings")),
    LESSON_TIMEZONE="Asia/Kuala_Lumpur",
    MENTEES_MANAGE_LESSONS=False,
    MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
    FFPROBE_BIN=os.environ.get("FFPROBE_BIN", "ffprobe"),
)
app.register_blueprint(learning)


def read_users():
    return read_accounts(app.config["USERS_FILE"])


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def load_current_user():
    g.current_user = None
    if "user_id" in session:
        g.current_user = next((user for user in read_users() if user["id"] == session["user_id"]), None)
        if g.current_user is None:
            session.clear()
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        if not expected:
            abort(400, description="This form has expired. Reload the page and try again.")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")):
            abort(400, description="This form has expired. Reload the page and try again.")


@app.context_processor
def account_context():
    return {"current_user": g.current_user, "csrf_token": csrf_token}


@app.after_request
def protect_account_responses(response):
    if request.endpoint in {"dashboard", "coordinator", "pairings", "export", "export_csv"}:
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
        return render_template("error.html", error="This upload is too large. The recording limit is 1 GB."), 413
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
	return render_template("signup.html")

def build_users_csv(users):
	output = io.StringIO()

	fieldnames = [
		"id",
		"username",
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

		writer.writerow(row)

	return output.getvalue()


EXPORT_ROLES = {"mentees": "mentee", "mentors": "mentor"}


def users_for_role(users, role):
	return [user for user in users if str(user.get("role", "")).strip().lower() == role]


@app.route('/export')
@coordinator_required
def export():
	users = read_users()

	groups = []
	for slug, role in EXPORT_ROLES.items():
		raw = build_users_csv(users_for_role(users, role))
		rows = list(csv.reader(io.StringIO(raw)))
		groups.append({"slug": slug, "label": slug.title(), "headers": rows[0], "rows": rows[1:], "raw": raw})

	return render_template("table.html", groups=groups)


@app.route('/export/<group>.csv')
@coordinator_required
def export_csv(group):
	if group not in EXPORT_ROLES:
		abort(404)
	users = read_users()
	raw = build_users_csv(users_for_role(users, EXPORT_ROLES[group]))
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

    password_hash = ph.hash(request.form["password"])
    with edit_accounts(app.config["USERS_FILE"]) as users:
        if any(user["username"].lower() == username for user in users):
            return {"error": "2"}
        users.append({
            "id": max((user["id"] for user in users), default=0) + 1,
            "username": username, "password": password_hash,
            "name": [request.form["firstname"], request.form["surname"]],
            "subjects": subjects, "age": age, "gender": request.form["gender_own"],
            "gender_preferred": gender_preferred, "age_preferred": age_preferred,
            "role": request.form["role"], "partners": [],
            "availability": dict(zip(
                ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
                availability,
            )),
        })
    return {"error": "none"}

@app.route('/error1')
def error1():
	return render_template("error.html", error="Your username must consist of English letters, numbers, and underscores (_) only.")

@app.route('/error2')
def error2():
	return render_template("error.html", error="Your chosen username is already taken. Please choose another username.")

@app.route('/login')
def login():
    if g.current_user:
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
            session.permanent = True
            return redirect(url_for("coordinator" if user.get("is_coordinator") is True else "dashboard"))
    return render_template("error.html", error="The login credentials you entered are incorrect."), 401


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
    users = read_users()
    own_id = str(g.current_user["id"])
    partners = [user for user in users if str(user["id"]) != own_id and (
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
                           unpaired=sum(user["id"] not in paired_ids for user in mentors + mentees))


@app.route('/coordinator/pairings')
@coordinator_required
def pairings():
    users = read_users()
    # Never serialize password hashes or permissions into browser-side profile data.
    fields = {"id", "username", "name", "role", "subjects", "age", "gender",
              "gender_preferred", "age_preferred", "availability", "partners"}
    profiles = [{key: value for key, value in user.items() if key in fields}
                for user in users if user.get("role") in {"mentor", "mentee"}]
    return render_template("pairings.html", mentors=users_for_role(users, "mentor"),
                           mentees=users_for_role(users, "mentee"), pairs=pairing_rows(users), profiles=profiles)


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
	return render_template("learning_resources.html")


if __name__ == "__main__":
	app.run(debug=True, port=5001)

#python3 app.py
