"""Atomic, serialized updates to the existing JSON account store."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile


def read_accounts(path):
    with Path(path).open(encoding="utf-8") as source:
        return json.load(source)


@contextmanager
def edit_accounts(path):
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
