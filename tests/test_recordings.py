"""R2 access controls and complete upload/review lifecycles, without live credentials."""
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from werkzeug.exceptions import BadRequest

from app import app
from account_store import edit_accounts
from learning import activity_for_user, get_db
from recordings import PART_BYTES, RETENTION_SECONDS, SUCCESS_MESSAGE, probe_cloud_video
import test_learning
import recordings


def provider_error(code):
    return ClientError({'Error': {'Code': code}}, 'FakeOperation')


class FakeR2:
    """Provider-owned object metadata and multipart parts, separate from client requests."""
    def __init__(self):
        self.uploads = {}
        self.objects = {}
        self.signed = []
        self.copies = []
        self.copy_fails = False
        self.complete_calls = 0
        self.next_upload = 0

    def create_multipart_upload(self, **args):
        self.next_upload += 1
        upload_id = f'multipart-{self.next_upload}'
        self.uploads[upload_id] = {**args, 'Parts': []}
        return {'UploadId': upload_id}

    def generate_presigned_url(self, operation, **args):
        self.signed.append((operation, args))
        return 'https://storage.invalid/temporary-signed-operation'

    def head_object(self, **args):
        if args['Key'] not in self.objects:
            raise provider_error('404')
        return deepcopy(self.objects[args['Key']])

    def list_parts(self, **args):
        return {'Parts': deepcopy(self.uploads[args['UploadId']]['Parts']), 'IsTruncated': False}

    def complete_multipart_upload(self, **args):
        upload = self.uploads[args['UploadId']]
        self.complete_calls += 1
        self.objects[args['Key']] = {'ContentLength': sum(part['Size'] for part in upload['Parts']),
            'ContentType': upload['ContentType'], 'Metadata': upload['Metadata'],
            'LastModified': datetime.now(timezone.utc), 'ETag': 'verified-object-etag'}
        return {'ETag': 'verified-object-etag'}

    def abort_multipart_upload(self, **args):
        self.uploads.pop(args['UploadId'], None)
        return {}

    def copy_object(self, **args):
        if self.copy_fails:
            raise provider_error('ServiceUnavailable')
        self.copies.append(args)
        self.objects[args['Key']] = deepcopy(self.objects[args['CopySource']['Key']])
        return {'CopyObjectResult': {'ETag': 'verified-object-etag'}}


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_learning.LearningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.auth = self.fixture.auth
        self.lesson = self.fixture.lesson
        keys = ('R2_BUCKET_NAME', 'R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_ENDPOINT')
        original = {key: app.config[key] for key in keys}
        self.addCleanup(lambda: app.config.update(original))
        app.config.update(R2_BUCKET_NAME='private-test-bucket', R2_ACCOUNT_ID='test-account',
                          R2_ACCESS_KEY_ID='fake-access', R2_SECRET_ACCESS_KEY='fake-secret', R2_ENDPOINT='')
        self.r2 = FakeR2()
        self.real_s3_client = recordings.s3_client
        mock = patch('recordings.s3_client', return_value=self.r2)
        mock.start()
        self.addCleanup(mock.stop)
        self.session = self.fixture.create(past=True)

    def post(self, path, **data):
        return self.client.post(path, json=data, headers={'X-CSRF-Token': 'test-csrf'})

    def start(self, **data):
        values = {'filename': '../../zoom.mp4', 'content_type': 'video/mp4', 'file_size': PART_BYTES + 123,
                  'confirmed': True, **data}
        return self.post(f'/api/lessons/{self.session["id"]}/recording-uploads', **values)

    def upload_row(self, id):
        with app.app_context():
            return dict(get_db().execute('SELECT * FROM recording_uploads WHERE id = ?', (id,)).fetchone())

    def row(self, id):
        with app.app_context():
            return dict(get_db().execute('SELECT * FROM recordings WHERE id = ?', (id,)).fetchone())

    def put_parts(self, upload):
        row = self.upload_row(upload['upload_id'])
        self.r2.uploads[row['multipart_id']]['Parts'] = [{'PartNumber': number,
            'Size': min(PART_BYTES, row['file_size'] - (number - 1) * PART_BYTES), 'ETag': f'part-{number}'}
            for number in range(1, math.ceil(row['file_size'] / PART_BYTES) + 1)]

    def complete(self, upload, seconds=3600):
        self.put_parts(upload)
        with patch('recordings.probe_cloud_video', return_value=seconds):
            response = self.post(upload['complete_url'])
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json['message'], SUCCESS_MESSAGE)
        return self.row(upload['upload_id'])

    def saved(self):
        response = self.start()
        self.assertEqual(response.status_code, 201)
        return self.complete(response.json)

    def test_upload_requires_authenticated_assigned_approved_mentor(self):
        self.auth(None)
        self.assertEqual(self.start().status_code, 401)
        for user in (2, 3, 4, 99):
            self.auth(user)
            self.assertEqual(self.start().status_code, 403)
        self.auth(1)
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['mentor_approval_status'] = 'pending'
        self.assertEqual(self.start().status_code, 403)
        self.assertFalse(self.r2.uploads)

    def test_future_and_cancelled_sessions_cannot_receive_uploads(self):
        self.session = self.fixture.create()
        self.assertEqual(self.start().status_code, 409)
        response = self.client.post(f'/lessons/{self.session["id"]}/cancel', data={'_csrf_token': 'test-csrf'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.start().status_code, 409)
        self.assertFalse(self.r2.uploads)

    def test_missing_or_invalid_configuration_reports_safe_error(self):
        app.config['R2_ACCOUNT_ID'] = ''
        with patch('recordings.s3_client', self.real_s3_client):
            response = self.start()
        self.assertEqual(response.status_code, 503)
        self.assertIn('not configured', response.json['error'])
        app.config.update(R2_ACCOUNT_ID='test-account', R2_ENDPOINT='http://localhost:9000')
        with patch('recordings.s3_client', self.real_s3_client):
            self.assertEqual(self.start().status_code, 503)
        self.assertEqual(self.client.get('/dashboard').status_code, 200)

    def test_session_consent_types_size_and_csrf_validated_before_permission(self):
        for changes in ({'confirmed': False}, {'confirmed': 'true'}, {'file_size': 0}, {'file_size': True},
                        {'file_size': 2 * 1024**3 + 1}, {'content_type': 'text/html'},
                        {'filename': 'bad.html'}, {'filename': ''}, {'filename': 'x.mp4', 'content_type': 'video/webm'}):
            self.assertEqual(self.start(**changes).status_code, 400)
        response = self.client.post(f'/api/lessons/{self.session["id"]}/recording-uploads', json={'confirmed': True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post('/api/lessons/missing/recording-uploads').status_code, 404)
        self.assertFalse(self.r2.uploads)

    def test_correct_mentor_starts_upload_without_attendance_and_random_safe_key(self):
        response = self.start()
        self.assertEqual(response.status_code, 201)
        upload = self.upload_row(response.json['upload_id'])
        self.assertTrue(upload['r2_object_key'].startswith(f'sessions/{self.session["id"]}/'))
        self.assertNotIn('zoom', upload['r2_object_key'])
        self.assertNotIn('..', upload['r2_object_key'])
        self.assertEqual(upload['original_filename'], 'zoom.mp4')
        self.assertEqual(upload['mentor_id'], 1)
        self.assertGreater(upload['consent_at'], 0)
        self.assertNotIn('multipart_id', response.json)
        self.assertNotIn('r2_object_key', response.json)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 0)

    def test_direct_part_permissions_are_scoped_and_bind_size(self):
        upload = self.start().json
        self.assertEqual(self.post(upload['part_url'], part_number=1).status_code, 200)
        operation, args = self.r2.signed[-1]
        self.assertEqual(operation, 'upload_part')
        self.assertEqual(args['ExpiresIn'], 900)
        self.assertEqual(args['Params']['ContentLength'], PART_BYTES)
        self.post(upload['part_url'], part_number=2)
        self.assertEqual(self.r2.signed[-1][1]['Params']['ContentLength'], 123)
        for number in (0, 3, -1, True, '1'):
            self.assertEqual(self.post(upload['part_url'], part_number=number).status_code, 400)
        for user in (2, 3, 4, 99):
            self.auth(user)
            for path in (upload['part_url'], upload['complete_url'], upload['abort_url']):
                self.assertEqual(self.post(path, part_number=1).status_code, 403)

    def test_completion_checks_provider_parts_and_creates_recording_once(self):
        upload = self.start().json
        self.assertEqual(self.post(upload['complete_url']).status_code, 400)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        self.put_parts(upload)
        row = self.upload_row(upload['upload_id'])
        self.r2.uploads[row['multipart_id']]['Parts'][0]['Size'] -= 1
        self.assertEqual(self.post(upload['complete_url']).status_code, 400)
        saved = self.complete(upload, seconds=1800)
        self.assertEqual(saved['expires_at'] - saved['uploaded_at'], RETENTION_SECONDS)
        self.assertEqual(saved['review_status'], 'awaiting_review')
        self.assertEqual((saved['mentor_id'], saved['mentee_id']), (1, 3))
        self.assertEqual(self.post(upload['complete_url']).status_code, 200)
        self.assertEqual(self.r2.complete_calls, 1)
        for user in (1, 3):
            with app.app_context():
                activity = activity_for_user(user)
            self.assertEqual((activity['hours'], activity['completed']), (0.5, 1))
        self.assertIsNone(self.lesson(self.session['id'])['recording_filename'])
        self.assertFalse(Path(app.config['RECORDINGS_DIR']).exists())

    def test_invalid_video_or_storage_metadata_never_marks_attendance(self):
        upload = self.start().json
        self.put_parts(upload)
        with patch('recordings.probe_cloud_video', side_effect=BadRequest('Invalid video')):
            self.assertEqual(self.post(upload['complete_url']).status_code, 400)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        state = self.upload_row(upload['upload_id'])
        self.r2.objects[state['r2_object_key']]['ContentLength'] += 1
        self.assertEqual(self.post(upload['complete_url']).status_code, 400)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])

    def test_completed_object_recovered_after_response_or_probe_failure(self):
        upload = self.start().json
        self.put_parts(upload)
        with patch('recordings.probe_cloud_video', side_effect=BadRequest('Try again')):
            self.assertEqual(self.post(upload['complete_url']).status_code, 400)
        with patch('recordings.probe_cloud_video', return_value=3600):
            self.assertEqual(self.post(upload['complete_url']).status_code, 200)
        self.assertEqual(self.r2.complete_calls, 1)

    def test_lost_provider_completion_response_is_recoverable(self):
        upload = self.start().json
        self.put_parts(upload)
        complete = self.r2.complete_multipart_upload
        def lost_response(**args):
            complete(**args)
            raise provider_error('ServiceUnavailable')
        with patch.object(self.r2, 'complete_multipart_upload', side_effect=lost_response):
            self.assertEqual(self.post(upload['complete_url']).status_code, 503)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        with patch('recordings.probe_cloud_video', return_value=3600):
            self.assertEqual(self.post(upload['complete_url']).status_code, 200)
        self.assertEqual(self.r2.complete_calls, 1)

    def test_pairing_changes_during_verification_prevent_finalization(self):
        upload = self.start().json
        self.put_parts(upload)
        def unlink_during_probe(_):
            with edit_accounts(app.config['USERS_FILE']) as users:
                users[0]['partners'].remove(3)
            return 3600
        with patch('recordings.probe_cloud_video', side_effect=unlink_during_probe):
            self.assertEqual(self.post(upload['complete_url']).status_code, 403)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        with app.app_context():
            self.assertEqual(get_db().execute('SELECT COUNT(*) FROM recordings').fetchone()[0], 0)

    def test_processing_upload_cannot_be_cancelled_or_overwritten(self):
        upload = self.start().json
        with app.app_context(), get_db() as db:
            db.execute("UPDATE recording_uploads SET status = 'processing', processing_at = ? WHERE id = ?",
                       (int(time.time()), upload['upload_id']))
        self.assertEqual(self.post(upload['complete_url']).status_code, 409)
        self.assertEqual(self.post(upload['abort_url']).status_code, 409)
        self.assertEqual(self.post(upload['part_url'], part_number=1).status_code, 409)
        self.assertEqual(self.start().status_code, 409)
        self.assertEqual(self.upload_row(upload['upload_id'])['status'], 'processing')

    def test_playback_only_assigned_participants_and_coordinators(self):
        row = self.saved()
        url = f'/api/recordings/{row["id"]}/playback'
        for user in (2, 4):
            self.auth(user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.get(f'/recordings/{row["id"]}').status_code, 403)
        self.auth(None)
        self.assertEqual(self.client.get(url).status_code, 401)
        for user in (1, 3, 99):
            self.auth(user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['expires_in'], 1200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(self.r2.signed[-1][0], 'get_object')
            self.assertEqual(self.r2.signed[-1][1]['Params']['Key'], row['r2_object_key'])
            self.assertEqual(self.client.get(f'/recordings/{row["id"]}').status_code, 200)

    def test_admin_role_prevents_all_upload_actions_even_for_assigned_mentor(self):
        upload = self.start().json
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['is_coordinator'] = True
        for user in (1, 99):
            self.auth(user)
            self.assertEqual(self.start().status_code, 403)
            self.assertEqual(self.post(upload['part_url'], part_number=1).status_code, 403)
            self.assertEqual(self.post(upload['complete_url']).status_code, 403)
            self.assertEqual(self.post(upload['abort_url']).status_code, 403)
            room = self.client.get('/pairings/1/3').get_data(as_text=True)
            self.assertIn('Read-only admin view.', room)
            self.assertNotIn('data-recording-version', room)
        self.assertEqual(self.upload_row(upload['upload_id'])['status'], 'pending')
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        self.assertEqual(self.r2.signed, [])

    def test_expired_and_lifecycle_missing_objects_fail_without_losing_history(self):
        row = self.saved()
        with app.app_context(), get_db() as db:
            db.execute('UPDATE recordings SET expires_at = ? WHERE id = ?', (int(time.time()) - 1, row['id']))
        self.auth(3)
        self.assertEqual(self.client.get(f'/api/recordings/{row["id"]}/playback').status_code, 410)
        for path in ('/dashboard', '/pairings/1/3', f'/recordings/{row["id"]}'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('expired', response.get_data(as_text=True))
            self.assertNotIn('private-recording-video', response.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(activity_for_user(3)['hours'], 1)
        self.auth(99)
        self.assertEqual(self.client.get('/coordinator/lessons').status_code, 200)
        self.assertEqual(self.client.post(f'/recordings/{row["id"]}/flag', data={'_csrf_token': 'test-csrf', 'confirm_flag': 'yes'}).status_code, 410)
        with app.app_context(), get_db() as db:
            db.execute('UPDATE recordings SET expires_at = ? WHERE id = ?', (int(time.time()) + 50, row['id']))
        response = self.client.get(f'/api/recordings/{row["id"]}/playback')
        self.assertLessEqual(response.json['expires_in'], 50)
        self.r2.objects.clear()
        self.assertEqual(self.client.get(f'/api/recordings/{row["id"]}/playback').status_code, 410)

    def test_coordinator_actions_require_coordinator_and_confirmation(self):
        row = self.saved()
        for user in (1, 2, 3, 4):
            self.auth(user)
            for action in ('review', 'flag'):
                response = self.client.post(f'/recordings/{row["id"]}/{action}', data={'_csrf_token': 'test-csrf', 'confirm_flag': 'yes'})
                self.assertEqual(response.status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.post(f'/recordings/{row["id"]}/flag', data={'_csrf_token': 'test-csrf'}).status_code, 400)
        self.assertFalse(self.row(row['id'])['safeguarding_flag'])

    def test_final_review_comments_are_shared_with_paired_participants(self):
        row = self.saved()
        self.auth(99)
        response = self.client.post(f'/recordings/{row["id"]}/review', data={'_csrf_token': 'test-csrf', 'review_note': '<script>Shared review</script>'})
        self.assertEqual(response.status_code, 302)
        reviewed = self.row(row['id'])
        self.assertEqual(reviewed['review_status'], 'reviewed')
        self.assertEqual(reviewed['reviewed_by'], 99)
        self.assertEqual(reviewed['review_submitted_by'], 99)
        self.assertGreater(reviewed['reviewed_at'], 0)
        for path in ('/coordinator/lessons', f'/recordings/{row["id"]}'):
            self.assertIn('&lt;script&gt;Shared review&lt;/script&gt;', self.client.get(path).get_data(as_text=True))
        watch = self.client.get(f'/recordings/{row["id"]}').get_data(as_text=True)
        self.assertNotIn('<textarea', watch)
        self.assertNotIn('Mark reviewed', watch)
        self.assertIn(f'/coordinator/lessons#recording-{row["id"]}', watch)
        monitor = self.client.get('/coordinator/lessons').get_data(as_text=True)
        self.assertNotIn(f'action="/recordings/{row["id"]}/review"', monitor)
        self.assertNotIn('Mark reviewed', monitor)
        for user in (1, 3):
            self.auth(user)
            for path in ('/dashboard', '/pairings/1/3', f'/recordings/{row["id"]}'):
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn('&lt;script&gt;Shared review&lt;/script&gt;', html)
                self.assertIn('Reviewed by Person99 Example', html)
                self.assertNotIn('<script>Shared review</script>', html)
                self.assertNotIn('Mark reviewed', html)
                self.assertNotIn(row['r2_object_key'], html)
                self.assertNotIn('fake-secret', html)
                self.assertNotIn('private-test-bucket', html)

    def test_final_review_is_immutable_and_reviewer_must_be_an_admin(self):
        row = self.saved()
        with edit_accounts(app.config['USERS_FILE']) as users:
            users.append({'id': 100, 'username': 'reviewer', 'name': ['Assigned', 'Reviewer'],
                          'role': 'staff', 'is_coordinator': True})
        self.auth(99)
        path = f'/recordings/{row["id"]}/review'
        for reviewer in ('1', '3', '123456', 'invalid', ''):
            self.assertEqual(self.client.post(path, data={'_csrf_token': 'test-csrf',
                             'reviewed_by': reviewer, 'review_note': 'Comments'}).status_code, 400)
            self.assertIsNone(self.row(row['id'])['reviewed_at'])
        html = self.client.get('/coordinator/lessons').get_data(as_text=True)
        self.assertIn('value="100"', html)
        self.assertIn('Assigned Reviewer', html)
        self.assertNotIn('value="1"', html)
        self.assertEqual(self.client.post(path, data={'_csrf_token': 'test-csrf',
                         'reviewed_by': '100', 'review_note': 'Final comments'}).status_code, 302)
        reviewed = self.row(row['id'])
        self.assertEqual((reviewed['reviewed_by'], reviewed['review_submitted_by'], reviewed['reviewer_name']),
                         (100, 99, 'Assigned Reviewer'))
        for user in (99, 100):
            self.auth(user)
            self.assertEqual(self.client.post(path, data={'_csrf_token': 'test-csrf',
                             'reviewed_by': '99', 'review_note': 'Changed'}).status_code, 409)
            self.assertEqual(self.row(row['id']), reviewed)
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[-1]['name'] = ['Changed', 'Name']
        self.auth(3)
        self.assertIn('Reviewed by Assigned Reviewer', self.client.get('/pairings/1/3').get_data(as_text=True))
        self.assertIn('Final comments', self.client.get('/dashboard').get_data(as_text=True))
        for user in (2, 4):
            self.auth(user)
            self.assertEqual(self.client.get(f'/recordings/{row["id"]}').status_code, 403)
            self.assertNotIn('Final comments', self.client.get('/dashboard').get_data(as_text=True))
        self.auth(1)
        self.assertEqual(self.start(replace=True, recording_version=row['id']).status_code, 409)
        self.assertNotIn('Replace this recording', self.client.get('/pairings/1/3').get_data(as_text=True))
        self.assertEqual(self.flag(row).status_code, 302)
        flagged = self.row(row['id'])
        for key in ('reviewed_at', 'reviewed_by', 'review_note', 'review_submitted_by', 'reviewer_name'):
            self.assertEqual(flagged[key], reviewed[key])
        self.auth(3)
        self.assertNotIn('PRIVATE concern', self.client.get(f'/recordings/{row["id"]}').get_data(as_text=True))

    def test_review_during_replacement_prevents_overwriting_the_final_review(self):
        row = self.saved()
        upload = self.start(replace=True, recording_version=row['id']).json
        self.put_parts(upload)
        def review_during_probe(_):
            with app.app_context(), get_db() as db:
                now = int(time.time())
                db.execute('UPDATE recordings SET reviewed_at = ?, reviewed_by = 99, review_note = ? WHERE id = ?',
                           (now, 'Final review', row['id']))
                db.execute('UPDATE lessons SET reviewed_at = ?, reviewed_by = 99, review_note = ? WHERE id = ?',
                           (now, 'Final review', self.session['id']))
            return 1800
        with patch('recordings.probe_cloud_video', side_effect=review_during_probe):
            self.assertEqual(self.post(upload['complete_url']).status_code, 409)
        self.assertTrue(self.row(row['id'])['active'])
        self.assertEqual(self.row(row['id'])['review_note'], 'Final review')
        self.assertEqual(self.lesson(self.session['id'])['recording_seconds'], 3600)

    def flag(self, row):
        self.auth(99)
        return self.client.post(f'/recordings/{row["id"]}/flag', data={
            '_csrf_token': 'test-csrf', 'confirm_flag': 'yes', 'safeguarding_note': 'PRIVATE concern'})

    def test_flag_copies_and_verifies_retained_object_before_updating_metadata(self):
        row = self.saved()
        self.assertEqual(self.flag(row).status_code, 302)
        flagged = self.row(row['id'])
        self.assertEqual(flagged['r2_object_key'], row['r2_object_key'].replace('sessions/', 'flagged/', 1))
        self.assertIn(flagged['r2_object_key'], self.r2.objects)
        self.assertEqual(flagged['review_status'], 'flagged')
        self.assertEqual(flagged['safeguarding_flag'], 1)
        self.assertEqual(flagged['flagged_by'], 99)
        self.assertGreater(flagged['flagged_at'], 0)
        self.assertEqual(self.flag(row).status_code, 302)
        self.assertEqual(len(self.r2.copies), 1)
        self.client.post(f'/recordings/{row["id"]}/review', data={'_csrf_token': 'test-csrf'})
        self.assertEqual(self.row(row['id'])['review_status'], 'flagged')
        with app.app_context(), get_db() as db:
            db.execute('UPDATE recordings SET expires_at = ? WHERE id = ?', (int(time.time()) - 10, row['id']))
        for user in (1, 3, 99):
            self.auth(user)
            self.assertEqual(self.client.get(f'/api/recordings/{row["id"]}/playback').status_code, 200)
            html = self.client.get(f'/recordings/{row["id"]}').get_data(as_text=True)
            self.assertEqual('PRIVATE concern' in html, user == 99)
        self.auth(4)
        self.assertEqual(self.client.get(f'/api/recordings/{row["id"]}/playback').status_code, 403)

    def test_failed_copy_or_verification_never_falsely_flags_recording(self):
        row = self.saved()
        self.r2.copy_fails = True
        self.assertEqual(self.flag(row).status_code, 503)
        self.assertEqual(self.row(row['id']), row)
        self.r2.copy_fails = False
        original_head = self.r2.head_object
        def bad_copy(**args):
            head = original_head(**args)
            if args['Key'].startswith('flagged/'):
                head['ContentLength'] = 0
            return head
        with patch.object(self.r2, 'head_object', side_effect=bad_copy):
            self.assertEqual(self.flag(row).status_code, 503)
        self.assertEqual(self.row(row['id']), row)

    def test_deliberate_replacement_updates_hours_once_and_retains_flagged_history(self):
        row = self.saved()
        self.flag(row)
        self.auth(1)
        self.assertEqual(self.start().status_code, 409)
        self.assertEqual(self.start(replace=True, recording_version='old').status_code, 409)
        upload = self.start(replace=True, recording_version=row['id']).json
        second = self.complete(upload, seconds=1800)
        with app.app_context():
            self.assertEqual((activity_for_user(1)['hours'], activity_for_user(3)['completed']), (0.5, 1))
        old = self.row(row['id'])
        self.assertFalse(old['active'])
        self.assertTrue(old['safeguarding_flag'])
        self.assertIn(old['r2_object_key'], self.r2.objects)
        self.assertEqual(second['review_status'], 'awaiting_review')
        self.assertIsNone(self.lesson(self.session['id'])['reviewed_at'])
        self.assertEqual(self.client.get(f'/api/recordings/{old["id"]}/playback').status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.get(f'/api/recordings/{old["id"]}/playback').status_code, 200)
        self.assertIn('Previous version', self.client.get('/coordinator/lessons').get_data(as_text=True))

    def test_cancel_restart_expiry_and_inactive_pairing(self):
        upload = self.start().json
        second = self.start().json
        self.assertEqual(self.upload_row(upload['upload_id'])['status'], 'aborted')
        self.assertEqual(self.post(upload['part_url'], part_number=1).status_code, 410)
        self.assertEqual(self.post(second['abort_url']).status_code, 200)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])
        upload = self.start().json
        with app.app_context(), get_db() as db:
            db.execute('UPDATE recording_uploads SET created_at = 0 WHERE id = ?', (upload['upload_id'],))
        self.assertEqual(self.post(upload['complete_url']).status_code, 410)
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['partners'].remove(3)
        self.assertEqual(self.start().status_code, 403)

    def test_expired_uploads_release_slots_without_deleting_retained_videos(self):
        uploads = [self.start().json]
        for date in ('2026-09-01T10:00', '2026-09-02T10:00'):
            self.session = self.fixture.create(scheduled_at=date)
            uploads.append(self.start().json)
        self.session = self.fixture.create(scheduled_at='2026-09-03T10:00')
        self.assertEqual(self.start().status_code, 409)
        with app.app_context(), get_db() as db:
            db.execute('UPDATE recording_uploads SET created_at = 0')
        self.assertEqual(self.start().status_code, 201)
        for upload in uploads:
            self.assertEqual(self.upload_row(upload['upload_id'])['status'], 'aborted')

    def test_unlink_revokes_new_playback_but_coordinator_keeps_history(self):
        row = self.saved()
        with edit_accounts(app.config['USERS_FILE']) as users:
            users[0]['partners'].remove(3)
        for user in (1, 3):
            self.auth(user)
            self.assertEqual(self.client.get(f'/api/recordings/{row["id"]}/playback').status_code, 403)
        self.auth(99)
        self.assertEqual(self.client.get(f'/api/recordings/{row["id"]}/playback').status_code, 200)

    def test_old_server_upload_route_is_disabled_without_reading_video(self):
        self.assertEqual(self.client.post(f'/lessons/{self.session["id"]}/recording', data=b'video bytes').status_code, 410)
        self.assertIsNone(self.lesson(self.session['id'])['recorded_at'])

    def test_schema_addition_preserves_existing_local_recordings(self):
        self.fixture.seed_legacy_recording(self.session['id'])
        with app.app_context(), get_db() as db:
            db.execute('DROP TABLE recording_uploads')
            db.execute('DROP TABLE recordings')
        self.auth(3)
        response = self.client.get(f'/lessons/{self.session["id"]}/recording', headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'test')
        response.close()
        self.auth(1)
        legacy = self.lesson(self.session['id'])
        replacement = self.start(replace=True, recording_version=legacy['recording_filename'])
        self.assertEqual(replacement.status_code, 201)
        self.complete(replacement.json, seconds=900)
        with app.app_context():
            self.assertEqual(activity_for_user(3)['hours'], 0.25)

    def test_real_sdk_presigns_content_length_and_only_cloudflare_endpoint(self):
        client = boto3.client('s3', endpoint_url='https://test-account.r2.cloudflarestorage.com',
            aws_access_key_id='test-access', aws_secret_access_key='test-secret', region_name='auto',
            config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
                          request_checksum_calculation='when_required'))
        upload = self.start().json
        with patch('recordings.s3_client', return_value=client):
            response = self.post(upload['part_url'], part_number=1)
        self.assertEqual(response.status_code, 200)
        parsed = urlsplit(response.json['url'])
        self.assertEqual(parsed.hostname, 'test-account.r2.cloudflarestorage.com')
        params = parse_qs(parsed.query)
        self.assertIn('content-length', params['X-Amz-SignedHeaders'][0])
        self.assertEqual(params['partNumber'], ['1'])
        self.assertNotIn('test-secret', response.json['url'])

    def test_remote_probe_uses_trusted_signed_url_and_never_client_duration(self):
        upload = self.start().json
        row = self.upload_row(upload['upload_id'])
        metadata = '{"format":{"duration":"3600"},"streams":[{"codec_type":"video","codec_name":"h264"}]}'
        with app.app_context(), patch('recordings.subprocess.run', return_value=subprocess.CompletedProcess([], 0, metadata)) as run:
            self.assertEqual(probe_cloud_video(row), 3600)
        command = run.call_args.args[0]
        self.assertIn('https,tls,tcp', command)
        self.assertIn('mov', command)
        self.assertEqual(command[-1], 'https://storage.invalid/temporary-signed-operation')
        for metadata in ('{}', '{"format":{"duration":"nan"},"streams":[]}', '{"format":{"duration":"-1"},"streams":[]}'):
            with app.app_context(), patch('recordings.subprocess.run', return_value=subprocess.CompletedProcess([], 0, metadata)):
                with self.assertRaises(BadRequest):
                    probe_cloud_video(row)
