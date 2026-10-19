"""Back up local data and prepare an email mapping without changing any accounts.

This script does not connect to Supabase, create users, or send messages.
Run again during the final maintenance window for the definitive import snapshot.
"""

import csv
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from account_store import account_email


def email_mapping(users):
    counts = Counter(account_email(user).casefold() for user in users if account_email(user))
    rows = []
    for user in users:
        email = account_email(user)
        status = 'missing_email' if not email else 'duplicate_email' if counts[email.casefold()] > 1 else 'ready'
        rows.append({'account_id': user['id'], 'username': user['username'],
                     'current_email': email, 'migration_email': email, 'status': status})
    return rows


def snapshot_sqlite(source, destination):
    """Use SQLite's backup API so committed WAL records are included."""
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as source_db:
        with sqlite3.connect(destination) as target_db:
            source_db.backup(target_db)
            integrity = target_db.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError('SQLite snapshot failed its integrity check.')
            tables = [row[0] for row in target_db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            counts = {}
            for table in tables:
                identifier = '"' + table.replace('"', '""') + '"'
                counts[table] = target_db.execute('SELECT count(*) FROM ' + identifier).fetchone()[0]
    destination.chmod(0o600)
    return counts


def prepare(config, backup_root):
    users_file = Path(config.get('KYNNECTED_USERS_FILE') or ROOT / 'users.json')
    database = Path(config.get('KYNNECTED_LEARNING_DATABASE') or ROOT / 'instance/learning.sqlite3')
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = Path(tempfile.mkdtemp(prefix=timestamp + '-', dir=backup_root))
    try:
        with users_file.with_suffix(users_file.suffix + '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            contents = users_file.read_bytes()
            users = json.loads(contents)
        if not isinstance(users, list) or len({u['id'] for u in users}) != len(users):
            raise ValueError('The account store must contain unique account IDs.')
        target = destination / 'users.json'
        target.write_bytes(contents)
        target.chmod(0o600)
        rows = email_mapping(users)
        worksheet = destination / 'account-emails.csv'
        with worksheet.open('w', encoding='utf-8', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=['account_id', 'username', 'current_email', 'migration_email', 'status'])
            writer.writeheader()
            writer.writerows(rows)
        worksheet.chmod(0o600)
        counts = snapshot_sqlite(database, destination / 'learning.sqlite3') if database.exists() else {}
        file_counts = {}
        for label, setting in [('materials', 'KYNNECTED_MATERIALS_DIR'), ('recordings', 'KYNNECTED_RECORDINGS_DIR')]:
            source = Path(config.get(setting) or ROOT / 'instance' / label)
            files = [path for path in source.rglob('*') if path.is_file()] if source.exists() else []
            file_counts[label] = {'files': len(files), 'bytes': sum(path.stat().st_size for path in files)}
            if source.exists():
                shutil.copytree(source, destination / label)
        manifest = {
            'created_at': datetime.now(timezone.utc).isoformat(),
            'accounts': len(users), 'email_status_counts': dict(Counter(row['status'] for row in rows)),
            'sqlite_rows': counts, 'local_files': file_counts,
            'scope': 'Local accounts, SQLite, materials and legacy recordings. Remote R2 objects are not copied.',
            'files_sha256': {},
        }
        for path in destination.rglob('*'):
            if path.is_file():
                path.chmod(0o600)
                hasher = hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        hasher.update(chunk)
                manifest['files_sha256'][str(path.relative_to(destination))] = hasher.hexdigest()
        manifest_path = destination / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        manifest_path.chmod(0o600)
    except Exception:
        # Retain partial snapshots for inspection; never mistake them for a completed backup.
        (destination / 'INCOMPLETE').write_text('Preparation failed. Do not use this snapshot for import.\n')
        raise
    return destination, manifest


def main():
    from dotenv import dotenv_values
    config = {**dotenv_values(ROOT / '.env', interpolate=False), **os.environ}
    destination, manifest = prepare(config, ROOT / 'instance/supabase-migration')
    print('Local backup:', destination)
    print('Email worksheet:', destination / 'account-emails.csv')
    print('Accounts:', manifest['accounts'])
    print('Email readiness:', json.dumps(manifest['email_status_counts']))
    print('SQLite row counts:', json.dumps(manifest['sqlite_rows']))
    print('Local file counts:', json.dumps(manifest['local_files']))
    print('No source accounts, remote users, databases, or storage objects were changed.')


if __name__ == '__main__':
    main()
