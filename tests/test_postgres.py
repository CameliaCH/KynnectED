"""Postgres integration checks use disposable schemas, never the app's tables.

Opt in with KYNNECTED_POSTGRES_TEST_URL. Default test runs skip the live suite.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import copy
import os
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from app import app, ph, LEARNER_GUIDELINES_VERSION
from account_store import read_accounts, edit_accounts, next_account_id
from database import connect, PostgresDatabase, parameter_sql, read_profiles, save_profiles
from password_reset import allow_request
from scripts.migrate_supabase_database import import_snapshot, TABLES, RUN_NAME


class PostgresSQLTests(unittest.TestCase):
    def test_bound_parameters_leave_question_marks_in_literals(self):
        self.assertEqual(parameter_sql("SELECT '?' AS example WHERE name = ? AND note LIKE '50%'"),
                         "SELECT '?' AS example WHERE name = %s AND note LIKE '50%%'")


@unittest.skipUnless(os.environ.get('KYNNECTED_POSTGRES_TEST_URL'), 'Postgres integration URL not set')
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from psycopg import sql
        suffix = uuid4().hex[:12]
        cls.schema = 'kynnected_test_' + suffix
        cls.private = 'kynnected_test_private_' + suffix
        cls.config = dict(DATA_BACKEND='supabase', TESTING=True, SUPABASE_TESTING=True,
                          SUPABASE_DATABASE_URL=os.environ['KYNNECTED_POSTGRES_TEST_URL'],
                          SUPABASE_APP_SCHEMA=cls.schema, SUPABASE_PRIVATE_SCHEMA=cls.private)
        cls.password_hash = ph.hash('integration-password')
        cls.users = []
        for id, role, partners in [(1, 'mentor', [3]), (3, 'mentee', [1]), (99, 'mentor', [])]:
            cls.users.append(dict(id=id, username=f'person{id}', email=f'person{id}@example.com',
                                  role=role, partners=partners, name=[f'Person{id}', 'Test'], subjects=['maths'],
                                  password=cls.password_hash, age=18, gender='female', gender_preferred=['same'],
                                  age_preferred=['same-age'], availability={'monday': ['09:00']}))
        cls.users[-1]['is_coordinator'] = True
        cls.records = {table: [] for table in TABLES}
        cls.records['messages'] = [dict(id=7, mentor_id=101, mentee_id=102, sender_id=101,
                                       body='Archived message', created_at=1)]
        with connect(cls.config) as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        cls.addClassCleanup(cls.cleanup_schemas)
        status, counts = import_snapshot(cls.config, cls.users, cls.records, 'test-fixture', apply=True)
        if status != 'imported' or counts['messages'] != 1:
            raise RuntimeError('Integration fixture import did not complete.')

    @classmethod
    def cleanup_schemas(cls):
        from psycopg import sql
        with connect(cls.config) as db:
            for schema in (cls.schema, cls.private):
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def setUp(self):
        previous = dict(app.config)
        self.addCleanup(lambda: (app.config.clear(), app.config.update(previous)))
        app.config.update(self.config)
        from psycopg import sql
        with connect(self.config) as db:
            with db.transaction():
                for table in ('recording_uploads', 'recordings', 'missing_recording_reports', 'lessons',
                              'resource_materials', 'messages', 'profiles', 'reset_limits'):
                    db.execute(sql.SQL('TRUNCATE {} CASCADE').format(sql.Identifier(table)))
                save_profiles(db, copy.deepcopy(self.users))
        self.client = app.test_client()

    def auth(self, id):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=id, data_backend='supabase', csrf_token='test-csrf')

    def test_profiles_keep_credentials_private_and_roll_back_failed_changes(self):
        with app.app_context():
            self.assertEqual(read_accounts('unused'), self.users)
            with self.assertRaises(RuntimeError):
                with edit_accounts('unused') as users:
                    users[0]['name'] = ['Not committed']
                    raise RuntimeError('rollback')
            self.assertEqual(read_accounts('unused'), self.users)
            with edit_accounts('unused') as users:
                users[0]['school'] = 'Example College'
            self.assertEqual(read_accounts('unused')[0]['school'], 'Example College')
        with connect(self.config) as db:
            self.assertFalse(db.execute("SELECT data ? 'password' AS exposed FROM profiles LIMIT 1").fetchone()['exposed'])
            self.assertEqual(db.execute('SELECT password_hash FROM account_credentials WHERE account_id=1').fetchone()[0], self.password_hash)

    def test_api_roles_have_no_access_to_tables_or_credentials(self):
        with connect(self.config) as db:
            for role in ('anon', 'authenticated', 'service_role'):
                for table in ('profiles', *TABLES):
                    self.assertFalse(db.execute('SELECT has_table_privilege(%s, %s, %s) AS allowed',
                                                (role, self.schema + '.' + table, 'SELECT')).fetchone()['allowed'])
                self.assertFalse(db.execute('SELECT has_schema_privilege(%s, %s, %s) AS allowed',
                                            (role, self.private, 'USAGE')).fetchone()['allowed'])
            self.assertEqual(db.execute('''SELECT count(*) AS count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname=%s AND c.relkind='r' AND NOT c.relrowsecurity''', (self.schema,)).fetchone()['count'], 0)

    def test_login_dashboards_and_exports_use_postgres(self):
        self.client.get('/login')
        with self.client.session_transaction() as session:
            csrf = session['csrf_token']
        response = self.client.post('/login-process', data={
            '_csrf_token': csrf, 'username': 'person99', 'password': 'integration-password'})
        self.assertEqual(response.location, '/coordinator')
        for path in ('/coordinator', '/dashboard', '/coordinator/pairings', '/export',
                     '/coordinator/lessons', '/coordinator/materials', '/export/mentors.csv'):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.auth(1)
        self.assertEqual(self.client.get('/dashboard').status_code, 200)
        self.assertEqual(self.client.get('/coordinator').status_code, 403)

    def test_legacy_sessions_are_invalid_after_switch(self):
        with self.client.session_transaction() as session:
            session['user_id'] = 1
        self.assertEqual(self.client.get('/dashboard').location, '/login')

    def test_lesson_conflict_chat_and_unauthorised_reads(self):
        self.auth(1)
        start = datetime.now(ZoneInfo('Asia/Kuala_Lumpur')) + timedelta(days=2)
        data = dict(_csrf_token='test-csrf', title='Practice', scheduled_at=start.strftime('%Y-%m-%dT%H:%M'),
                    planned_minutes='60', zoom_url='https://zoom.us/j/123456789')
        self.assertEqual(self.client.post('/pairings/1/3/lessons', data=data).status_code, 302)
        self.assertEqual(self.client.post('/pairings/1/3/lessons', data=data).status_code, 409)
        response = self.client.post('/pairings/1/3/messages', data={'_csrf_token': 'test-csrf', 'message': 'Hello ? 50% café'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['messages'][0]['body'], 'Hello ? 50% café')
        self.auth(3)
        self.assertEqual(self.client.get('/pairings/1/3').status_code, 200)
        self.assertEqual(self.client.post('/pairings/1/3/lessons', data=data).status_code, 403)

    def test_concurrent_account_edits_do_not_lose_updates_and_ids_are_not_reused(self):
        def update(field):
            with app.app_context():
                with edit_accounts('unused') as users:
                    users[0][field] = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(update, ['first_update', 'second_update']))
        with app.app_context():
            users = read_accounts('unused')
            self.assertTrue(users[0]['first_update'])
            self.assertTrue(users[0]['second_update'])
            first, second = next_account_id(users), next_account_id(users)
            self.assertGreater(first, 102)
            self.assertGreater(second, first)

    def test_rate_limiting_uses_shared_postgres_counters(self):
        for _ in range(5):
            self.assertTrue(allow_request(app.config, 'person1', '127.0.0.1'))
        self.assertFalse(allow_request(app.config, 'person1', '127.0.0.1'))

    def test_import_rerun_does_not_overwrite_application_changes(self):
        with connect(self.config) as db:
            db.execute("UPDATE profiles SET school='New school' WHERE id=1")
        status, _ = import_snapshot(self.config, self.users, self.records, 'test-fixture', apply=True)
        self.assertEqual(status, 'already_imported')
        with connect(self.config) as db:
            self.assertEqual(db.execute('SELECT school FROM profiles WHERE id=1').fetchone()[0], 'New school')
        with self.assertRaises(ValueError):
            import_snapshot(self.config, self.users, self.records, 'different-snapshot', apply=True)


if __name__ == '__main__':
    unittest.main()
