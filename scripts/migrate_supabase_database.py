"""Import a verified local snapshot into an empty Supabase application database.

Default: validate and report only. --apply performs a single atomic transaction.
Re-running a completed import never overwrites subsequent application activity.
"""

import argparse
from contextlib import closing
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from account_store import account_email, valid_contact_email
from database import connect, save_profiles, ACCOUNT_LOCK, WRITE_LOCK

MIGRATION = ROOT / 'supabase/migrations/202610180001_database.sql'
TABLES = ('lessons', 'missing_recording_reports', 'resource_materials', 'messages', 'recordings', 'recording_uploads')
ROWID_TABLES = ('missing_recording_reports', 'resource_materials', 'recordings')
USER_COLUMNS = {'mentor_id', 'mentee_id', 'sender_id', 'created_by', 'uploaded_by',
                'reviewed_by', 'review_submitted_by', 'flagged_by'}
RUN_NAME = '202610180001-local-import'


def migration_sql(app_schema='public', private_schema='kynnected_private'):
    # Custom schemas are exclusively for isolated integration tests.
    import re
    if not all(re.fullmatch(r'[a-z][a-z0-9_]*', value) for value in (app_schema, private_schema)):
        raise ValueError('Invalid schema name.')
    return MIGRATION.read_text().replace('kynnected_private', private_schema).replace('public', app_schema)


def validate_connection(config):
    project = urlsplit(config.get('SUPABASE_URL') or '')
    parsed = urlsplit(config.get('SUPABASE_DATABASE_URL') or '')
    if project.scheme != 'https' or not (project.hostname or '').endswith('.supabase.co'):
        raise ValueError('Set a valid SUPABASE_URL.')
    ref = project.hostname.split('.')[0]
    valid_host = parsed.hostname == 'db.' + project.hostname or (parsed.hostname or '').endswith('.pooler.supabase.com')
    valid_user = parsed.username == ('postgres' if parsed.hostname == 'db.' + project.hostname else 'postgres.' + ref)
    password = unquote(parsed.password or '')
    if (parsed.scheme not in {'postgres', 'postgresql'} or not valid_host or not valid_user
            or not password or 'YOUR-PASSWORD' in password.upper() or 'YOUR_PASSWORD' in password.upper()):
        raise ValueError('Database URL must match this project and contain the actual database password.')


def load_snapshot(path):
    path = path.resolve()
    if (path / 'INCOMPLETE').exists():
        raise ValueError('This snapshot is incomplete.')
    manifest = json.loads((path / 'manifest.json').read_text())
    for name in ('users.json', 'learning.sqlite3'):
        source = path / name
        if source.exists():
            if hashlib.sha256(source.read_bytes()).hexdigest() != manifest['files_sha256'].get(name):
                raise ValueError('Snapshot verification failed for ' + name)
    users = json.loads((path / 'users.json').read_text())
    with (path / 'account-emails.csv').open(newline='') as source:
        mappings = list(csv.DictReader(source))
    if len(mappings) != len(users) or len({row['account_id'] for row in mappings}) != len(users):
        raise ValueError('Email worksheet does not match the accounts.')
    mappings = {row['account_id']: row for row in mappings}
    for user in users:
        row = mappings.get(str(user['id']))
        if not row or row['username'] != user['username']:
            raise ValueError('Email worksheet account identities changed.')
        email = row['migration_email'].strip()
        if not valid_contact_email(email):
            raise ValueError('Complete the migration_email column for every account.')
        if email != account_email(user):
            user['email'] = email
            if user.get('contact_method') == 'email':
                user['contact_value'] = email
    if len({account_email(u).casefold() for u in users}) != len(users):
        raise ValueError('Each retained account needs a unique migration email.')
    records = {table: [] for table in TABLES}
    database = path / 'learning.sqlite3'
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            existing = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                if table in existing:
                    select = 'rowid AS rowid, *' if table in ROWID_TABLES else '*'
                    records[table] = [dict(row) for row in db.execute(f'SELECT {select} FROM {table}')]
    fingerprint = hashlib.sha256(json.dumps({'users': users, 'records': records}, sort_keys=True).encode()).hexdigest()
    return users, records, fingerprint


def import_snapshot(config, users, records, fingerprint, apply=False):
    from psycopg import sql
    from psycopg.types.json import Jsonb
    app_schema = config.get('SUPABASE_APP_SCHEMA', 'public')
    private_schema = config.get('SUPABASE_PRIVATE_SCHEMA', 'kynnected_private')
    counts = {'profiles': len(users), **{table: len(rows) for table, rows in records.items()}}
    with connect(config) as connection:
        with connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (WRITE_LOCK,))
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (ACCOUNT_LOCK,))
            marker = connection.execute('SELECT to_regclass(%s) AS table_name',
                                        (private_schema + '.migration_runs',)).fetchone()['table_name']
            if marker:
                existing = connection.execute('SELECT source_sha256 FROM migration_runs WHERE name = %s', (RUN_NAME,)).fetchone()
                if existing:
                    if existing['source_sha256'] != fingerprint:
                        raise ValueError('An import already exists from a different snapshot. Refusing to overwrite it.')
                    return 'already_imported', counts
            collisions = connection.execute('''SELECT table_name FROM information_schema.tables
                WHERE table_schema = %s AND table_name = ANY(%s)''', (app_schema, ['profiles', *TABLES])).fetchall()
            if collisions:
                raise ValueError('Application tables already exist. Refusing to overwrite them.')
            if not apply:
                return 'ready', counts
            connection.execute(migration_sql(app_schema, private_schema))
            save_profiles(connection, users)
            for table in TABLES:
                columns = {row['column_name'] for row in connection.execute('''SELECT column_name FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s''', (app_schema, table))}
                for row in records[table]:
                    if not row.keys() <= columns:
                        raise ValueError('Source contains unsupported columns in ' + table)
                    connection.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                        sql.Identifier(table), sql.SQL(', ').join(map(sql.Identifier, row.keys())),
                        sql.SQL(', ').join(sql.Placeholder() for _ in row)), tuple(row.values()))
            # Keep historic/deleted account IDs out of the allocation range.
            user_ids = [u['id'] for u in users]
            user_ids += [value for rows in records.values() for row in rows for key, value in row.items()
                         if key in USER_COLUMNS and isinstance(value, int)]
            account_max = max(user_ids, default=0)
            sequences = [('profiles', 'id', account_max),
                         ('messages', 'id', max((r['id'] for r in records['messages']), default=0))]
            sequences += [(table, 'rowid', max((r['rowid'] for r in records[table]), default=0)) for table in ROWID_TABLES]
            for table, column, maximum in sequences:
                connection.execute('SELECT setval(pg_get_serial_sequence(%s, %s), %s, %s)',
                                   (table, column, max(maximum, 1), maximum > 0))
                sequence = connection.execute('SELECT pg_get_serial_sequence(%s, %s) AS name', (table, column)).fetchone()['name']
                schema_name, sequence_name = sequence.split('.', 1)
                connection.execute(sql.SQL('REVOKE ALL ON SEQUENCE {}.{} FROM PUBLIC, anon, authenticated, service_role').format(
                    sql.Identifier(schema_name), sql.Identifier(sequence_name)))
            for table, expected in counts.items():
                actual = connection.execute(sql.SQL('SELECT count(*) AS count FROM {}').format(sql.Identifier(table))).fetchone()['count']
                if actual != expected:
                    raise ValueError('Import count did not match for ' + table)
            connection.execute('INSERT INTO migration_runs(name, source_sha256, counts) VALUES (%s, %s, %s)',
                               (RUN_NAME, fingerprint, Jsonb(counts)))
    return 'imported', counts


def main():
    from dotenv import dotenv_values
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--apply', action='store_true', help='Create tables and atomically import the snapshot.')
    args = parser.parse_args()
    config = {**dotenv_values(ROOT / '.env', interpolate=False), **os.environ}
    try:
        validate_connection(config)
        users, records, fingerprint = load_snapshot(args.snapshot)
        status, counts = import_snapshot(config, users, records, fingerprint, args.apply)
        print('Migration status:', status)
        print('Record counts:', json.dumps(counts))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # Driver errors can include connection details or row data. Never print them.
        print('Migration failed (' + type(error).__name__ + '). Any transaction was rolled back.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
