import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from app import app, ph
from account_store import edit_accounts, read_accounts
from password_reset import deliver_reset, issue_token, public_url, queue_reset, send_email


class PasswordResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_hash = ph.hash('original-password')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'users.json'
        original_config = dict(app.config)
        self.addCleanup(lambda: app.config.update(original_config))
        app.config.update(TESTING=True, EMAIL_RESETS_ENABLED=True, USERS_FILE=str(self.path), PUBLIC_URL='https://kynnected.example',
                          SMTP_HOST='smtp.example', SMTP_FROM='team@kynnected.example',
                          SMTP_USERNAME='mail-user', SMTP_PASSWORD='mail-password', SMTP_PORT=587, SMTP_SSL=False)
        self.users = [dict(id=1, username='mentor', password=self.original_hash, role='mentor',
                           name=['Test', 'Mentor'], contact_method='email', contact_value='saved@example.com',
                           partners=[2], is_coordinator=True, mentor_approval_status='pending'),
                      dict(id=2, username='student', password=self.original_hash, role='mentee',
                           contact_method='whatsapp', contact_value='+60123456789', partners=[1])]
        self.path.write_text(json.dumps(self.users))
        self.client = app.test_client()
        self.client.get('/forgot-password')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf_token']

    def token(self, id=1):
        with edit_accounts(self.path) as users:
            return issue_token(next(user for user in users if user['id'] == id))

    def submit(self, token, **values):
        return self.client.post('/reset-password/' + token, data={
            '_csrf_token': self.csrf, 'password': 'a new long password',
            'confirm_password': 'a new long password', **values})

    def test_reset_changes_only_credentials_and_revokes_existing_sessions(self):
        stale_client = app.test_client()
        with stale_client.session_transaction() as session:
            session['user_id'] = 1
        token = self.token()
        response = self.submit(token)
        self.assertEqual(response.location, '/login')
        users = read_accounts(self.path)
        self.assertTrue(ph.verify(users[0]['password'], 'a new long password'))
        expected = copy.deepcopy(self.users[0])
        expected.update(password=users[0]['password'], auth_version=1)
        self.assertEqual(users, [expected, self.users[1]])
        self.assertEqual(stale_client.get('/dashboard').location, '/login')
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)
        self.client.get('/login')
        with self.client.session_transaction() as session:
            csrf = session['csrf_token']
        self.assertEqual(self.client.post('/login-process', data={
            'username': 'mentor', 'password': 'original-password', '_csrf_token': csrf}).status_code, 401)
        response = self.client.post('/login-process', data={
            'username': 'mentor', 'password': 'a new long password', '_csrf_token': csrf})
        self.assertEqual(response.location, '/coordinator')
        with self.client.session_transaction() as session:
            self.assertEqual(session['auth_version'], 1)
        self.assertEqual(self.client.get('/login').location, '/coordinator')
        self.assertEqual(self.client.get('/reset-password/' + token).status_code, 400)

    def test_expired_tampered_and_superseded_links_do_not_change_password(self):
        old = self.token()
        current = self.token()
        with patch('password_reset.time.time', return_value=time.time() + 1801):
            self.assertEqual(self.submit(current).status_code, 400)
        for token in [old, current[:-1] + ('A' if current[-1] != 'A' else 'B'), 'invalid', 'é' * 43]:
            self.assertEqual(self.submit(token).status_code, 400)
        self.assertEqual(read_accounts(self.path)[0]['password'], self.original_hash)

    def test_csrf_password_rules_and_headers(self):
        token = self.token()
        self.assertEqual(self.client.post('/forgot-password', data={'username': 'mentor'}).status_code, 400)
        self.assertEqual(self.submit(token, _csrf_token='wrong').status_code, 400)
        for values in [{'password': 'short'}, {'password': 'x' * 129}, {'confirm_password': 'different'}]:
            self.assertEqual(self.submit(token, **values).status_code, 400)
        response = self.client.get('/reset-password/' + token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertNotIn(token, self.path.read_text())
        self.assertEqual(self.submit(token).status_code, 302)

    def test_concurrent_submissions_can_only_consume_link_once(self):
        token = self.token()
        def reset_once(number):
            client = app.test_client()
            client.get('/reset-password/' + token)
            with client.session_transaction() as session:
                csrf = session['csrf_token']
            return client.post('/reset-password/' + token, data={
                '_csrf_token': csrf, 'password': 'a new long password',
                'confirm_password': 'a new long password'}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(reset_once, range(2)))
        self.assertEqual(sorted(statuses), [302, 400])

    @patch('password_reset.queue_reset')
    def test_requests_are_generic_and_limited_without_disclosing_email(self, queue):
        bodies = []
        for username in ['mentor', 'student', 'missing']:
            response = self.client.post('/forgot-password', data={'username': username, '_csrf_token': self.csrf})
            self.assertEqual(response.status_code, 200)
            bodies.append(response.data)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0], bodies[2])
        self.assertNotIn(b'saved@example.com', bodies[0])
        queue.reset_mock()
        for index in range(8):
            self.client.post('/forgot-password', data={'username': 'MENTOR', '_csrf_token': self.csrf})
        self.assertEqual(queue.call_count, 4)  # Five/hour, including the earlier request.
        self.assertEqual(read_accounts(self.path), self.users)

    @patch('password_reset.queue_reset')
    def test_ip_limit_applies_across_usernames(self, queue):
        for index in range(25):
            self.client.post('/forgot-password', data={'username': f'user{index}', '_csrf_token': self.csrf})
        self.assertEqual(queue.call_count, 20)

    @patch('password_reset.send_email')
    def test_delivery_uses_saved_email_and_trusted_origin(self, send):
        deliver_reset(dict(app.config), 'mentor', app.logger)
        config, recipient, link = send.call_args.args
        self.assertEqual(recipient, 'saved@example.com')
        self.assertTrue(link.startswith('https://kynnected.example/reset-password/'))
        token = link.rsplit('/', 1)[-1]
        self.assertEqual(self.client.get('/reset-password/' + token).status_code, 200)
        for username in ['mentor', 'student', 'missing']:
            deliver_reset(dict(app.config), username, app.logger)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn(token, self.path.read_text())

    @patch('password_reset.send_email', side_effect=OSError('secret SMTP details'))
    def test_failed_delivery_discards_token_and_keeps_password(self, send):
        with self.assertLogs(app.logger, level='ERROR') as logs:
            deliver_reset(dict(app.config), 'mentor', app.logger)
        self.assertNotIn('secret SMTP details', ''.join(logs.output))
        self.assertEqual(read_accounts(self.path), self.users)

    @patch('password_reset.smtplib.SMTP')
    def test_smtp_uses_tls_and_never_emails_password(self, smtp):
        send_email(dict(app.config), 'saved@example.com', 'https://kynnected.example/reset-password/example')
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once_with('mail-user', 'mail-password')
        message = server.send_message.call_args.args[0]
        self.assertEqual(message['To'], 'saved@example.com')
        self.assertNotIn(self.original_hash, str(message))

    @patch('password_reset._workers')
    def test_email_delivery_is_queued(self, workers):
        queue_reset(dict(app.config), 'missing', app.logger)
        workers.submit.assert_called_once()
        callback = workers.submit.return_value.add_done_callback.call_args.args[0]
        callback(None)  # Release the bounded slot, as a completed worker would.

    def test_missing_mail_setup_and_team_assisted_recovery(self):
        app.config['SMTP_HOST'] = ''
        response = self.client.get('/forgot-password')
        self.assertIn(b'Contact the KynnectED team with your username', response.data)
        self.assertNotIn(b'Send reset link', response.data)
        result = app.test_cli_runner().invoke(args=['create-password-reset', 'STUDENT'])
        self.assertEqual(result.exit_code, 0, result.output)
        link = result.output.strip().splitlines()[-1]
        self.assertEqual(self.submit(link.rsplit('/', 1)[-1]).status_code, 302)
        self.assertTrue(ph.verify(read_accounts(self.path)[1]['password'], 'a new long password'))

    def test_mail_with_username_requires_password_before_offering_email_reset(self):
        app.config['SMTP_PASSWORD'] = ''
        response = self.client.get('/forgot-password')
        self.assertIn(b'Contact the KynnectED team with your username', response.data)
        with patch('password_reset.queue_reset') as queue:
            self.client.post('/forgot-password', data={'username': 'mentor', '_csrf_token': self.csrf})
        queue.assert_not_called()

    def test_team_contact_mode_disables_email_requests_even_with_smtp_configured(self):
        app.config['EMAIL_RESETS_ENABLED'] = False
        response = self.client.get('/forgot-password')
        self.assertIn(b'Contact the KynnectED team with your username', response.data)
        self.assertIn(b'mailto:kynnected.org@gmail.com', response.data)
        self.assertNotIn(b'Send reset link', response.data)
        with patch('password_reset.queue_reset') as queue:
            response = self.client.post('/forgot-password', data={'username': 'mentor', '_csrf_token': self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'a reset link will be sent', response.data)
        queue.assert_not_called()
        self.assertEqual(read_accounts(self.path), self.users)

    def test_origin_validation_and_cli_errors_do_not_mutate_accounts(self):
        for origin in ['', 'http://public.example', 'https://user:pass@site.example',
                       'https://site.example/path', 'https://site.example?other', '//site.example']:
            app.config['PUBLIC_URL'] = origin
            with self.assertRaises(ValueError):
                public_url(app.config)
            result = app.test_cli_runner().invoke(args=['create-password-reset', 'mentor'])
            self.assertNotEqual(result.exit_code, 0)
        app.config['PUBLIC_URL'] = 'http://localhost:5001'
        self.assertEqual(public_url(app.config), 'http://localhost:5001')
        result = app.test_cli_runner().invoke(args=['create-password-reset', 'missing'])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(read_accounts(self.path), self.users)

    def login_legacy_account(self):
        response = self.client.post('/login-process', data={
            'username': 'student', 'password': 'original-password', '_csrf_token': self.csrf})
        self.assertEqual(response.location, '/account/email')
        page = self.client.get('/account/email')
        self.assertIn(b'+60123456789', page.data)
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf_token']

    def test_legacy_login_requires_email_and_preserves_whatsapp_and_profile(self):
        self.login_legacy_account()
        self.assertEqual(self.client.get('/dashboard').location, '/account/email')
        self.assertEqual(self.client.get('/learning-resources').location, '/account/email')
        response = self.client.post('/account/email', data={
            '_csrf_token': self.csrf, 'email': 'student@example.com', 'password': 'original-password'})
        self.assertEqual(response.location, '/dashboard')
        expected = dict(self.users[1], email='student@example.com', whatsapp='+60123456789',
                        contact_method='email', contact_value='student@example.com')
        self.assertEqual(read_accounts(self.path), [self.users[0], expected])
        with self.client.session_transaction() as session:
            self.assertNotIn('require_email', session)
        self.assertEqual(self.client.get('/account/email').location, '/dashboard')
        with patch('password_reset.send_email') as send:
            deliver_reset(dict(app.config), 'student', app.logger)
        self.assertEqual(send.call_args.args[1], 'student@example.com')

    def test_adding_email_requires_login_csrf_valid_details_and_current_password(self):
        self.assertEqual(self.client.get('/account/email').location, '/login')
        self.login_legacy_account()
        for values in ({'email': ''}, {'email': 'invalid'}, {'whatsapp': 'abc'},
                       {'password': 'wrong'}, {'_csrf_token': 'wrong'}):
            response = self.client.post('/account/email', data={
                '_csrf_token': self.csrf, 'email': 'student@example.com',
                'password': 'original-password', **values})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(read_accounts(self.path), self.users)

    def test_existing_email_cannot_be_overwritten_using_completion_route(self):
        with self.client.session_transaction() as session:
            session['user_id'] = 1
        response = self.client.post('/account/email', data={
            '_csrf_token': self.csrf, 'email': 'attacker@example.com', 'password': 'original-password'})
        self.assertEqual(response.location, '/coordinator')
        self.assertEqual(read_accounts(self.path), self.users)

    def test_recovery_prefers_email_over_legacy_contact_value(self):
        with edit_accounts(self.path) as users:
            users[0]['email'] = 'recovery@example.com'
            users[0]['whatsapp'] = '+60123456789'
        with patch('password_reset.send_email') as send:
            deliver_reset(dict(app.config), 'mentor', app.logger)
        self.assertEqual(send.call_args.args[1], 'recovery@example.com')


if __name__ == '__main__':
    unittest.main()
