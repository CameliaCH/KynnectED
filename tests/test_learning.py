from datetime import datetime, timedelta
import csv
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import app
from account_store import edit_accounts
from learning import activity_for_user, get_db, probe_video, recording_report_summaries, MISSING_RECORDING_REASONS, SCHEMA


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

    def seed_legacy_recording(self, id, seconds=3600):
        """Existing disk recordings remain readable after moving new uploads to R2."""
        directory = Path(app.config['RECORDINGS_DIR'])
        directory.mkdir(exist_ok=True)
        previous = self.lesson(id)['recording_filename']
        filename = uuid4().hex + '.mp4'
        (directory / filename).write_bytes(b'test-video-content')
        if previous:
            (directory / previous).unlink(missing_ok=True)
        with app.app_context(), get_db() as db:
            db.execute('UPDATE lessons SET recording_filename = ?, recording_original = ?, recording_mimetype = ?, '
                       'recording_seconds = ?, recorded_at = ?, uploaded_by = 1, reviewed_at = NULL, reviewed_by = NULL, '
                       'review_note = NULL WHERE id = ?',
                       (filename, 'recording.mp4', 'video/mp4', seconds, int(datetime.now().timestamp()), id))

    def report(self, lesson_id, reason='forgot_to_record', note='', **changes):
        return self.client.post(f'/lessons/{lesson_id}/missing-recording', data={
            '_csrf_token': 'test-csrf', 'reason': reason, 'note': note, **changes})

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
        self.assertEqual(activity, {"hours": 0, "completed": 0, "upcoming": [], "finished": []})
        self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/edit', data=self.form()).status_code, 409)

    def test_lesson_moves_to_finished_at_scheduled_end_without_marking_attendance(self):
        lesson = self.create()
        end = lesson['scheduled_at'] + lesson['planned_minutes'] * 60
        for now, finished in ((lesson['scheduled_at'] - 1, False),
                              (lesson['scheduled_at'], False), (end - 1, False),
                              (end, True), (end + 1, True)):
            with self.subTest(now=now), patch('learning.time', **{'time.return_value': now}):
                with app.app_context():
                    activity = activity_for_user(1)
                self.assertEqual([item['id'] for item in activity['finished']], [lesson['id']] if finished else [])
                self.assertEqual([item['id'] for item in activity['upcoming']], [] if finished else [lesson['id']])
                self.assertEqual((activity['hours'], activity['completed']), (0, 0))
                html = self.client.get('/pairings/1/3').get_data(as_text=True)
                upcoming_html, finished_html = html.split('<section id="recordings"', 1)
                self.assertEqual('Algebra practice' in upcoming_html, not finished)
                self.assertEqual('Algebra practice' in finished_html, finished)
                if finished:
                    self.assertIn('Awaiting upload', finished_html)
                    self.assertIn(f'action="/api/lessons/{lesson["id"]}/recording-uploads"', finished_html)
                    self.assertNotIn('Open Zoom meeting', finished_html)
        self.assertIsNone(self.lesson(lesson['id'])['recorded_at'])
        # An elapsed lesson cannot be moved back into upcoming sessions.
        corrected = datetime.fromtimestamp(end, ZoneInfo('Asia/Kuala_Lumpur')) + timedelta(days=1)
        with patch('learning.time', **{'time.return_value': end}):
            response = self.client.post(f'/lessons/{lesson["id"]}/edit',
                                        data=self.form(scheduled_at=corrected.strftime('%Y-%m-%dT%H:%M')))
            self.assertEqual(response.status_code, 409)
            with app.app_context():
                self.assertEqual(activity_for_user(1)['finished'][0]['id'], lesson['id'])
                self.assertEqual(activity_for_user(1)['upcoming'], [])

    def test_finished_lists_include_every_past_lesson_and_role_specific_recording_actions(self):
        lessons = []
        for number in range(6):
            date = datetime.now(ZoneInfo('Asia/Kuala_Lumpur')) - timedelta(days=number + 1)
            lessons.append(self.create(title=f'Past lesson {number}', scheduled_at=date.strftime('%Y-%m-%dT%H:%M')))
        self.seed_legacy_recording(lessons[0]['id'], seconds=1800)
        self.create(title='Future lesson')
        other_date = datetime.now(ZoneInfo('Asia/Kuala_Lumpur')) - timedelta(days=8)
        self.create(mentee=4, title='Other pairing lesson', scheduled_at=other_date.strftime('%Y-%m-%dT%H:%M'))
        for user_id in (1, 3):
            self.auth(user_id)
            dashboard = self.client.get('/dashboard').get_data(as_text=True)
            upcoming, history = dashboard.split('<section class="dashboard-upcoming" id="finished-lessons"', 1)
            for number in range(6):
                self.assertNotIn(f'Past lesson {number}', upcoming)
                self.assertIn(f'Past lesson {number}', history)
            self.assertIn('Future lesson', upcoming)
            self.assertNotIn('Future lesson', history)
            self.assertIn(f'href="/lessons/{lessons[0]["id"]}/recording"', history)
            self.assertEqual('Other pairing lesson' in history, user_id == 1)
            room = self.client.get('/pairings/1/3').get_data(as_text=True).split('<section id="recordings"', 1)[1]
            for number in range(6):
                self.assertIn(f'Past lesson {number}', room)
            self.assertIn(f'href="/lessons/{lessons[0]["id"]}/recording"', room)
            self.assertNotIn('Other pairing lesson', room)
            for missing in lessons[1:]:
                self.assertNotIn(f'href="/lessons/{missing["id"]}/recording"', history)
            for html in (history, room):
                if user_id == 1:
                    self.assertIn('Awaiting upload', html)
                    self.assertIn('Upload recording', html)
                else:
                    self.assertIn('Recording not yet available', html)
                    self.assertNotIn('Awaiting upload', html)
                    self.assertNotIn('Upload recording', html)
            with app.app_context():
                activity = activity_for_user(user_id)
                self.assertEqual((activity['hours'], activity['completed']), (0.5, 1))
                self.assertEqual(len(activity['finished']), 7 if user_id == 1 else 6)
        self.auth(4)
        dashboard = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('Other pairing lesson', dashboard)
        self.assertNotIn('Past lesson 0', dashboard)
        self.assertEqual(self.client.get(f'/lessons/{lessons[0]["id"]}/recording').status_code, 403)

    def test_edit_and_cancel_lock_at_start_for_mentor_and_admins_are_read_only(self):
        lesson = self.create()
        with patch('learning.time', **{'time.return_value': lesson['scheduled_at'] - 1}):
            self.assertIn('Edit lesson', self.client.get('/pairings/1/3').get_data(as_text=True))
            response = self.client.post(f'/lessons/{lesson["id"]}/edit', data=self.form(title='Before start'))
            self.assertEqual(response.status_code, 302)
        lesson = self.lesson(lesson['id'])
        for user_id in (1, 99):
            self.auth(user_id)
            for timestamp in (lesson['scheduled_at'], lesson['scheduled_at'] + 1, lesson['scheduled_at'] + 7200):
                with self.subTest(user=user_id, timestamp=timestamp), patch('learning.time', **{'time.return_value': timestamp}):
                    html = self.client.get('/pairings/1/3').get_data(as_text=True)
                    self.assertNotIn('Edit lesson', html)
                    self.assertNotIn('Cancel lesson', html)
                    expected = 403 if user_id == 99 else 409
                    self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/edit', data=self.form()).status_code, expected)
                    self.assertEqual(self.client.post(f'/lessons/{lesson["id"]}/cancel', data={'_csrf_token': 'test-csrf'}).status_code, expected)
                    self.assertEqual(self.lesson(lesson['id']), lesson)

    def test_admin_lesson_management_stays_read_only_but_participants_can_chat(self):
        future = self.create()
        past = self.create(past=True)
        recorded = self.create(title='Recorded lesson', scheduled_at=(
            datetime.now(ZoneInfo('Asia/Kuala_Lumpur')) - timedelta(days=2)).strftime('%Y-%m-%dT%H:%M'))
        self.seed_legacy_recording(recorded['id'])
        chat = '/pairings/1/3/messages'
        self.client.post(chat, data={'_csrf_token': 'test-csrf', 'message': 'Existing conversation'})
        with edit_accounts(app.config['USERS_FILE']) as users:
            for user in users:
                if user['id'] in (1, 3):
                    user['is_coordinator'] = True
        for user_id in (99, 1, 3):
            with self.subTest(admin=user_id):
                self.auth(user_id)
                response = self.client.get('/pairings/1/3')
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                for text in ('Algebra practice', 'Recorded lesson',
                             'Existing conversation', 'Watch recording'):
                    self.assertIn(text, html)
                for text in ('Add a lesson', 'Edit lesson', 'Cancel lesson', 'Upload recording',
                             'Replace this recording', 'Don’t have a recording?', 'Mark reviewed'):
                    self.assertNotIn(text, html)
                for text in ('Send message', 'id="chat-form"'):
                    if user_id in (1, 3):
                        self.assertIn(text, html)
                    else:
                        self.assertNotIn(text, html)
                if user_id in (1, 3):
                    self.assertNotIn('Read-only admin view.', html)
                    self.assertNotIn('This chat is read-only for you.', html)
                else:
                    self.assertIn('Read-only admin view.', html)
                    self.assertIn('This chat is read-only for you.', html)
                self.assertEqual(self.client.get(chat).json['messages'][0]['body'], 'Existing conversation')
                video = self.client.get(f'/lessons/{recorded["id"]}/recording')
                self.assertEqual(video.status_code, 200)
                video.close()
                self.assertEqual(self.client.post('/pairings/1/3/lessons', data=self.form()).status_code, 403)
                self.assertEqual(self.client.post(f'/lessons/{future["id"]}/edit', data=self.form(title='Changed')).status_code, 403)
                self.assertEqual(self.client.post(f'/lessons/{future["id"]}/cancel', data={'_csrf_token': 'test-csrf'}).status_code, 403)
                self.assertEqual(self.report(past['id']).status_code, 403)
                reply = self.client.post(chat, data={'_csrf_token': 'test-csrf',
                                                     'message': 'Admin reply', 'sender_id': '99'})
                self.assertEqual(reply.status_code, 200 if user_id in (1, 3) else 403)
                if user_id in (1, 3):
                    self.assertEqual(reply.json['messages'][0]['sender_id'], user_id)
                dashboard = self.client.get('/dashboard').get_data(as_text=True)
                self.assertNotIn('Upload recording', dashboard)
                self.assertNotIn('data-recording-version', dashboard)
                self.assertNotIn('Don’t have a recording?', dashboard)
        self.assertEqual(self.lesson(future['id']), future)
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lessons').fetchone()[0], 3)
            self.assertEqual([row['sender_id'] for row in db.execute('SELECT sender_id FROM messages ORDER BY id')], [1, 1, 3])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM missing_recording_reports').fetchone()[0], 0)

    def test_coordinator_participants_cannot_chat_after_unlink_or_approval_revocation(self):
        path = '/pairings/1/3/messages'
        for inactive_reason in ('unlinked', 'pending_mentor'):
            with edit_accounts(app.config['USERS_FILE']) as users:
                for user in users:
                    if user['id'] in (1, 3):
                        user['is_coordinator'] = True
                        user['partners'] = [] if inactive_reason == 'unlinked' else [3 if user['id'] == 1 else 1]
                    if user['id'] == 1:
                        user['mentor_approval_status'] = 'pending' if inactive_reason == 'pending_mentor' else 'approved'
            for user_id in (1, 3):
                with self.subTest(reason=inactive_reason, user=user_id):
                    self.auth(user_id)
                    html = self.client.get('/pairings/1/3').get_data(as_text=True)
                    self.assertNotIn('id="chat-form"', html)
                    self.assertIn('This chat is read-only for you.', html)
                    self.assertEqual(self.client.get(path).status_code, 200)
                    self.assertEqual(self.client.post(path, data={
                        '_csrf_token': 'test-csrf', 'message': 'Must not be sent'}).status_code, 403)
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_missing_recording_validates_reason_other_text_and_access(self):
        lesson = self.create(past=True)
        for changes in ({'reason': ''}, {'reason': 'invented'}, {'reason': 'other'},
                        {'reason': 'other', 'note': ' '}, {'reason': 'other', 'note': 'x' * 1001},
                        {'_csrf_token': 'wrong'}):
            self.assertEqual(self.report(lesson['id'], **changes).status_code, 400)
        for user_id in (2, 3, 4, 99):
            self.auth(user_id)
            self.assertEqual(self.report(lesson['id']).status_code, 403)
        self.auth(None)
        self.assertEqual(self.report(lesson['id']).location, '/login')
        with app.app_context():
            self.assertEqual(recording_report_summaries(), {})
        self.auth(1)
        self.assertEqual(self.report(lesson['id'], reason='other', note='  Recording was unavailable.  ', mentor_id='2').status_code, 302)
        with app.app_context():
            report = recording_report_summaries()[1]['reports'][0]
        self.assertEqual(report['note'], 'Recording was unavailable.')
        self.assertEqual(report['reason'], 'other')

    def test_reports_only_apply_to_finished_unrecorded_lessons(self):
        future = self.create()
        self.assertEqual(self.report(future['id']).status_code, 409)
        with patch('learning.time', **{'time.return_value': future['scheduled_at']}):
            self.assertEqual(self.report(future['id']).status_code, 409)
        self.client.post(f'/lessons/{future["id"]}/cancel', data={'_csrf_token': 'test-csrf'})
        self.assertEqual(self.report(future['id']).status_code, 409)
        past = self.create(past=True)
        self.seed_legacy_recording(past['id'])
        self.assertEqual(self.report(past['id']).status_code, 409)
        with app.app_context():
            self.assertEqual(recording_report_summaries(), {})

    def test_duplicate_reports_count_once_preserve_history_and_do_not_mark_attendance(self):
        lesson = self.create(past=True)
        note = '<script>private explanation</script>, "quoted"\nsecond line'
        before = Path(app.config['USERS_FILE']).read_bytes()
        self.assertEqual(self.report(lesson['id'], reason='other', note=note).status_code, 302)
        for reason in ('other', 'forgot_to_record'):
            self.assertEqual(self.report(lesson['id'], reason=reason, note='changed').status_code, 409)
        with app.app_context():
            report = recording_report_summaries()[1]
            self.assertEqual((report['count'], report['reason_counts']['other']), (1, 1))
            self.assertEqual(report['reports'][0]['note'], note)
            activity = activity_for_user(1)
            self.assertEqual((activity['hours'], activity['completed']), (0, 0))
        html = self.client.get('/pairings/1/3').get_data(as_text=True)
        self.assertIn('&lt;script&gt;private explanation&lt;/script&gt;', html)
        self.assertNotIn('Submit explanation', html)
        self.auth(3)
        self.assertNotIn('private explanation', self.client.get('/pairings/1/3').get_data(as_text=True))
        self.auth(99)
        self.assertIn('private explanation', self.client.get('/coordinator/lessons').get_data(as_text=True))
        self.auth(1)
        self.seed_legacy_recording(lesson['id'], seconds=1800)
        with app.app_context():
            report = recording_report_summaries()[1]
            self.assertEqual(report['count'], 1)
            self.assertTrue(report['reports'][0]['recording_uploaded'])
            self.assertEqual((activity_for_user(1)['hours'], activity_for_user(1)['completed']), (0.5, 1))
        self.assertEqual(Path(app.config['USERS_FILE']).read_bytes(), before)

    def test_counter_reasons_csv_and_red_flag_after_four_reports_across_pairings(self):
        for number, reason in enumerate(MISSING_RECORDING_REASONS):
            self.auth(1)
            date = datetime.now(ZoneInfo('Asia/Kuala_Lumpur')) - timedelta(days=number + 1)
            lesson = self.create(mentee=3 if number % 2 == 0 else 4,
                                 title=f'Missing video {number}', scheduled_at=date.strftime('%Y-%m-%dT%H:%M'))
            self.assertEqual(self.report(lesson['id'], reason=reason, note='Personal explanation' if reason == 'other' else '').status_code, 302)
            if number in (2, 3, 5):
                self.auth(99)
                response = self.client.get('/export/mentors.csv')
                rows = list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))
                row = next(row for row in rows if row['id'] == '1')
                self.assertEqual(row['missing_recording_count'], str(number + 1))
                self.assertEqual(row['recording_flagged'], 'yes' if number > 2 else 'no')
                details = json.loads(row['missing_recording_responses'])
                self.assertEqual(len(details), number + 1)
                self.assertEqual(details[0]['lesson_id'], lesson['id'])
                self.assertEqual(details[0]['reason'], reason)
                for path in ('/export', '/coordinator/lessons'):
                    html = self.client.get(path).get_data(as_text=True)
                    self.assertEqual('class="recording-flagged-name"' in html, number > 2)
        self.assertEqual(row['missing_recording_other'], '1')
        self.assertEqual(row['missing_recording_forgot_to_record'], '1')
        self.assertEqual(row['missing_recording_lesson_did_not_take_place'], '1')
        self.assertEqual(details[0]['note'], 'Personal explanation')
        self.assertIn('lesson_date', details[0])
        self.assertIn('submitted_at', details[0])
        mentees = self.client.get('/export/mentees.csv').get_data(as_text=True)
        self.assertNotIn('missing_recording_count', mentees)
        self.assertNotIn('Personal explanation', mentees)
        self.auth(3)
        self.assertEqual(self.client.get('/export/mentors.csv').status_code, 403)

    def test_existing_database_keeps_lessons_and_adds_report_table(self):
        lesson = self.create(past=True)
        with app.app_context():
            with get_db() as db:
                db.execute('DROP TABLE missing_recording_reports')
        # The next connection initializes the additive schema without replacing old records.
        self.assertEqual(self.report(lesson['id']).status_code, 302)
        self.assertEqual(self.lesson(lesson['id'])['title'], lesson['title'])
        with app.app_context():
            self.assertEqual(recording_report_summaries()[1]['count'], 1)

    def test_deadline_notice_overdue_state_and_late_upload_recovery(self):
        date = datetime.now(ZoneInfo('Asia/Kuala_Lumpur')) - timedelta(days=8)
        lesson = self.create(scheduled_at=date.strftime('%Y-%m-%dT%H:%M'))
        for path in ('/dashboard', '/pairings/1/3'):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('Recordings must be uploaded within one week of the lesson.', html)
            self.assertIn('Failure to upload recordings for three lessons may result in removal from the programme.', html)
            self.assertIn('Overdue', html)
        with app.app_context():
            self.assertTrue(activity_for_user(1)['finished'][0]['upload_overdue'])
        self.seed_legacy_recording(lesson['id'])
        with app.app_context():
            self.assertFalse(activity_for_user(1)['finished'][0]['upload_overdue'])

    def test_playback_download_and_range_requests_are_private(self):
        lesson = self.create(past=True)
        self.seed_legacy_recording(lesson["id"])
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
        self.auth(2)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.auth(99)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_legacy_review_is_final_and_comments_are_shared(self):
        lesson = self.create(past=True)
        self.seed_legacy_recording(lesson["id"])
        data = {"_csrf_token": "test-csrf", "review_note": "Shared quality note", "recording_version": self.lesson(lesson["id"])["recording_filename"]}
        path = f'/lessons/{lesson["id"]}/review'
        self.assertEqual(self.client.post(path, data=data).status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        room = self.client.get('/pairings/1/3').get_data(as_text=True)
        self.assertIn("Shared quality note", room)
        self.assertNotIn(f'action="{path}"', room)
        self.assertNotIn(f'action="{path}"', self.client.get('/coordinator/lessons').get_data(as_text=True))
        saved = self.lesson(lesson['id'])
        self.assertEqual(self.client.post(path, data={**data, 'review_note': 'Changed'}).status_code, 409)
        self.assertEqual(self.lesson(lesson['id']), saved)
        self.auth(3)
        for view in ('/pairings/1/3', '/dashboard'):
            html = self.client.get(view).get_data(as_text=True)
            self.assertIn('Shared quality note', html)
            self.assertIn('Reviewed by Person99 Example', html)

    def test_legacy_review_assignment_validates_admin_and_remains_permanent(self):
        lesson = self.create(past=True)
        self.seed_legacy_recording(lesson['id'])
        with edit_accounts(app.config['USERS_FILE']) as users:
            users.append({'id': 100, 'username': 'reviewer', 'name': ['Assigned', 'Admin'],
                          'role': 'staff', 'is_coordinator': True})
        self.auth(99)
        path = f'/lessons/{lesson["id"]}/review'
        data = {'_csrf_token': 'test-csrf', 'recording_version': self.lesson(lesson['id'])['recording_filename'],
                'review_note': '<script>Shared comments</script>'}
        for reviewer in ('1', '3', '100000', 'invalid', ''):
            self.assertEqual(self.client.post(path, data={**data, 'reviewed_by': reviewer}).status_code, 400)
        form = self.client.get('/coordinator/lessons').get_data(as_text=True)
        self.assertIn('Assigned Admin', form)
        self.assertIn(f'action="{path}"', form)
        self.assertEqual(self.client.post(path, data={**data, 'reviewed_by': '100'}).status_code, 302)
        reviewed = self.lesson(lesson['id'])
        self.assertEqual((reviewed['reviewed_by'], reviewed['review_submitted_by'], reviewed['reviewer_name']),
                         (100, 99, 'Assigned Admin'))
        self.auth(100)
        self.assertEqual(self.client.post(path, data={**data, 'reviewed_by': '99'}).status_code, 409)
        self.assertEqual(self.lesson(lesson['id']), reviewed)
        for user in (1, 3):
            self.auth(user)
            html = self.client.get('/pairings/1/3').get_data(as_text=True)
            self.assertIn('Reviewed by Assigned Admin', html)
            self.assertIn('&lt;script&gt;Shared comments&lt;/script&gt;', html)
            self.assertNotIn('Replace this recording', html)

    def test_existing_reviews_survive_additive_reviewer_metadata_migration(self):
        old_schema = SCHEMA.replace('    review_submitted_by INTEGER,\n', '').replace('    reviewer_name TEXT,\n', '')
        with sqlite3.connect(app.config['LEARNING_DATABASE']) as db:
            db.executescript(old_schema)
            db.execute('''INSERT INTO lessons(id, mentor_id, mentee_id, title, scheduled_at, planned_minutes,
                zoom_url, created_by, created_at, recorded_at, reviewed_at, reviewed_by, review_note)
                VALUES ('existing-review', 1, 3, 'Existing lesson', 100, 60, 'https://zoom.us/j/1', 1, 100,
                        200, 300, 99, 'Existing final comments')''')
            db.execute('''INSERT INTO recordings(id, session_id, mentor_id, mentee_id, r2_object_key,
                original_filename, content_type, file_size, uploaded_at, expires_at,
                reviewed_at, reviewed_by, review_note, review_status)
                VALUES ('existing-recording', 'existing-review', 1, 3, 'sessions/existing.mp4',
                        'zoom.mp4', 'video/mp4', 1000, 200, 400, 300, 99, 'Existing final comments', 'reviewed')''')
        for _ in range(2):
            with app.app_context():
                db = get_db()
                for table in ('lessons', 'recordings'):
                    row = dict(db.execute(f'SELECT * FROM {table}').fetchone())
                    self.assertEqual((row['reviewed_at'], row['reviewed_by'], row['review_note']),
                                     (300, 99, 'Existing final comments'))
                    self.assertIn('review_submitted_by', row)
                    self.assertIn('reviewer_name', row)
        self.auth(3)
        html = self.client.get('/pairings/1/3').get_data(as_text=True)
        self.assertIn('Existing final comments', html)
        self.assertIn('Reviewed by Person99 Example', html)

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
        self.seed_legacy_recording(lesson["id"])
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



if __name__ == '__main__':
    unittest.main()
