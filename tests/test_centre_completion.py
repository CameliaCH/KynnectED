import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from account_store import edit_accounts, read_accounts
from app import app, ph


class CentreCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = ph.hash('centre-test-password')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.path = root / 'users.json'
        previous = {key: app.config[key] for key in ('TESTING', 'USERS_FILE', 'LEARNING_DATABASE')}
        self.addCleanup(lambda: app.config.update(previous))
        app.config.update(TESTING=True, USERS_FILE=str(self.path), LEARNING_DATABASE=str(root / 'learning.sqlite3'))
        self.path.write_text(json.dumps([
            {'id': 1, 'username': 'student', 'role': 'mentee', 'name': ['Test', 'Student'],
             'password': self.password_hash, 'email': 'student@example.com', 'partners': []},
            {'id': 2, 'username': 'mentor', 'role': 'mentor', 'name': ['Test', 'Mentor'],
             'email': 'mentor@example.com', 'partners': []},
            {'id': 99, 'username': 'admin', 'role': 'staff', 'is_coordinator': True},
        ]))
        self.client = app.test_client()

    def auth(self, user_id=1):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=user_id, csrf_token='centre-csrf')

    def save(self, centre, **extra):
        return self.client.post('/account/centre', data={
            'centre': centre, '_csrf_token': 'centre-csrf', **extra})

    def test_existing_sessions_with_missing_null_or_blank_centre_must_complete_it(self):
        for centre in (None, '', '   '):
            with edit_accounts(self.path) as users:
                users[0]['centre'] = centre
            self.auth()
            for path in ('/', '/dashboard', '/learning-resources/english', '/pairings/2/1'):
                self.assertEqual(self.client.get(path).location, '/account/centre')
            response = self.client.get('/account/centre')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn('Which centre are you from?', response.get_data(as_text=True))
            with self.client.get('/static/css/style.css') as static_response:
                self.assertEqual(static_response.status_code, 200)
        before = self.path.read_bytes()
        self.assertEqual(self.client.post('/pairings/2/1/messages', data={
            '_csrf_token': 'centre-csrf', 'body': 'Cannot bypass completion'}).location, '/account/centre')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.post('/logout', data={'_csrf_token': 'centre-csrf'}).location, '/')

    def test_login_requires_centre_and_email_completion_can_happen_first(self):
        for missing_email in (False, True):
            with edit_accounts(self.path) as users:
                users[0]['email'] = '' if missing_email else 'student@example.com'
            with self.client.session_transaction() as session:
                session.clear()
                session['csrf_token'] = 'centre-csrf'
            response = self.client.post('/login-process', data={
                'username': 'student', 'password': 'centre-test-password', '_csrf_token': 'centre-csrf'})
            self.assertEqual(response.location, '/account/email' if missing_email else '/account/centre')
            if missing_email:
                self.assertEqual(self.client.get('/account/email').status_code, 200)
                with self.client.session_transaction() as session:
                    token = session['csrf_token']
                response = self.client.post('/account/email', data={
                    'email': 'student@example.com', 'password': 'centre-test-password', '_csrf_token': token},
                    follow_redirects=True)
                self.assertEqual(response.request.path, '/account/centre')

    def test_save_persists_only_own_centre_and_exports_it(self):
        self.auth()
        before = read_accounts(self.path)
        self.assertEqual(self.save('  Étoile, Learning Centre  ', user_id=2).location, '/dashboard')
        expected = before
        expected[0]['centre'] = 'Étoile, Learning Centre'
        self.assertEqual(read_accounts(self.path), expected)
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self.assertEqual(self.client.get('/account/centre').location, '/dashboard')
        self.auth(99)
        response = self.client.get('/export/mentees.csv')
        row = list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))[0]
        self.assertEqual(row['centre'], 'Étoile, Learning Centre')

    def test_invalid_answers_and_missing_csrf_do_not_change_accounts(self):
        self.auth()
        before = self.path.read_bytes()
        for centre in ('', '   ', 'x' * 151):
            self.assertEqual(self.save(centre).status_code, 400)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.post('/account/centre', data={'centre': 'Example'}).status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.save('No centre').status_code, 302)
        self.assertEqual(self.client.get('/dashboard').status_code, 200)

    def test_completed_students_and_mentors_are_not_prompted_or_overwritten(self):
        with edit_accounts(self.path) as users:
            users[0]['centre'] = 'Existing Centre'
        before = self.path.read_bytes()
        for user_id in (1, 2):
            self.auth(user_id)
            self.assertEqual(self.client.get('/dashboard').status_code, 200)
            self.assertEqual(self.save('Replacement').location, '/dashboard')
        self.assertEqual(self.path.read_bytes(), before)

    def test_anonymous_users_must_log_in(self):
        self.assertEqual(self.client.get('/account/centre').location, '/login')
