from flask import Flask, render_template

app = Flask(__name__)


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