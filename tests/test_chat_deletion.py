import unittest

from account_store import edit_accounts
from app import app
from learning import get_db
import test_learning


class ChatDeletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_learning.LearningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.auth = self.fixture.auth

    def send(self, body='Message to remove', mentee=3):
        self.auth(1)
        response = self.client.post(f'/pairings/1/{mentee}/messages', data={
            '_csrf_token': 'test-csrf', 'message': body})
        self.assertEqual(response.status_code, 200)
        return response.json['messages'][0]['id']

    def delete(self, message_id, mentee=3, token='test-csrf'):
        return self.client.post(f'/coordinator/pairings/1/{mentee}/messages/{message_id}/delete',
                                data={'_csrf_token': token})

    def test_enabling_deletion_preserves_existing_conversations_in_multiple_rooms(self):
        first = self.send('Existing conversation in first room')
        second = self.send('Existing conversation in second room', mentee=4)
        with app.app_context(), get_db() as db:
            # Simulate a database from before the moderation feature was added.
            db.execute('DROP TABLE message_deletions')
        self.auth(99)
        for mentee, message_id in [(3, first), (4, second)]:
            page = self.client.get(f'/pairings/1/{mentee}').get_data(as_text=True)
            self.assertIn(f'data-message-id="{message_id}"', page)
            self.assertIn('id="chat-empty" class="availability-help" hidden', page)
            data = self.client.get(f'/pairings/1/{mentee}/messages').json
            self.assertEqual([row['id'] for row in data['messages']], [message_id])
            self.assertEqual(data['deleted_ids'], [])

    def test_only_coordinators_can_see_controls_and_delete_even_own_messages(self):
        message_id = self.send()
        for user_id in (1, 3, 2):
            self.auth(user_id)
            self.assertEqual(self.delete(message_id).status_code, 403)
            self.assertEqual(self.client.get('/coordinator/pairings/1/3').status_code, 403)
            if user_id != 2:
                html = self.client.get('/pairings/1/3').get_data(as_text=True)
                self.assertNotIn('data-delete-message=', html)
                self.assertNotIn('data-can-delete="true"', html)
        self.auth(None)
        self.assertEqual(self.delete(message_id).status_code, 401)
        self.auth(99)
        html = self.client.get('/coordinator/pairings/1/3').get_data(as_text=True)
        self.assertIn(f'data-delete-message="{message_id}"', html)
        self.assertEqual(self.delete(message_id).status_code, 200)

    def test_coordinator_uses_review_room_without_enabling_deletion_in_personal_room(self):
        message_id = self.send()
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['is_coordinator'] = True
        self.auth(1)
        pairings = self.client.get('/coordinator/pairings').get_data(as_text=True)
        self.assertIn('href="/coordinator/pairings/1/3"', pairings)
        dashboard = self.client.get('/dashboard').get_data(as_text=True)
        self.assertIn('href="/pairings/1/3"', dashboard)
        review = self.client.get('/coordinator/pairings/1/3').get_data(as_text=True)
        self.assertIn(f'data-delete-message="{message_id}"', review)
        self.assertIn('data-delete-messages-url="/coordinator/pairings/1/3/messages"', review)
        # Opening the review room must not enable moderation in other tabs.
        personal = self.client.get('/pairings/1/3').get_data(as_text=True)
        self.assertIn(f'data-message-id="{message_id}"', personal)
        self.assertIn('id="chat-form"', personal)
        self.assertNotIn('data-delete-message=', personal)
        self.assertNotIn('data-can-delete=', personal)
        self.assertEqual(self.client.post(f'/pairings/1/3/messages/{message_id}/delete',
                                         data={'_csrf_token': 'test-csrf'}).status_code, 404)
        self.assertEqual(self.delete(message_id).status_code, 200)

    def test_deletion_removes_content_and_queued_push_only_for_selected_message(self):
        removed = self.send()
        kept = self.send('Keep this message')
        with app.app_context(), get_db() as db:
            db.execute('INSERT INTO push_outbox(message_id, subscription_id, recipient_id, mentor_id, mentee_id, expires_at, next_attempt) VALUES (?, 1, 3, 1, 3, 9999999999, 0)', (removed,))
            db.execute('INSERT INTO push_outbox(message_id, subscription_id, recipient_id, mentor_id, mentee_id, expires_at, next_attempt) VALUES (?, 1, 3, 1, 3, 9999999999, 0)', (kept,))
        self.auth(99)
        response = self.delete(removed)
        self.assertEqual(response.json, {'messages': [], 'deleted_ids': [removed]})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with app.app_context():
            self.assertIsNone(get_db().execute('SELECT * FROM messages WHERE id = ?', (removed,)).fetchone())
            record = dict(get_db().execute('SELECT * FROM message_deletions WHERE message_id = ?', (removed,)).fetchone())
            self.assertEqual(record['deleted_by'], 99)
            self.assertNotIn('body', record)
            self.assertEqual([row['message_id'] for row in get_db().execute('SELECT message_id FROM push_outbox')], [kept])
        self.auth(3)
        self.assertNotIn('Message to remove', self.client.get('/pairings/1/3').get_data(as_text=True))
        for query in ('', f'?before={kept + 1}', '?after=0'):
            rows = self.client.get('/pairings/1/3/messages' + query).json['messages']
            self.assertEqual([row['id'] for row in rows], [kept])

    def test_csrf_pair_scope_and_missing_messages(self):
        message_id = self.send()
        self.auth(99)
        self.assertEqual(self.delete(message_id, token='invalid').status_code, 400)
        self.assertEqual(self.delete(message_id, mentee=4).status_code, 404)
        self.assertEqual(self.delete(message_id + 100).status_code, 404)
        self.assertEqual(self.client.get(f'/coordinator/pairings/1/3/messages/{message_id}/delete').status_code, 405)
        self.assertEqual(self.delete(message_id).status_code, 200)
        self.assertEqual(self.delete(message_id).status_code, 404)

    def test_polling_tracks_deletions_in_deletion_order_and_only_within_pair(self):
        older = self.send('Older message')
        newer = self.send('Newer message')
        other = self.send(mentee=4)
        self.auth(99)
        self.delete(newer)
        first = self.client.get(f'/pairings/1/3/messages?after={newer}').json
        self.assertEqual(first['deleted_ids'], [newer])
        self.delete(older)
        self.delete(other, mentee=4)
        self.auth(3)
        second = self.client.get(f'/pairings/1/3/messages?after={newer}&deletions_after={first["deletion_cursor"]}').json
        self.assertEqual(second['messages'], [])
        self.assertEqual(second['deleted_ids'], [older])
        self.assertGreater(second['deletion_cursor'], first['deletion_cursor'])
        for value in ('invalid', '-1', str(2**63)):
            self.assertEqual(self.client.get('/pairings/1/3/messages?deletions_after=' + value).status_code, 400)

    def test_coordinators_can_moderate_inactive_pairings_without_gaining_chat_permission(self):
        message_id = self.send()
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['partners'] = [4]
        self.auth(99)
        self.assertEqual(self.delete(message_id).status_code, 200)
        self.assertEqual(self.client.post('/pairings/1/3/messages', data={
            '_csrf_token': 'test-csrf', 'message': 'Still cannot send'}).status_code, 403)
