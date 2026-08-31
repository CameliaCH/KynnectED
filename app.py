from flask import Flask, render_template, request, redirect, send_file
import json, argon2, sys, csv, io
from datetime import datetime, timedelta

ph = argon2.PasswordHasher()

app = Flask(__name__)

app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.route('/signup')
def signup():
	return render_template("signup.html")

@app.route('/export')
def export():
	with open("users.json", "r") as f:
		users = json.load(f)

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
			"partners": " ".join(user.get("partners", [])),
		}

		for day in days:
			row[day] = " ".join(availability.get(day, []))

		writer.writerow(row)

	resulting_csv = output.getvalue()
	
	reader = csv.reader(io.StringIO(resulting_csv))
	rows = list(reader)

	headers = rows[0]
	data = rows[1:]

	return render_template("table.html", headers=headers, rows=data, raw=resulting_csv)

@app.route('/signup-process', methods=['POST'])
def signup_process():
	with open("users.json", "r+") as f:
		data = json.load(f)
		for i in request.form["username"]:
			if i.lower() not in "abcdefghijklmnopqrstuvwxyz0123456789_":
				return {"error": "1"}
		for i in data:
			if request.form["username"].lower() == i["username"].lower():
				return {"error": "2"}
		data.append({"id": data[-1]["id"]+1, "username": request.form["username"].lower(), "password": ph.hash(request.form["password"]), "name": [request.form["firstname"], request.form["surname"]], "subjects": json.loads(request.form["selectedSubjects"]), "age": int(request.form["age"]), "gender": request.form["gender_own"], "gender_preferred": json.loads(request.form["gender_preferred"]), "age_preferred": json.loads(request.form["age_preferred"]), "role": request.form["role"], "partners": [], "availability": {"sunday": json.loads(request.form["days"])[0], "monday": json.loads(request.form["days"])[1], "tuesday": json.loads(request.form["days"])[2], "wednesday": json.loads(request.form["days"])[3], "thursday": json.loads(request.form["days"])[4], "friday": json.loads(request.form["days"])[5], "saturday": json.loads(request.form["days"])[6]}})
		
	with open("users.json", "w") as f:
		json.dump(data, f, indent=4)
	return {"error": "none"}

@app.route('/error1')
def error1():
	return render_template("error.html", error="Your username must consist of English letters, numbers, and underscores (_) only.")

@app.route('/error2')
def error2():
	return render_template("error.html", error="Your chosen username is already taken. Please choose another username.")

@app.route('/login')
def login():
	return render_template("login.html")

@app.route('/login-process', methods=['POST'])
def login_process():
	with open("users.json", "r") as f:
		data = json.load(f)
		for i in data:
			if request.form["username"].lower() == i["username"].lower():
				try:
					ph.verify(i["password"], request.form["password"])
					return f"Successfuly logged in as {request.form['username'].lower()}"
				except argon2.exceptions.VerifyMismatchError:
					return render_template("error.html", error="The login credentials you entered are incorrect.")
		
	return render_template("error.html", error="The login credentials you entered are incorrect.")


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