from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import app
from account_store import edit_accounts
from learning import activity_for_user, get_db, probe_video


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        keys = ["TESTING", "USERS_FILE", "LEARNING_DATABASE", "RECORDINGS_DIR", "MAX_CONTENT_LENGTH"]
        original = {key: app.config[key] for key in keys}
        self.addCleanup(lambda: app.config.update(original))
        app.config.update(TESTING=True, USERS_FILE=str(self.root / "users.json"),
                          LEARNING_DATABASE=str(self.root / "learning.sqlite3"),
                          RECORDINGS_DIR=str(self.root / "recordings"), MAX_CONTENT_LENGTH=1024 * 1024)
        users = []
        for id, role, partners in [(1, "mentor", [3, 4]), (2, "mentor", [3]), (3, "mentee", [1, 2]),
                                   (4, "mentee", [1]), (99, "mentor", [])]:
            users.append({"id": id, "username": f"person{id}", "name": [f"Person{id}", "Example"],
                          "role": role, "partners": partners, "subjects": ["maths"], "is_coordinator": id == 99})
        Path(app.config["USERS_FILE"]).write_text(json.dumps(users))
        self.client = app.test_client()
        self.auth(1)

    def auth(self, id):
        with self.client.session_transaction() as session:
            session.clear()
            if id:
                session["user_id"] = id
            session["csrf_token"] = "test-csrf"

    def form(self, **changes):
        future = datetime.now(ZoneInfo("Asia/Kuala_Lumpur")) + timedelta(days=2)
        return {"_csrf_token": "test-csrf", "title": "Algebra practice", "scheduled_at": future.strftime("%Y-%m-%dT%H:%M"),
                "planned_minutes": "60", "zoom_url": "https://us06web.zoom.us/j/123456789?pwd=example", **changes}

    def create(self, mentor=1, mentee=3, past=False, **changes):
        if past:
            changes["scheduled_at"] = (datetime.now(ZoneInfo("Asia/Kuala_Lumpur")) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(f"/pairings/{mentor}/{mentee}/lessons", data=self.form(**changes))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        with app.app_context():
            return dict(get_db().execute("SELECT * FROM lessons ORDER BY rowid DESC LIMIT 1").fetchone())

    def lesson(self, id):
        with app.app_context():
            return dict(get_db().execute("SELECT * FROM lessons WHERE id = ?", (id,)).fetchone())

    def upload(self, id, seconds=3600):
        with patch("learning.probe_video", return_value=seconds):
            return self.client.post(f"/lessons/{id}/recording", data={
                "_csrf_token": "test-csrf", "recording": (io.BytesIO(b"test-video-content"), "../../recording.mp4")},
                content_type="multipart/form-data")

    def test_room_and_management_permissions(self):
        self.auth(None)
        self.assertEqual(self.client.get("/pairings/1/3").location, "/login")
        self.assertEqual(self.client.get("/pairings/1/3/messages").status_code, 401)
        self.auth(2)
        self.assertEqual(self.client.get("/pairings/1/3").status_code, 403)
        self.assertEqual(self.client.post("/pairings/1/3/lessons", data=self.form()).status_code, 403)
        self.auth(3)
        html = self.client.get("/pairings/1/3").get_data(as_text=True)
        self.assertIn("Pairing chat", html)
        self.assertNotIn("Add a lesson", html)
        self.assertEqual(self.client.post("/pairings/1/3/lessons", data=self.form()).status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.get("/pairings/1/3").status_code, 200)
        self.assertEqual(self.client.get("/coordinator/lessons").status_code, 200)

    def test_schedule_visible_to_both_participants_only(self):
        lesson = self.create()
        for id in [1, 3]:
            self.auth(id)
            html = self.client.get("/dashboard").get_data(as_text=True)
            self.assertIn("Algebra practice", html)
            self.assertIn('/pairings/1/3', html)
            self.assertIn("Join Zoom", html)
        self.auth(4)
        self.assertNotIn("Algebra practice", self.client.get("/dashboard").get_data(as_text=True))
        self.auth(1)
        self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/edit', data=self.form(title="Updated algebra")).status_code, 302)
        self.assertEqual(self.lesson(lesson["id"])["title"], "Updated algebra")

    def test_conflicts_across_many_to_many_pairings_and_adjacent_slots(self):
        lesson = self.create()
        data = self.form(scheduled_at=datetime.fromtimestamp(lesson["scheduled_at"], ZoneInfo("Asia/Kuala_Lumpur")).strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(self.client.post('/pairings/1/4/lessons', data=data).status_code, 409)
        self.auth(2)
        self.assertEqual(self.client.post('/pairings/2/3/lessons', data=data).status_code, 409)
        data["scheduled_at"] = datetime.fromtimestamp(lesson["scheduled_at"] + 3600, ZoneInfo("Asia/Kuala_Lumpur")).strftime("%Y-%m-%dT%H:%M")
        self.assertEqual(self.client.post('/pairings/2/3/lessons', data=data).status_code, 302)

    def test_invalid_schedule_and_zoom_links(self):
        for url in ["javascript:alert(1)", "https://zoom.us.evil.example/j/1", "https://zoom.us@evil.example/j/1", "http://zoom.us/j/1"]:
            self.assertEqual(self.client.post('/pairings/1/3/lessons', data=self.form(zoom_url=url)).status_code, 400)
        for change in [{"title": " "}, {"scheduled_at": "invalid"}, {"planned_minutes": "0"}, {"planned_minutes": "241"}, {"_csrf_token": "wrong"}]:
            self.assertEqual(self.client.post('/pairings/1/3/lessons', data=self.form(**change)).status_code, 400)

    def test_cancelled_lessons_do_not_count_as_upcoming_or_attended(self):
        lesson = self.create()
        self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/cancel', data={"_csrf_token": "test-csrf"}).status_code, 302)
        with app.app_context():
            activity = activity_for_user(1)
        self.assertEqual(activity, {"hours": 0, "completed": 0, "upcoming": []})
        self.assertEqual(self.upload(lesson["id"]).status_code, 409)
        self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/edit', data=self.form()).status_code, 409)

    def test_upload_marks_both_present_and_replacement_does_not_double_count(self):
        lesson = self.create(past=True)
        self.assertEqual(self.upload(lesson["id"]).status_code, 302)
        first = self.lesson(lesson["id"])
        for id in [1, 3]:
            with app.app_context():
                activity = activity_for_user(id)
            self.assertEqual((activity["hours"], activity["completed"]), (1, 1))
        with app.app_context():
            self.assertEqual(activity_for_user(4)["hours"], 0)
        self.assertEqual(self.upload(lesson["id"], 1800).status_code, 302)
        second = self.lesson(lesson["id"])
        with app.app_context():
            activity = activity_for_user(3)
        self.assertEqual((activity["hours"], activity["completed"]), (0.5, 1))
        self.assertNotEqual(first["recording_filename"], second["recording_filename"])
        self.assertFalse((Path(app.config["RECORDINGS_DIR"]) / first["recording_filename"]).exists())
        self.assertEqual(len(list(Path(app.config["RECORDINGS_DIR"]).iterdir())), 1)
        self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/cancel', data={"_csrf_token": "test-csrf"}).status_code, 409)

    def test_playback_download_and_range_requests_are_private(self):
        lesson = self.create(past=True)
        self.upload(lesson["id"])
        path = f'/lessons/{lesson["id"]}/recording'
        self.auth(3)
        response = self.client.get(path, headers={"Range": "bytes=0-3"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"test")
        self.assertEqual(response.mimetype, "video/mp4")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response.close()
        download = self.client.get(path + '?download=1')
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()
        self.assertEqual(self.upload(lesson["id"]).status_code, 403)
        self.auth(2)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.auth(99)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_bad_uploads_do_not_mark_attendance_or_destroy_previous_recording(self):
        future = self.create()
        self.assertEqual(self.upload(future["id"]).status_code, 409)
        lesson = self.create(past=True)
        for filename, content in [('malware.html', b'<script>x</script>'), ('fake.mp4', b'not video'), ('empty.mp4', b'')]:
            response = self.client.post(f'/lessons/{lesson["id"]}/recording', data={"_csrf_token": "test-csrf", "recording": (io.BytesIO(content), filename)})
            self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.lesson(lesson["id"])["recorded_at"])
        self.upload(lesson["id"])
        before = self.lesson(lesson["id"])
        response = self.client.post(f'/lessons/{lesson["id"]}/recording', data={"_csrf_token": "test-csrf", "recording": (io.BytesIO(b'invalid'), 'bad.mp4')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.lesson(lesson["id"]), before)

    def test_upload_limit_is_enforced(self):
        lesson = self.create(past=True)
        app.config["MAX_CONTENT_LENGTH"] = 128
        response = self.client.post(f'/lessons/{lesson["id"]}/recording',
                                    headers={"X-CSRF-Token": "test-csrf"},
                                    data={"recording": (io.BytesIO(b'x' * 1024), 'large.mp4')})
        self.assertEqual(response.status_code, 413)
        self.assertIsNone(self.lesson(lesson["id"])["recorded_at"])

    def test_ajax_upload_preserves_success_notice_for_room_reload(self):
        lesson = self.create(past=True)
        with patch("learning.probe_video", return_value=60):
            response = self.client.post(f'/lessons/{lesson["id"]}/recording',
                headers={"X-CSRF-Token": "test-csrf", "X-Requested-With": "XMLHttpRequest"},
                data={"recording": (io.BytesIO(b"video"), "zoom.mp4")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["redirect"], '/pairings/1/3#recordings')
        html = self.client.get('/pairings/1/3').get_data(as_text=True)
        self.assertIn('Recording uploaded. Attendance and learning hours', html)
        self.assertIn('<video', html)

    def test_quality_review_is_private_and_reset_on_replacement(self):
        lesson = self.create(past=True)
        self.upload(lesson["id"])
        data = {"_csrf_token": "test-csrf", "review_note": "Private quality note", "recording_version": self.lesson(lesson["id"])["recording_filename"]}
        path = f'/lessons/{lesson["id"]}/review'
        self.assertEqual(self.client.post(path, data=data).status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertIn("Private quality note", self.client.get('/pairings/1/3').get_data(as_text=True))
        self.auth(3)
        self.assertNotIn("Private quality note", self.client.get('/pairings/1/3').get_data(as_text=True))
        self.auth(1)
        self.upload(lesson["id"], 1800)
        self.assertIsNone(self.lesson(lesson["id"])["reviewed_at"])
        self.auth(99)
        self.assertEqual(self.client.post(path, data=data).status_code, 409)

    def test_chat_persists_escapes_html_and_enforces_pair_access(self):
        path = '/pairings/1/3/messages'
        response = self.client.post(path, data={"_csrf_token": "test-csrf", "message": "<script>alert(1)</script>", "sender_id": "99"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["messages"][0]["sender_id"], 1)
        self.auth(3)
        self.assertEqual(len(self.client.get(path).json["messages"]), 1)
        html = self.client.get('/pairings/1/3').get_data(as_text=True)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.auth(2)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(path, data={"_csrf_token": "test-csrf", "message": "unauthorized"}).status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(path, data={"_csrf_token": "test-csrf", "message": "coordinator"}).status_code, 403)

    def test_chat_pagination_and_validation(self):
        path = '/pairings/1/3/messages'
        for i in range(55):
            self.client.post(path, data={"_csrf_token": "test-csrf", "message": str(i)})
        recent = self.client.get(path).json["messages"]
        self.assertEqual(len(recent), 50)
        earlier = self.client.get(path + '?before=' + str(recent[0]["id"])).json["messages"]
        self.assertEqual([item["body"] for item in earlier], ['0', '1', '2', '3', '4'])
        self.assertEqual(self.client.get(path + '?after=' + str(recent[-1]["id"])).json["messages"], [])
        for message in ['', ' ', 'x' * 2001]:
            self.assertEqual(self.client.post(path, data={"_csrf_token": "test-csrf", "message": message}).status_code, 400)
        self.assertEqual(self.client.get(path + '?after=invalid').status_code, 400)

    def test_unlink_revokes_room_access_but_keeps_learning_history(self):
        lesson = self.create(past=True)
        self.upload(lesson["id"])
        with edit_accounts(app.config["USERS_FILE"]) as users:
            users[0]["partners"].remove(3)
            users[2]["partners"].remove(1)
        self.assertEqual(self.client.get('/pairings/1/3').status_code, 403)
        self.assertEqual(self.client.get('/pairings/1/3/messages').status_code, 403)
        self.assertEqual(self.client.get(f'/lessons/{lesson["id"]}/recording').status_code, 403)
        with app.app_context():
            self.assertEqual(activity_for_user(1)["hours"], 1)
        self.auth(99)
        self.assertIn('This pairing is inactive', self.client.get('/pairings/1/3').get_data(as_text=True))
        self.assertEqual(self.client.post('/pairings/1/3/lessons', data=self.form()).status_code, 403)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is needed for the real-video check')
    def test_real_video_validation_and_duration(self):
        path = self.root / 'actual.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:d=2',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
        with app.app_context():
            self.assertAlmostEqual(probe_video(path, '.mp4'), 2, delta=0.1)
        lesson = self.create(past=True)
        response = self.client.post(f'/lessons/{lesson["id"]}/recording',
                                    data={"_csrf_token": "test-csrf", "recording": (io.BytesIO(path.read_bytes()), 'zoom.mp4')})
        self.assertEqual(response.status_code, 302)
        self.assertAlmostEqual(self.lesson(lesson["id"])["recording_seconds"], 2, delta=0.1)


if __name__ == '__main__':
    unittest.main()
