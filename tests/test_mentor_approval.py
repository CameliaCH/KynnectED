"""Mentor application approval, contact details, and programme access."""
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from app import app, ph, MENTOR_CONDUCT_VERSION, MENTOR_CONDUCT_DOCUMENT_SHA256
from account_store import edit_accounts, read_accounts


class MentorApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = ph.hash('approval-test-password')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.accounts = self.root / 'users.json'
        keys = ('TESTING', 'USERS_FILE', 'LEARNING_DATABASE', 'RECORDINGS_DIR', 'MATERIALS_DIR')
        previous = {key: app.config[key] for key in keys}
        self.addCleanup(lambda: app.config.update(previous))
        app.config.update(TESTING=True, USERS_FILE=str(self.accounts),
                          LEARNING_DATABASE=str(self.root / 'learning.sqlite3'),
                          RECORDINGS_DIR=str(self.root / 'recordings'), MATERIALS_DIR=str(self.root / 'materials'))
        self.accounts.write_text(json.dumps([
            {'id': 99, 'username': 'admin', 'name': ['Admin', 'Test'], 'role': 'mentor',
             'is_coordinator': True, 'partners': [], 'subjects': [], 'password': self.password_hash},
            {'id': 1, 'username': 'legacymentor', 'name': ['Existing', 'Mentor'], 'role': 'mentor',
             'partners': [], 'subjects': [], 'password': self.password_hash},
            {'id': 2, 'username': 'student', 'name': ['Student', 'Test'], 'role': 'mentee',
             'partners': [], 'subjects': [], 'password': self.password_hash},
        ]))
        self.client = app.test_client()
        self.client.get('/signup')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf_token']

    def auth(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            if user_id:
                session['user_id'] = user_id
            session['csrf_token'] = self.csrf

    def signup(self, **changes):
        data = {'username': 'applicant', 'firstname': 'New', 'surname': 'Applicant', 'age': '18',
                'email': 'applicant@example.com', 'password': 'approval-test-password', 'role': 'mentor',
                'selectedSubjects': '["english"]', 'gender_own': 'female', 'gender_preferred': '["same"]',
                'age_preferred': '["same-age"]', 'days': json.dumps([['empty']] * 7), '_csrf_token': self.csrf,
                'mentor_conduct_accepted': 'yes', 'mentor_conduct_version': MENTOR_CONDUCT_VERSION,
                'mentor_conduct_document_sha256': MENTOR_CONDUCT_DOCUMENT_SHA256}
        data.update(changes)
        return self.client.post('/signup-process', data=data)

    def applicant(self):
        return next(user for user in read_accounts(self.accounts) if user['username'] == 'applicant')

    def approve(self, mentor_id, **changes):
        return self.client.post(f'/coordinator/mentors/{mentor_id}/approve', data={'_csrf_token': self.csrf, **changes})

    def csv_rows(self, group):
        response = self.client.get(f'/export/{group}.csv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig'))))

    def test_new_mentor_is_pending_and_contactable_but_not_in_active_lists(self):
        self.assertEqual(self.signup(mentor_approval_status='approved', is_coordinator='true').json, {'error': 'none'})
        applicant = self.applicant()
        self.assertEqual(applicant['mentor_approval_status'], 'pending')
        self.assertEqual(applicant['email'], 'applicant@example.com')
        self.assertNotIn('is_coordinator', applicant)
        self.assertIsNotNone(datetime.fromisoformat(applicant['mentor_applied_at']).tzinfo)
        self.auth(99)
        for path in ('/coordinator/mentor-approvals', '/export'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            html = response.get_data(as_text=True)
            self.assertIn('Pending mentors', html)
            self.assertIn('href="mailto:applicant@example.com"', html)
            self.assertIn('Approve mentor', html)
        pending = self.csv_rows('pending-mentors')
        self.assertEqual([(row['username'], row['email']) for row in pending], [('applicant', 'applicant@example.com')])
        self.assertNotIn('applicant', [row['username'] for row in self.csv_rows('mentors')])
        self.assertNotIn('@applicant', self.client.get('/coordinator/pairings').get_data(as_text=True))
        before = self.accounts.read_bytes()
        response = self.client.post('/coordinator/pairings', data={
            'action': 'link', 'mentor_id': applicant['id'], 'mentee_id': 2, '_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.accounts.read_bytes(), before)

    def test_approval_moves_mentor_preserving_profile_consent_and_existing_login(self):
        self.signup()
        original = self.applicant()
        self.auth(original['id'])
        self.assertIn('Awaiting approval', self.client.get('/dashboard').get_data(as_text=True))
        # Use a separate admin client so the applicant's existing session stays active.
        admin = app.test_client()
        with admin.session_transaction() as session:
            session['user_id'] = 99
            session['csrf_token'] = self.csrf
        start = datetime.now(timezone.utc)
        response = admin.post(f'/coordinator/mentors/{original["id"]}/approve', data={'_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 302)
        approved = self.applicant()
        self.assertEqual(approved['mentor_approval_status'], 'approved')
        self.assertEqual(approved['mentor_approved_by'], 99)
        self.assertGreaterEqual(datetime.fromisoformat(approved['mentor_approved_at']), start)
        for field in ('id', 'email', 'password', 'name', 'subjects', 'availability', 'mentor_code_of_conduct'):
            self.assertEqual(approved[field], original[field])
        self.assertNotIn('Awaiting approval', self.client.get('/dashboard').get_data(as_text=True))
        self.assertEqual(self.client.get('/learning-resources/submit').status_code, 200)
        self.auth(99)
        self.assertEqual(self.csv_rows('pending-mentors'), [])
        row = next(row for row in self.csv_rows('mentors') if row['username'] == 'applicant')
        self.assertEqual(row['email'], 'applicant@example.com')
        self.assertEqual(row['mentor_approval_status'], 'approved')
        self.assertIn('@applicant', self.client.get('/coordinator/pairings').get_data(as_text=True))
        before = self.accounts.read_bytes()
        self.assertEqual(self.approve(original['id']).status_code, 409)
        self.assertEqual(self.accounts.read_bytes(), before)

    def test_pending_login_has_status_and_cannot_bypass_approval(self):
        self.signup()
        applicant = self.applicant()
        response = self.client.post('/login-process', data={
            'username': 'applicant', 'password': 'approval-test-password', '_csrf_token': self.csrf})
        self.assertEqual(response.location, '/dashboard')
        response = self.client.get('/dashboard')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        html = response.get_data(as_text=True)
        self.assertIn('Awaiting approval', html)
        self.assertIn('applicant@example.com', html)
        self.assertNotIn('Your pairings', html)
        self.assertEqual(self.client.get('/learning-resources/submit').status_code, 403)
        self.assertNotIn('Submit slides', self.client.get('/learning-resources').get_data(as_text=True))
        self.assertNotIn('Submit slides', self.client.get('/learning-resources/english').get_data(as_text=True))
        self.assertEqual(self.client.get('/learning-resources/safeguarding-guidance').status_code, 200)
        # Even manually inserted partner IDs cannot turn a pending mentor into an active pairing.
        with edit_accounts(self.accounts) as users:
            next(user for user in users if user['id'] == applicant['id'])['partners'] = [2]
            next(user for user in users if user['id'] == 2)['partners'] = [applicant['id']]
        self.assertEqual(self.client.get(f'/pairings/{applicant["id"]}/2').status_code, 403)
        self.assertEqual(self.client.get(f'/pairings/{applicant["id"]}/2/messages').status_code, 403)

    def test_approval_routes_are_admin_only_and_require_csrf(self):
        self.signup()
        applicant = self.applicant()
        before = self.accounts.read_bytes()
        for user_id in (1, 2, applicant['id']):
            self.auth(user_id)
            self.assertEqual(self.client.get('/coordinator/mentor-approvals').status_code, 403)
            self.assertEqual(self.client.get('/export/pending-mentors.csv').status_code, 403)
            self.assertEqual(self.approve(applicant['id']).status_code, 403)
        self.auth(None)
        self.assertEqual(self.client.get('/coordinator/mentor-approvals').location, '/login')
        self.assertEqual(self.approve(applicant['id']).location, '/login')
        self.auth(99)
        self.assertEqual(self.approve(applicant['id'], _csrf_token='wrong').status_code, 400)
        self.assertEqual(self.approve(2).status_code, 404)
        self.assertEqual(self.approve(12345).status_code, 404)
        self.assertEqual(self.accounts.read_bytes(), before)

    def test_email_validation_and_mentee_signup_without_email(self):
        before = self.accounts.read_bytes()
        for email in ('', 'not-an-email', 'bad@@example.com', 'a@example.com\nBcc:x@y.com',
                      '.bad@example.com', 'a@bad..com', 'a@-example.com', 'a' * 255 + '@example.com'):
            with self.subTest(email=email):
                response = self.signup(email=email)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json['error'], 'email')
                self.assertEqual(self.accounts.read_bytes(), before)
        self.assertEqual(self.signup(email='', role='mentee').json, {'error': 'none'})
        self.assertNotIn('mentor_approval_status', self.applicant())
        self.assertNotIn('email', self.applicant())

    def test_existing_mentors_remain_approved_and_team_section_is_saved(self):
        self.auth(1)
        self.assertNotIn('Awaiting approval', self.client.get('/dashboard').get_data(as_text=True))
        self.assertEqual(self.client.get('/learning-resources/submit').status_code, 200)
        self.auth(99)
        self.assertEqual([row['username'] for row in self.csv_rows('mentors')], ['admin', 'legacymentor'])
        self.assertEqual(self.csv_rows('pending-mentors'), [])
        html = self.client.get('/about').get_data(as_text=True)
        self.assertNotIn('<h2>Our team</h2>', html)
        self.assertNotIn('Founder / President', html)
        saved = (Path(app.root_path) / 'templates/about_team.html').read_text()
        self.assertIn('<h2>Our team</h2>', saved)
        self.assertIn('Camelia', saved)
        self.assertIn('Igyeom', saved)


if __name__ == '__main__':
    unittest.main()
