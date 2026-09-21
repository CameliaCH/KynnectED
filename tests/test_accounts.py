import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from app import app, ph
from account_store import edit_accounts, read_accounts


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = ph.hash("test-password-only")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "users.json"
        original_config = {key: app.config[key] for key in ["USERS_FILE", "TESTING", "LEARNING_DATABASE", "RECORDINGS_DIR"]}
        self.addCleanup(lambda: app.config.update(original_config))
        app.config.update(USERS_FILE=str(self.path), TESTING=True,
                          LEARNING_DATABASE=str(self.path.parent / "learning.sqlite3"),
                          RECORDINGS_DIR=str(self.path.parent / "recordings"))
        self.users = [self.account(1, "mentor"), self.account(2, "mentor"),
                      self.account(3, "mentee"), self.account(4, "mentee"), self.account(99, "mentor")]
        self.users[-1]["is_coordinator"] = True
        self.path.write_text(json.dumps(self.users))
        self.client = app.test_client()

    def account(self, id, role):
        return {"id": id, "username": f"person{id}", "password": self.password_hash,
                "name": [f"Person{id}", "Example"], "role": role, "subjects": ["maths"],
                "age": 18, "gender": "male", "gender_preferred": ["same"],
                "age_preferred": ["same-age"], "partners": [], "availability": {"monday": ["09:00"]}}

    def authenticate(self, id=99):
        with self.client.session_transaction() as session:
            session["user_id"] = id
            session["csrf_token"] = "test-csrf"

    def post_pair(self, mentor=1, mentee=3, action="link", **extra):
        return self.client.post("/coordinator/pairings", data={
            "mentor_id": mentor, "mentee_id": mentee, "action": action,
            "_csrf_token": "test-csrf", **extra,
        })

    def test_coordinator_routes_require_login_and_permission(self):
        paths = ["/coordinator", "/coordinator/pairings", "/export", "/export/mentors.csv", "/export/mentees.csv"]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 302)
        self.authenticate(1)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
        before = self.path.read_bytes()
        self.assertEqual(self.post_pair().status_code, 403)
        self.assertEqual(before, self.path.read_bytes())

    def test_login_redirects_and_logout_clears_session(self):
        for id, destination in [(99, "/coordinator"), (1, "/dashboard")]:
            self.client.get("/login")
            with self.client.session_transaction() as session:
                token = session["csrf_token"]
            response = self.client.post("/login-process", data={
                "username": f"PERSON{id}", "password": "test-password-only", "_csrf_token": token,
            })
            self.assertEqual(response.location, destination)
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])
            self.assertEqual(self.client.get(destination).status_code, 200)
            with self.client.session_transaction() as session:
                self.assertEqual(session["user_id"], id)
                token = session["csrf_token"]
            self.assertEqual(self.client.post("/logout", data={"_csrf_token": token}).status_code, 302)
            self.assertEqual(self.client.get("/dashboard").location, "/login")

    def test_invalid_login_and_csrf_do_not_authenticate(self):
        self.assertEqual(self.client.post("/login-process", data={"username": "person99"}).status_code, 400)
        self.client.get("/login")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        for username in ["person99", "no-such-account"]:
            response = self.client.post("/login-process", data={
                "username": username, "password": "incorrect", "_csrf_token": token,
            })
            self.assertEqual(response.status_code, 401)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_many_to_many_links_persist_on_both_accounts(self):
        self.authenticate()
        for mentor, mentee in [(1, 3), (1, 4), (2, 3), (1, 3)]:
            self.assertEqual(self.post_pair(mentor, mentee).status_code, 302)
        accounts = {user["id"]: user for user in read_accounts(self.path)}
        self.assertEqual(accounts[1]["partners"], [4, 3])
        self.assertEqual(accounts[2]["partners"], [3])
        self.assertEqual(accounts[3]["partners"], [2, 1])
        self.assertEqual(accounts[4]["partners"], [1])
        self.assertEqual(accounts[1]["password"], self.password_hash)
        html = self.client.get("/coordinator/pairings").get_data(as_text=True)
        self.assertEqual(html.count('name="action" value="unlink"'), 3)
        self.assertNotIn(self.password_hash, html)
        self.assertNotIn('"is_coordinator":', html)
        rows = list(csv.DictReader(io.StringIO(self.client.get("/export/mentees.csv").data.decode("utf-8-sig"))))
        self.assertEqual(next(row for row in rows if row["id"] == "3")["partners"], "2 1")

    def test_unlink_preserves_other_connections(self):
        self.authenticate()
        for pair in [(1, 3), (1, 4), (2, 3)]:
            self.post_pair(*pair)
        self.assertEqual(self.post_pair(1, 3, "unlink").status_code, 302)
        accounts = {user["id"]: user for user in read_accounts(self.path)}
        self.assertEqual(accounts[1]["partners"], [4])
        self.assertEqual(accounts[3]["partners"], [2])
        self.assertEqual(accounts[2]["partners"], [3])
        self.assertEqual(accounts[4]["partners"], [1])
        self.post_pair(1, 3, "unlink")
        self.assertEqual(accounts, {user["id"]: user for user in read_accounts(self.path)})

    def test_bad_pairing_requests_leave_accounts_unchanged(self):
        self.authenticate()
        before = self.path.read_bytes()
        for mentor, mentee in [(1, 1), (1, 2), (3, 1), (1, 500), (500, 3), ("invalid", 3)]:
            self.assertEqual(self.post_pair(mentor, mentee).status_code, 400)
        self.assertEqual(self.post_pair(action="invalid").status_code, 400)
        self.assertEqual(self.post_pair(_csrf_token="wrong").status_code, 400)
        self.assertEqual(self.post_pair(_csrf_token="invalid-✓").status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_member_dashboard_only_displays_their_connections(self):
        self.authenticate()
        self.post_pair(1, 3)
        self.post_pair(2, 4)
        self.authenticate(3)
        response = self.client.get("/dashboard")
        html = response.get_data(as_text=True)
        self.assertIn("@person1", html)
        self.assertNotIn("@person2", html)
        self.assertNotIn("@person4", html)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_cli_grant_and_revoke_apply_to_existing_sessions(self):
        self.authenticate(1)
        runner = app.test_cli_runner()
        self.assertEqual(runner.invoke(args=["set-coordinator", "PERSON1"]).exit_code, 0)
        self.assertEqual(self.client.get("/coordinator").status_code, 200)
        self.assertEqual(runner.invoke(args=["set-coordinator", "person1", "--revoke"]).exit_code, 0)
        self.assertEqual(self.client.get("/coordinator").status_code, 403)
        self.assertNotEqual(runner.invoke(args=["set-coordinator", "missing"]).exit_code, 0)

    def test_signup_cannot_grant_permissions_and_preserves_pairings(self):
        self.authenticate()
        self.post_pair(1, 3)
        data = {"username": "newmember", "firstname": "New", "surname": "Member", "age": "18",
                "password": "signup-test", "role": "mentee", "selectedSubjects": '["maths"]',
                "gender_own": "male", "gender_preferred": '["same"]', "age_preferred": '["same-age"]',
                "days": json.dumps([["empty"]] * 7), "_csrf_token": "test-csrf", "is_coordinator": "true"}
        self.assertEqual(self.client.post("/signup-process", data=data).json, {"error": "none"})
        users = read_accounts(self.path)
        self.assertEqual(users[0]["partners"], [3])
        self.assertEqual(users[2]["partners"], [1])
        self.assertNotIn("is_coordinator", users[-1])
        data.update(username="adminattempt", role="coordinator")
        self.assertEqual(self.client.post("/signup-process", data=data).status_code, 400)
        self.assertEqual(read_accounts(self.path), users)

    def test_pairing_repairs_legacy_string_ids_without_duplicates(self):
        with edit_accounts(self.path) as users:
            users[0]["partners"] = ["3"]
        self.authenticate()
        self.post_pair()
        users = read_accounts(self.path)
        self.assertEqual(users[0]["partners"], [3])
        self.assertEqual(users[2]["partners"], [1])

    def test_failed_store_update_does_not_overwrite_file(self):
        before = copy.deepcopy(read_accounts(self.path))
        with self.assertRaises(ValueError):
            with edit_accounts(self.path) as users:
                users[0]["partners"] = [3]
                raise ValueError("cancel this edit")
        self.assertEqual(read_accounts(self.path), before)


if __name__ == "__main__":
    unittest.main()
