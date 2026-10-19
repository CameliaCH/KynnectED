"""Mentor declaration acceptance and document access."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app import app, MENTOR_CONDUCT_VERSION, MENTOR_CONDUCT_DOCUMENT_SHA256, LEARNER_GUIDELINES_VERSION
from account_store import read_accounts


class ConductTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.accounts = self.root / "users.json"
        self.accounts.write_text('[]')
        keys = ("TESTING", "USERS_FILE", "LEARNING_DATABASE", "RECORDINGS_DIR")
        previous = {key: app.config[key] for key in keys}
        self.addCleanup(lambda: app.config.update(previous))
        app.config.update(TESTING=True, USERS_FILE=str(self.accounts),
                          LEARNING_DATABASE=str(self.root / 'learning.sqlite3'),
                          RECORDINGS_DIR=str(self.root / 'recordings'))
        self.client = app.test_client()
        self.client.get('/signup')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf_token']

    def signup_data(self, **changes):
        data = {"username": "newmentor", "firstname": "New", "surname": "Mentor", "age": "18",
                "school": "Example High School",
                "email": "newmentor@example.com",
                "password": "signup-test", "role": "mentor", "selectedSubjects": '["english"]',
                "gender_own": "female", "gender_preferred": '["same"]', "age_preferred": '["same-age"]',
                "days": json.dumps([["empty"]] * 7), "_csrf_token": self.csrf,
                "mentor_conduct_accepted": "yes", "mentor_conduct_version": MENTOR_CONDUCT_VERSION,
                "learner_guidelines_accepted": "yes", "learner_guidelines_version": LEARNER_GUIDELINES_VERSION,
                "mentor_conduct_document_sha256": MENTOR_CONDUCT_DOCUMENT_SHA256}
        data.update(changes)
        return data

    def test_missing_or_unchecked_declaration_cannot_create_mentor(self):
        for acceptance in (None, '', 'no', 'true'):
            data = self.signup_data(mentor_conduct_accepted=acceptance)
            if acceptance is None:
                data.pop('mentor_conduct_accepted')
            response = self.client.post('/signup-process', data=data)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json['error'], 'conduct')
            self.assertEqual(read_accounts(self.accounts), [])

    def test_outdated_version_or_document_cannot_create_mentor(self):
        for changes in ({'mentor_conduct_version': '0.9'}, {'mentor_conduct_version': ''},
                        {'mentor_conduct_document_sha256': 'old-document'},
                        {'mentor_conduct_document_sha256': ''}):
            response = self.client.post('/signup-process', data=self.signup_data(**changes))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json['error'], 'conduct')
            self.assertEqual(read_accounts(self.accounts), [])

    def test_mentor_acceptance_is_saved_with_actual_document_and_utc_time(self):
        start = datetime.now(timezone.utc)
        response = self.client.post('/signup-process', data=self.signup_data())
        self.assertEqual(response.json, {'error': 'none'})
        users = read_accounts(self.accounts)
        self.assertEqual(len(users), 1)
        acceptance = users[0]['mentor_code_of_conduct']
        self.assertEqual(acceptance['version'], MENTOR_CONDUCT_VERSION)
        self.assertEqual(acceptance['document_sha256'], MENTOR_CONDUCT_DOCUMENT_SHA256)
        timestamp = datetime.fromisoformat(acceptance['accepted_at'])
        self.assertEqual(timestamp.utcoffset().total_seconds(), 0)
        self.assertGreaterEqual(timestamp, start)
        self.assertLessEqual(timestamp, datetime.now(timezone.utc))
        # A duplicate request must neither create another account nor change acceptance.
        response = self.client.post('/signup-process', data=self.signup_data())
        self.assertEqual(response.json, {'error': '2'})
        self.assertEqual(read_accounts(self.accounts), users)

    def test_mentee_signup_does_not_require_or_store_mentor_acceptance(self):
        for number, provide_consent in enumerate((False, True)):
            data = self.signup_data(role='mentee', username=f'newstudent{number}')
            if not provide_consent:
                for field in ('mentor_conduct_accepted', 'mentor_conduct_version', 'mentor_conduct_document_sha256'):
                    data.pop(field)
            response = self.client.post('/signup-process', data=data)
            self.assertEqual(response.json, {'error': 'none'})
            self.assertNotIn('mentor_code_of_conduct', read_accounts(self.accounts)[-1])

    def test_public_documents_are_the_supplied_pdfs(self):
        response = self.client.get('/code-of-conduct')
        self.assertEqual(response.status_code, 200)
        self.assertIn('KynnectED_Mentor_Code_of_Conduct_Website.pdf', response.get_data(as_text=True))
        self.assertNotIn('By clicking the tick box below', response.get_data(as_text=True))
        for name in ('Signup', 'Website'):
            filename = f'KynnectED_Mentor_Code_of_Conduct_{name}.pdf'
            response = self.client.get('/static/documents/' + filename)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'application/pdf')
            original = (Path(app.static_folder) / 'documents' / filename).read_bytes()
            self.assertEqual(hashlib.sha256(response.data).hexdigest(), hashlib.sha256(original).hexdigest())
            response.close()
        signup = self.client.get('/signup').get_data(as_text=True)
        self.assertIn('KynnectED_Mentor_Code_of_Conduct_Signup.pdf', signup)
        self.assertIn('By clicking the tick box below, I confirm that:', signup)

    def test_learner_guide_must_be_accepted_at_current_version(self):
        for changes in ({'learner_guidelines_accepted': None}, {'learner_guidelines_accepted': 'no'},
                        {'learner_guidelines_version': None}, {'learner_guidelines_version': 'old'}):
            with self.subTest(changes=changes):
                data = self.signup_data(role='mentee', **changes)
                data = {key: value for key, value in data.items() if value is not None}
                response = self.client.post('/signup-process', data=data)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json['error'], 'guidelines')
                self.assertEqual(read_accounts(self.accounts), [])

    def test_learner_acceptance_is_saved_and_not_required_for_mentors(self):
        start = datetime.now(timezone.utc)
        response = self.client.post('/signup-process', data=self.signup_data(role='mentee'))
        self.assertEqual(response.json, {'error': 'none'})
        learner = read_accounts(self.accounts)[0]
        self.assertEqual(learner['learner_guidelines']['version'], LEARNER_GUIDELINES_VERSION)
        self.assertGreaterEqual(datetime.fromisoformat(learner['learner_guidelines']['accepted_at']), start)
        mentor_data = self.signup_data(username='anothermentor')
        mentor_data.pop('learner_guidelines_accepted')
        mentor_data.pop('learner_guidelines_version')
        self.assertEqual(self.client.post('/signup-process', data=mentor_data).json, {'error': 'none'})
        self.assertNotIn('learner_guidelines', read_accounts(self.accounts)[-1])

    def test_other_subjects_are_saved_as_names_for_both_roles(self):
        for role in ('mentor', 'mentee'):
            response = self.client.post('/signup-process', data=self.signup_data(
                username=f'custom{role}', role=role,
                selectedSubjects=json.dumps(['maths', 'english', 'general homework support', 'other']),
                other_subjects=' Science, Malay, science, Math\n Art '))
            self.assertEqual(response.json, {'error': 'none'})
            self.assertEqual(read_accounts(self.accounts)[-1]['subjects'],
                             ['maths', 'english', 'general homework support', 'science', 'malay', 'art'])

    def test_invalid_subject_selection_does_not_create_account(self):
        for subjects, other in (([], ''), (['science'], ''), (['other'], ''), (['other'], ' , \n '),
                                (['other'], 'Other'), (['other'], 'a' * 201)):
            with self.subTest(subjects=subjects, other=other):
                response = self.client.post('/signup-process', data=self.signup_data(
                    selectedSubjects=json.dumps(subjects), other_subjects=other))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json['error'], 'subjects')
                self.assertEqual(read_accounts(self.accounts), [])

    def test_unselected_other_subjects_are_ignored(self):
        response = self.client.post('/signup-process', data=self.signup_data(other_subjects='Science'))
        self.assertEqual(response.json, {'error': 'none'})
        self.assertEqual(read_accounts(self.accounts)[0]['subjects'], ['english'])

    def test_mentor_dashboard_has_dedicated_conduct_link(self):
        users = [{'id': 1, 'username': 'mentor', 'name': ['Mentor', 'Test'], 'role': 'mentor', 'subjects': [], 'partners': []},
                 {'id': 2, 'username': 'mentee', 'name': ['Student', 'Test'], 'role': 'mentee', 'subjects': [], 'partners': []}]
        self.accounts.write_text(json.dumps(users))
        for user_id, should_show in ((1, True), (2, False)):
            with self.client.session_transaction() as session:
                session['user_id'] = user_id
            response = self.client.get('/dashboard')
            self.assertEqual(response.status_code, 200)
            self.assertEqual('id="dashboard-conduct-heading"' in response.get_data(as_text=True), should_show)


if __name__ == '__main__':
    unittest.main()
