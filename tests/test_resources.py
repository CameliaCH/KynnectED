"""Member library, slide approval, private downloads, and safeguarding resources."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from app import app, RESOURCE_GROUPS
from learning import get_db


class ResourceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "users.json"
        path.write_text(json.dumps([
            {"id": 1, "username": "mentor", "name": ["Test", "Mentor"], "role": "mentor"},
            {"id": 2, "username": "mentee", "name": ["Test", "Student"], "role": "mentee"},
            {"id": 3, "username": "coordinator", "name": ["Test", "Coordinator"], "role": "staff", "is_coordinator": True},
            {"id": 4, "username": "othermentor", "name": ["Other", "Mentor"], "role": "mentor"},
        ]))
        self.root = Path(directory.name)
        previous = {key: app.config[key] for key in ("TESTING", "USERS_FILE", "LEARNING_DATABASE", "MATERIALS_DIR", "MAX_MATERIAL_BYTES")}
        self.addCleanup(lambda: app.config.update(previous))
        app.config.update(TESTING=True, USERS_FILE=str(path), LEARNING_DATABASE=str(self.root / 'learning.sqlite3'),
                          MATERIALS_DIR=str(self.root / 'materials'), MAX_MATERIAL_BYTES=25 * 1024 * 1024)
        self.client = app.test_client()

    def auth(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            if user_id:
                session['user_id'] = user_id
            session['csrf_token'] = 'resource-csrf'

    def upload(self, filename='lesson.pdf', content=b'%PDF-1.4\nTest slides\n%%EOF', **changes):
        response = self.client.post('/learning-resources/submit', data={
            '_csrf_token': 'resource-csrf', 'title': 'Useful lesson slides', 'folder': 'english',
            'material': (io.BytesIO(content), filename), **changes}, content_type='multipart/form-data')
        response.request.close()
        response.request.environ['wsgi.input'].close()
        return response

    def submissions(self):
        with app.app_context():
            return [dict(row) for row in get_db().execute('SELECT * FROM resource_materials ORDER BY rowid')]

    def review(self, material_id, action='approve', **changes):
        return self.client.post(f'/materials/{material_id}/review', data={
            '_csrf_token': 'resource-csrf', 'action': action, **changes})

    def test_folder_urls_require_login(self):
        for folders in RESOURCE_GROUPS.values():
            for slug, _ in folders:
                response = self.client.get(f"/learning-resources/{slug}")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/login")
        self.assertIn('data-locked-folder=', self.client.get('/learning-resources').get_data(as_text=True))

    def test_both_roles_can_open_empty_folders(self):
        for user_id in (1, 2):
            with self.client.session_transaction() as session:
                session['user_id'] = user_id
            for folders in RESOURCE_GROUPS.values():
                for slug, title in folders:
                    response = self.client.get(f"/learning-resources/{slug}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertIn(title, response.get_data(as_text=True))
                    if slug == 'safeguarding-guidance':
                        self.assertIn('Mentor Code of Conduct', response.get_data(as_text=True))
                        self.assertIn('href="/code-of-conduct"', response.get_data(as_text=True))
                        self.assertIn('KynnectED_Mentor_Code_of_Conduct_Website.pdf', response.get_data(as_text=True))
                        self.assertNotIn('No materials yet', response.get_data(as_text=True))
                    else:
                        self.assertIn('materials haven’t been added yet', response.get_data(as_text=True))
            self.assertNotIn('data-locked-folder=', self.client.get('/learning-resources').get_data(as_text=True))
            self.assertEqual(self.client.get('/learning-resources/not-a-folder').status_code, 404)
            self.client.get('/login')
            with self.client.session_transaction() as session:
                token = session['csrf_token']
            self.client.post('/logout', data={'_csrf_token': token})
            self.assertEqual(self.client.get('/learning-resources/english').status_code, 302)

    def test_slides_stay_private_until_coordinator_approval(self):
        self.auth(1)
        response = self.upload(status='approved', uploaded_by='3')
        self.assertEqual(response.status_code, 302)
        material = self.submissions()[0]
        self.assertEqual((material['status'], material['uploaded_by']), ('pending', 1))
        self.assertNotIn('Useful lesson slides', self.client.get('/learning-resources/english').get_data(as_text=True))
        self.assertIn('Useful lesson slides', self.client.get('/learning-resources/submit').get_data(as_text=True))
        self.assertIn('Pending', self.client.get('/learning-resources/submit').get_data(as_text=True))
        url = f'/materials/{material["id"]}/download'
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200)
        self.assertIn('attachment', preview.headers['Content-Disposition'])
        self.assertEqual(preview.headers['Cache-Control'], 'no-store')
        self.assertEqual(preview.headers['X-Content-Type-Options'], 'nosniff')
        preview.close()
        for user_id in (2, 4):
            self.auth(user_id)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.review(material['id']).status_code, 403)
        self.auth(3)
        self.assertIn('Useful lesson slides', self.client.get('/coordinator/materials').get_data(as_text=True))
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200)
        preview.close()
        self.assertEqual(self.review(material['id']).status_code, 302)
        material = self.submissions()[0]
        self.assertEqual((material['status'], material['reviewed_by']), ('approved', 3))
        self.assertIsNotNone(material['reviewed_at'])
        self.assertEqual(self.review(material['id'], action='reject').status_code, 409)
        for user_id in (1, 2, 4):
            self.auth(user_id)
            self.assertIn('Useful lesson slides', self.client.get('/learning-resources/english').get_data(as_text=True))
            response = self.client.get(url)
            self.assertEqual(response.data, b'%PDF-1.4\nTest slides\n%%EOF')
            response.close()
        self.auth(None)
        self.assertEqual(self.client.get(url).location, '/login')
        self.assertEqual(self.client.get('/learning-resources/submit').location, '/login')
        self.assertNotIn('Useful lesson slides', self.client.get('/learning-resources').get_data(as_text=True))

    def test_declined_slides_never_appear_and_feedback_is_private(self):
        self.auth(1)
        self.upload()
        material = self.submissions()[0]
        self.auth(3)
        self.assertEqual(self.review(material['id'], action='reject', review_note='<script>Needs correction</script>').status_code, 302)
        self.auth(1)
        html = self.client.get('/learning-resources/submit').get_data(as_text=True)
        self.assertIn('Declined', html)
        self.assertIn('&lt;script&gt;Needs correction&lt;/script&gt;', html)
        self.assertNotIn('Useful lesson slides', self.client.get('/learning-resources/english').get_data(as_text=True))
        self.auth(2)
        self.assertEqual(self.client.get(f'/materials/{material["id"]}/download').status_code, 403)

    def test_valid_powerpoint_and_file_validation_clean_up_failed_uploads(self):
        self.auth(1)
        for filename, content in [('bad.html', b'<script>x</script>'), ('bad.pdf', b'not a pdf'),
                                  ('empty.pdf', b''), ('bad.pptx', b'not a zip')]:
            self.assertEqual(self.upload(filename, content).status_code, 400)
            self.assertEqual(self.submissions(), [])
            self.assertEqual(list((self.root / 'materials').glob('*')), [])
        powerpoint = io.BytesIO()
        with zipfile.ZipFile(powerpoint, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<Types><Override ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>')
            archive.writestr('ppt/presentation.xml', '<presentation/>')
        self.assertEqual(self.upload('../../lesson.pptx', powerpoint.getvalue()).status_code, 302)
        material = self.submissions()[0]
        self.assertEqual(material['original_filename'], 'lesson.pptx')
        self.assertTrue((self.root / 'materials' / material['filename']).is_file())

    def test_upload_size_fields_roles_and_csrf_are_enforced(self):
        self.auth(1)
        for changes in ({'title': ''}, {'title': 'x' * 121}, {'folder': 'invalid'}, {'_csrf_token': 'wrong'}):
            self.assertEqual(self.upload(**changes).status_code, 400)
        app.config['MAX_MATERIAL_BYTES'] = 1024 * 1024
        self.assertEqual(self.upload(content=b'%PDF-' + b'x' * (1024 * 1024)).status_code, 413)
        self.assertEqual(list((self.root / 'materials').glob('*')), [])
        self.assertEqual(self.submissions(), [])
        self.auth(2)
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.client.get('/learning-resources/submit').status_code, 403)
        self.assertEqual(self.client.get('/coordinator/materials').status_code, 403)
        self.auth(1)
        self.upload()
        material = self.submissions()[0]
        self.auth(3)
        for changes in ({'action': 'unknown'}, {'review_note': 'x' * 1001}, {'_csrf_token': 'wrong'}):
            self.assertEqual(self.review(material['id'], **changes).status_code, 400)
        self.assertEqual(self.submissions()[0]['status'], 'pending')

    def test_library_links_conduct_submission_and_instagram(self):
        self.auth(1)
        html = self.client.get('/learning-resources').get_data(as_text=True)
        self.assertIn('href="/learning-resources/submit"', html)
        self.assertIn('1 material', html)
        self.assertIn('https://www.instagram.com/kynnected_org/', html)
        self.assertNotIn('linkedin.com', html)
        html = self.client.get('/learning-resources/submit?folder=maths').get_data(as_text=True)
        self.assertIn('value="maths" selected', html)
        self.auth(2)
        self.assertNotIn('Submit slides', self.client.get('/learning-resources').get_data(as_text=True))


    def test_mentors_can_upload_to_every_student_folder_but_no_mentor_folder(self):
        self.auth(1)
        form = self.client.get('/learning-resources/submit?folder=mentor-handbook').get_data(as_text=True)
        for slug, _ in RESOURCE_GROUPS['mentors']:
            with self.subTest(folder=slug):
                self.assertNotIn(f'value="{slug}"', form)
                html = self.client.get(f'/learning-resources/{slug}').get_data(as_text=True)
                self.assertNotIn('Submit slides', html)
                self.assertEqual(self.upload(folder=slug, is_coordinator='true').status_code, 403)
                self.assertEqual(self.submissions(), [])
                self.assertEqual(list((self.root / 'materials').glob('*')), [])
        for slug, _ in RESOURCE_GROUPS['students']:
            with self.subTest(folder=slug):
                self.assertIn(f'value="{slug}"', form)
                html = self.client.get(f'/learning-resources/{slug}').get_data(as_text=True)
                self.assertIn(f'/learning-resources/submit?folder={slug}', html)
                self.assertEqual(self.upload(folder=slug).status_code, 302)
        self.assertEqual({row['folder'] for row in self.submissions()},
                         {slug for slug, _ in RESOURCE_GROUPS['students']})
        self.assertTrue(all(row['status'] == 'pending' for row in self.submissions()))

    def test_admins_can_upload_and_approve_in_both_resource_groups(self):
        self.auth(3)
        form = self.client.get('/learning-resources/submit').get_data(as_text=True)
        for folders in RESOURCE_GROUPS.values():
            for slug, _ in folders:
                with self.subTest(folder=slug):
                    self.assertIn(f'value="{slug}"', form)
                    html = self.client.get(f'/learning-resources/{slug}').get_data(as_text=True)
                    self.assertIn(f'/learning-resources/submit?folder={slug}', html)
                    self.assertEqual(self.upload(folder=slug).status_code, 302)
                    material = self.submissions()[-1]
                    self.assertEqual((material['folder'], material['uploaded_by']), (slug, 3))
                    self.assertEqual(self.review(material['id']).status_code, 302)
        self.auth(2)
        for folders in RESOURCE_GROUPS.values():
            for slug, _ in folders:
                self.assertIn('Useful lesson slides', self.client.get(f'/learning-resources/{slug}').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
