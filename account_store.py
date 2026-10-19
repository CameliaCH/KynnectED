"""Atomic, serialized updates to the existing JSON account store."""

from contextlib import contextmanager
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import tempfile

def mentor_is_approved(user):
    """Existing mentors remain approved; new signups have an explicit pending status."""
    return bool(user and user.get('role') == 'mentor' and user.get('mentor_approval_status', 'approved') == 'approved')


def valid_contact_email(value):
    if not value or len(value) > 254:
        return False
    local, separator, domain = value.partition('@')
    return bool(separator and len(local) <= 64 and not local.startswith('.') and not local.endswith('.')
                and '..' not in local and re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+", domain))


def normalize_contact_number(value):
    """Accept common phone formatting without requiring an email account."""
    if len(value) > 40 or not re.fullmatch(r'\+?[0-9 ()-]+', value):
        return None
    digits = re.sub(r'[^0-9]', '', value)
    if not 7 <= len(digits) <= 15:
        return None
    return ('+' if value.startswith('+') else '') + digits


def account_email(user):
    """Prefer the recovery email; support older email contact records."""
    if not user:
        return ''
    email = user.get('email', '')
    if valid_contact_email(email):
        return email
    email = user.get('contact_value', '') if user.get('contact_method') == 'email' else ''
    return email if valid_contact_email(email) else ''


def account_whatsapp(user):
    return user.get('whatsapp', user.get('contact_value', '') if user.get('contact_method') == 'whatsapp' else '')


def read_accounts(path, config=None):
    from database import uses_postgres, read_postgres_accounts
    if uses_postgres(config):
        return read_postgres_accounts(config)
    with Path(path).open(encoding="utf-8") as source:
        return json.load(source)


def next_account_id(users):
    from database import uses_postgres, connect
    if uses_postgres():
        with connect() as connection:
            return connection.execute("SELECT nextval(pg_get_serial_sequence('profiles', 'id')) AS id").fetchone()['id']
    return max((user['id'] for user in users), default=0) + 1


@contextmanager
def edit_accounts(path, config=None):
    from database import uses_postgres, edit_postgres_accounts
    if uses_postgres(config):
        with edit_postgres_accounts(config) as accounts:
            yield accounts
        return
    path = Path(path)
    # Lock a separate file: replacing the JSON must not replace the lock inode.
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        accounts = read_accounts(path)
        yield accounts
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".accounts-", delete=False) as target:
                temporary = Path(target.name)
                json.dump(accounts, target, indent=4, ensure_ascii=False)
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def session_secret(instance_path):
    """Keep local sessions valid across restarts without checking a key into git."""
    if os.environ.get("KYNNECTED_SECRET_KEY"):
        return os.environ["KYNNECTED_SECRET_KEY"]
    directory = Path(instance_path)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "session-secret").open("a+") as source:
        os.chmod(source.name, 0o600)
        fcntl.flock(source, fcntl.LOCK_EX)
        source.seek(0)
        secret = source.read().strip()
        if not secret:
            secret = secrets.token_hex(32)
            source.write(secret)
            source.flush()
        return secret
