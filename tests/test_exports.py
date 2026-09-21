import csv
import io
import json
import unittest
from pathlib import Path
import tempfile

from app import app


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "users.json"
        original_config = {key: app.config[key] for key in ["USERS_FILE", "TESTING", "LEARNING_DATABASE", "RECORDINGS_DIR"]}
        self.addCleanup(lambda: app.config.update(original_config))
        app.config.update(USERS_FILE=str(self.path), TESTING=True,
                          LEARNING_DATABASE=str(self.path.parent / "learning.sqlite3"),
                          RECORDINGS_DIR=str(self.path.parent / "recordings"))
        self.admin = {"id": 99, "username": "coordinator", "role": "staff", "is_coordinator": True}
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = 99
        self.users = [
            {
                "id": 1, "username": "sample_mentee", "role": "mentee",
                "name": ['Zoë, "Z"', "Line\nTwo"], "subjects": ["maths", "art"],
                "password": "never-export-this-hash", "partners": [2],
                "availability": {"monday": ["09:00", "15:30"], "tuesday": ["empty"]},
            },
            {"id": 2, "username": "sample_mentor", "role": "mentor", "name": ["Lee", "Tan"]},
            {"id": 3, "username": "unknown_role", "role": "unknown"},
        ]

    def get_with_users(self, path, users=None):
        data = self.users if users is None else users
        self.path.write_text(json.dumps(data + [self.admin]))
        before = self.path.read_bytes()
        response = self.client.get(path)
        self.assertEqual(before, self.path.read_bytes())
        return response

    def rows(self, response):
        return list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))

    def test_downloads_separate_roles_and_exclude_passwords(self):
        for group, expected in [("mentees", "sample_mentee"), ("mentors", "sample_mentor")]:
            with self.subTest(group=group):
                response = self.get_with_users(f"/export/{group}.csv")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "text/csv")
                self.assertIn(f"attachment; filename={group}.csv", response.headers["Content-Disposition"])
                rows = self.rows(response)
                self.assertEqual([row["username"] for row in rows], [expected])
                self.assertNotIn("password", rows[0])
                self.assertNotIn(b"never-export-this-hash", response.data)

    def test_csv_preserves_quoted_unicode_names_and_availability(self):
        row = self.rows(self.get_with_users("/export/mentees.csv"))[0]
        self.assertEqual(row["firstname"], 'Zoë, "Z"')
        self.assertEqual(row["lastname"], "Line\nTwo")
        self.assertEqual(row["subjects"], "maths art")
        self.assertEqual(row["partners"], "2")
        self.assertEqual(row["monday"], "09:00 15:30")
        self.assertEqual(row["tuesday"], "empty")
        self.assertEqual(row["wednesday"], "")

    def test_empty_role_still_downloads_headers(self):
        response = self.get_with_users("/export/mentors.csv", [self.users[0]])
        reader = csv.DictReader(io.StringIO(response.data.decode("utf-8-sig")))
        self.assertIn("username", reader.fieldnames)
        self.assertIn("saturday", reader.fieldnames)
        self.assertEqual(list(reader), [])

    def test_page_displays_separate_groups_and_download_links(self):
        html = self.get_with_users("/export").get_data(as_text=True)
        mentees, mentors = html.split('id="mentees-heading"')[1].split('id="mentors-heading"')
        self.assertIn("sample_mentee", mentees)
        self.assertNotIn("sample_mentor", mentees)
        self.assertIn("sample_mentor", mentors)
        self.assertNotIn("sample_mentee", mentors)
        self.assertIn('/export/mentees.csv', html)
        self.assertIn('/export/mentors.csv', html)
        self.assertNotIn("unknown_role", html)

    def test_empty_page_and_invalid_group(self):
        html = self.get_with_users("/export", []).get_data(as_text=True)
        self.assertIn("No mentees registered yet.", html)
        self.assertIn("No mentors registered yet.", html)
        self.assertEqual(self.client.get("/export/unknown.csv").status_code, 404)


if __name__ == "__main__":
    unittest.main()
