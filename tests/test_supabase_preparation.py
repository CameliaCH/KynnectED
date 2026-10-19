import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.prepare_supabase_migration import email_mapping, prepare


class SupabasePreparationTests(unittest.TestCase):
    def test_email_mapping_flags_missing_and_duplicate_addresses_without_merging(self):
        users = [dict(id=1, username='first', email='Shared@example.com'),
                 dict(id=2, username='second', contact_method='email', contact_value='shared@example.com'),
                 dict(id=3, username='third', contact_method='whatsapp', contact_value='+60123456789'),
                 dict(id=4, username='fourth', email='unique@example.com')]
        rows = email_mapping(users)
        self.assertEqual([row['status'] for row in rows],
                         ['duplicate_email', 'duplicate_email', 'missing_email', 'ready'])
        self.assertEqual([row['account_id'] for row in rows], [1, 2, 3, 4])
        self.assertNotIn('email', users[2])

    def test_snapshot_preserves_accounts_wal_records_and_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            users_file = root / 'users.json'
            original = json.dumps([dict(id=7, username='member', password='private-hash', partners=[8])]).encode()
            users_file.write_bytes(original)
            database = root / 'learning.sqlite3'
            db = sqlite3.connect(database)
            self.addCleanup(db.close)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)')
            db.execute("INSERT INTO messages VALUES (1, 'Keep this message')")
            db.commit()
            materials = root / 'materials'
            materials.mkdir()
            (materials / 'slides.pdf').write_bytes(b'keep this file')
            config = dict(KYNNECTED_USERS_FILE=str(users_file), KYNNECTED_LEARNING_DATABASE=str(database),
                          KYNNECTED_MATERIALS_DIR=str(materials), KYNNECTED_RECORDINGS_DIR=str(root / 'recordings'))
            destination, manifest = prepare(config, root / 'backups')
            self.assertEqual(users_file.read_bytes(), original)
            self.assertEqual((destination / 'users.json').read_bytes(), original)
            self.assertEqual(manifest['sqlite_rows'], {'messages': 1})
            self.assertEqual((destination / 'materials/slides.pdf').read_bytes(), b'keep this file')
            with sqlite3.connect(destination / 'learning.sqlite3') as snapshot:
                self.assertEqual(snapshot.execute('SELECT body FROM messages').fetchone()[0], 'Keep this message')
            self.assertNotIn('private-hash', (destination / 'account-emails.csv').read_text())
            self.assertEqual((destination / 'users.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertIn('users.json', manifest['files_sha256'])


if __name__ == '__main__':
    unittest.main()
