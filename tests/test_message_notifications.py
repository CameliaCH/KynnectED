"""Web Push subscriptions, encrypted delivery, retry queues, and account isolation."""
import base64
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import http_ece
from pywebpush import WebPushException
import requests

from account_store import edit_accounts
from app import app
from learning import get_db
from push_notifications import deliver_one
import test_learning


class MessageNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device_key = ec.generate_private_key(ec.SECP256R1())
        cls.auth_secret = b'0123456789abcdef'
        raw = cls.device_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        cls.keys = {name: base64.urlsafe_b64encode(value).rstrip(b'=').decode()
                    for name, value in [('p256dh', raw), ('auth', cls.auth_secret)]}

    def setUp(self):
        self.fixture = test_learning.LearningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.auth = self.fixture.auth
        previous = {key: app.config[key] for key in ('PUSH_ENABLED', 'PUSH_PRIVATE_KEY_FILE')}
        self.addCleanup(lambda: app.config.update(previous))
        app.config.update(PUSH_ENABLED=True, PUSH_PRIVATE_KEY_FILE=str(self.fixture.root / 'push.pem'))
        result = app.test_cli_runner().invoke(args=['setup-push'])
        self.assertEqual(result.exit_code, 0, result.output)

    def api(self, action, user_id=3, **data):
        return self.client.post('/api/push/' + action, json={'user_id': user_id, **data},
                                headers={'X-CSRF-Token': 'test-csrf'})

    def subscribe(self, user_id=3, device='device1'):
        self.auth(user_id)
        subscription = {'endpoint': 'https://web.push.apple.com/' + device, 'keys': self.keys}
        response = self.api('subscribe', user_id, subscription=subscription)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return subscription

    def send(self, sender=1, mentee=3):
        self.auth(sender)
        response = self.client.post(f'/pairings/1/{mentee}/messages', data={
            '_csrf_token': 'test-csrf', 'message': 'Private message text'})
        self.assertEqual(response.status_code, 200)
        return response.json['messages'][0]['id']

    def rows(self, table):
        with app.app_context():
            return [dict(row) for row in get_db().execute('SELECT * FROM ' + table)]

    def test_setup_is_private_repeatable_and_config_exposes_only_public_key(self):
        path = Path(app.config['PUSH_PRIVATE_KEY_FILE'])
        before = path.read_bytes()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(app.test_cli_runner().invoke(args=['setup-push']).exit_code, 0)
        self.assertEqual(path.read_bytes(), before)
        self.auth(3)
        response = self.client.get('/api/push/config')
        self.assertTrue(response.json['available'])
        self.assertEqual(len(response.json['public_key']), 87)
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        app.config['PUSH_ENABLED'] = False
        self.assertFalse(self.client.get('/api/push/config').json['available'])

    def test_subscriptions_require_login_csrf_identity_and_valid_device_keys(self):
        self.auth(None)
        self.assertEqual(self.client.get('/api/push/config').status_code, 401)
        self.auth(3)
        self.assertEqual(self.client.post('/api/push/subscribe', json={}).status_code, 400)
        self.assertEqual(self.api('subscribe', user_id=1).status_code, 409)
        for endpoint in ('http://web.push.apple.com/device', 'https://127.0.0.1/test',
                         'https://web.push.apple.com.evil.test/test',
                         'https://user@web.push.apple.com/device', 'https://web.push.apple.com:8080/device'):
            response = self.api('subscribe', subscription={'endpoint': endpoint, 'keys': self.keys})
            self.assertEqual(response.status_code, 400, endpoint)
        self.assertEqual(self.api('subscribe', subscription={'endpoint': 'https://web.push.apple.com/a',
                                                             'keys': {'auth': 'broken', 'p256dh': 'broken'}}).status_code, 400)
        self.assertEqual(self.rows('push_subscriptions'), [])

    def test_message_queues_only_recipient_devices_and_keeps_existing_chat(self):
        self.subscribe(3, 'phone')
        self.subscribe(3, 'laptop')
        self.subscribe(1, 'sender')
        self.subscribe(4, 'other-student')
        message_id = self.send()
        jobs = self.rows('push_outbox')
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job['recipient_id'] for job in jobs}, {3})
        self.assertEqual({job['message_id'] for job in jobs}, {message_id})
        self.assertEqual(self.rows('messages')[0]['body'], 'Private message text')
        with patch('push_notifications.webpush') as send, app.app_context():
            self.assertTrue(deliver_one())
            self.assertTrue(deliver_one())
            self.assertFalse(deliver_one())
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertGreater(call.kwargs['ttl'], 0)
            self.assertNotIn('Private message text', call.kwargs['data'])
            self.assertEqual(json.loads(call.kwargs['data'])['url'], '/pairings/1/3#chat')
        self.assertEqual(self.rows('push_outbox'), [])

    def test_real_library_encrypts_and_signs_without_contacting_provider(self):
        self.subscribe()
        self.send()
        response = requests.Response()
        response.status_code = 201
        with patch('push_notifications.NoRedirectSession.post', return_value=response) as post, app.app_context():
            self.assertTrue(deliver_one())
        sent = post.call_args.kwargs
        self.assertIn('Authorization', sent['headers'])
        self.assertGreater(int(sent['headers']['ttl']), 0)
        plain = http_ece.decrypt(sent['data'], private_key=self.device_key,
                                 auth_secret=self.auth_secret, version='aes128gcm')
        self.assertEqual(json.loads(plain)['url'], '/pairings/1/3#chat')
        self.assertEqual(self.rows('push_outbox'), [])

    def test_provider_failure_retries_without_losing_message(self):
        self.subscribe()
        self.send()
        with patch('push_notifications.webpush', side_effect=OSError('offline')), app.app_context():
            self.assertTrue(deliver_one())
            self.assertFalse(deliver_one())
        self.assertEqual(len(self.rows('messages')), 1)
        job = self.rows('push_outbox')[0]
        self.assertEqual(job['attempts'], 1)
        self.assertGreater(job['next_attempt'], time.time())
        with app.app_context(), get_db() as db:
            db.execute('UPDATE push_outbox SET next_attempt = 0')
        with patch('push_notifications.webpush') as send:
            result = app.test_cli_runner().invoke(args=['deliver-push'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.rows('push_outbox'), [])

    def test_repeated_subscribe_preserves_queued_alerts_and_device_identity(self):
        self.subscribe()
        self.send()
        before = self.rows('push_outbox')
        device_id = self.rows('push_subscriptions')[0]['id']
        self.subscribe()
        self.assertEqual(self.rows('push_outbox'), before)
        self.assertEqual(self.rows('push_subscriptions')[0]['id'], device_id)

    def test_expired_provider_subscription_is_removed(self):
        self.subscribe()
        self.send()
        response = requests.Response()
        response.status_code = 410
        with patch('push_notifications.webpush', side_effect=WebPushException('gone', response=response)), app.app_context():
            self.assertTrue(deliver_one())
        self.assertEqual(self.rows('push_subscriptions'), [])
        self.assertEqual(self.rows('push_outbox'), [])

    def test_unlinked_pairings_and_expired_jobs_are_not_sent(self):
        self.subscribe()
        self.send()
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['partners'] = [4]
        with patch('push_notifications.webpush') as send, app.app_context():
            self.assertTrue(deliver_one())
            send.assert_not_called()
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['partners'] = [3, 4]
        self.send()
        with app.app_context(), get_db() as db:
            db.execute('UPDATE push_outbox SET expires_at = 0')
        with patch('push_notifications.webpush') as send, app.app_context():
            self.assertFalse(deliver_one())
            send.assert_not_called()

    def test_stale_authentication_and_rotated_key_do_not_receive_alerts(self):
        for change in ('auth', 'key'):
            with self.subTest(change=change):
                with edit_accounts(app.config['USERS_FILE']) as users:
                    users[2]['auth_version'] = 0
                self.subscribe()
                self.send()
                if change == 'auth':
                    with edit_accounts(app.config['USERS_FILE']) as users:
                        users[2]['auth_version'] = 1
                else:
                    Path(app.config['PUSH_PRIVATE_KEY_FILE']).unlink()
                    app.test_cli_runner().invoke(args=['setup-push'])
                with patch('push_notifications.webpush') as send, app.app_context():
                    self.assertTrue(deliver_one())
                    send.assert_not_called()

    def test_unsubscribe_logout_and_account_switch_remove_pending_alerts(self):
        subscription = self.subscribe()
        self.send()
        self.auth(1)
        self.api('unsubscribe', user_id=1, endpoint=subscription['endpoint'])
        self.assertEqual(len(self.rows('push_subscriptions')), 1)
        self.auth(3)
        self.assertTrue(self.api('status', endpoint=subscription['endpoint']).json['enabled'])
        self.assertEqual(self.client.post('/logout', data={'_csrf_token': 'test-csrf'}).status_code, 302)
        self.assertEqual(self.rows('push_subscriptions'), [])
        self.assertEqual(self.rows('push_outbox'), [])
        self.subscribe()
        self.send()
        self.subscribe(4)  # Explicit enable on the same browser binds it to the new account.
        self.assertEqual(self.rows('push_subscriptions')[0]['user_id'], 4)
        self.assertEqual(self.rows('push_outbox'), [])

    def test_prompt_manifest_worker_and_profile_completion(self):
        self.auth(3)
        html = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('id="notification-prompt"', html)
        self.assertIn('even when KynnectED is closed', html)
        with self.client.get('/static/manifest.webmanifest') as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.data)['display'], 'standalone')
        with self.client.get('/message-notifications-worker.js') as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("addEventListener('push'", response.get_data(as_text=True))
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[2].pop('centre')
        self.assertEqual(self.client.get('/api/push/config').location, '/account/centre')
        self.assertNotIn('id="notification-prompt"', self.client.get('/account/centre').get_data(as_text=True))
