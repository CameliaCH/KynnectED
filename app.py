from flask import Flask, render_template, request, redirect
import json, argon2, sys
from datetime import datetime, timedelta

ph = argon2.PasswordHasher()

app = Flask(__name__)

app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.route('/signup')
def signup():
	return render_template("signup.html")

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