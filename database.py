"""Postgres connections for the Flask backend, with SQLite-compatible row access.

The initial migration keeps the existing server-side permission checks. Data API
roles have no grants on these tables until user-specific Auth policies are added.
"""

from contextlib import contextmanager
import copy
import re

from flask import current_app, has_app_context

WRITE_LOCK = 672932019
ACCOUNT_LOCK = 672932020


def settings(config=None):
    return config if config is not None else current_app.config if has_app_context() else {}


def uses_postgres(config=None):
    config = settings(config)
    # Existing tests explicitly use temporary JSON/SQLite fixtures. A separate,
    # opt-in integration suite tests Postgres in isolated schemas.
    return config.get('DATA_BACKEND') == 'supabase' and (
        not config.get('TESTING') or config.get('SUPABASE_TESTING', False))


class DatabaseRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    def build(values):
        result = DatabaseRow()
        for name, value in zip(names, values):
            result.setdefault(name, value)
        return result
    return build


def connect(config=None):
    import psycopg
    from psycopg import sql
    config = settings(config)
    uri = config.get('SUPABASE_DATABASE_URL')
    if not uri:
        raise RuntimeError('Supabase database connection is not configured.')
    connection = psycopg.connect(uri, connect_timeout=10, sslmode='require',
                                 autocommit=True, row_factory=row_factory)
    try:
        connection.execute(sql.SQL('SET search_path TO {}, {}, pg_catalog').format(
            sql.Identifier(config.get('SUPABASE_APP_SCHEMA', 'public')),
            sql.Identifier(config.get('SUPABASE_PRIVATE_SCHEMA', 'kynnected_private'))))
        connection.execute("SET statement_timeout = '30s'")
        connection.execute("SET lock_timeout = '15s'")
    except Exception:
        connection.close()
        raise
    return connection


def parameter_sql(statement):
    # Question marks in SQL literals are not placeholders. Values always remain
    # bound parameters, including JSON, chat text, passwords, and user IDs.
    pieces = re.split(r"('(?:[^']|'')*')", statement)
    return ''.join(piece.replace('%', '%%').replace('?', '%s') if index % 2 == 0
                   else piece.replace('%', '%%') for index, piece in enumerate(pieces))


class PostgresDatabase:
    def __init__(self, config=None):
        self.connection = connect(config)
        self.transactions = []

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        try:
            # Match SQLite's serialized write transactions. This covers schedule
            # conflicts, upload completion, reviews and other check-then-write flows.
            self.connection.execute('SELECT pg_advisory_xact_lock(%s)', (WRITE_LOCK,))
        except BaseException:
            import sys
            self.transactions.pop().__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *error):
        return self.transactions.pop().__exit__(*error)

    def execute(self, statement, params=None):
        if statement.strip().upper() == 'BEGIN IMMEDIATE':
            if not self.transactions:
                raise RuntimeError('Write transactions require a database context manager.')
            return self.connection.execute('SELECT 1')
        return self.connection.execute(parameter_sql(statement), params or ())

    def close(self):
        self.connection.close()


def read_profiles(connection):
    rows = connection.execute('''SELECT p.data, c.password_hash, c.reset_state
        FROM profiles p LEFT JOIN account_credentials c ON c.account_id = p.id ORDER BY p.id''')
    users = []
    for row in rows:
        user = copy.deepcopy(row['data'])
        if row['password_hash'] is not None:
            user['password'] = row['password_hash']
        if row['reset_state'] is not None:
            user['password_reset'] = row['reset_state']
        users.append(user)
    return users


def save_profiles(connection, users, original_ids=()):
    from psycopg.types.json import Jsonb
    ids = [user['id'] for user in users]
    if len(ids) != len(set(ids)):
        raise ValueError('Account IDs must be unique.')
    for user in users:
        profile = {key: value for key, value in user.items() if key not in {'password', 'password_reset'}}
        connection.execute('''INSERT INTO profiles(id, username, email, role, school, is_coordinator, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET username=EXCLUDED.username, email=EXCLUDED.email,
                role=EXCLUDED.role, school=EXCLUDED.school, is_coordinator=EXCLUDED.is_coordinator, data=EXCLUDED.data''',
            (user['id'], user['username'], user.get('email'), user.get('role'), user.get('school'),
             user.get('is_coordinator') is True, Jsonb(profile)))
        connection.execute('''INSERT INTO account_credentials(account_id, password_hash, reset_state)
            VALUES (%s, %s, %s) ON CONFLICT(account_id) DO UPDATE SET
                password_hash=EXCLUDED.password_hash, reset_state=EXCLUDED.reset_state''',
            (user['id'], user.get('password'), Jsonb(user['password_reset']) if user.get('password_reset') else None))
    removed = set(original_ids) - set(ids)
    if removed:
        connection.execute('DELETE FROM profiles WHERE id = ANY(%s)', (list(removed),))


def read_postgres_accounts(config=None):
    with connect(config) as connection:
        return read_profiles(connection)


@contextmanager
def edit_postgres_accounts(config=None):
    with connect(config) as connection:
        with connection.transaction():
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (ACCOUNT_LOCK,))
            users = read_profiles(connection)
            ids = [user['id'] for user in users]
            yield users
            save_profiles(connection, users, ids)
