"""Device subscriptions and durable, retryable Web Push delivery for pairing chat."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

import click
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Blueprint, abort, current_app, g, jsonify, request, session
from flask.cli import with_appcontext
from pywebpush import webpush, WebPushException
import requests

from account_store import read_accounts

push = Blueprint('push', __name__)
DELIVERY_SECONDS = 24 * 60 * 60
PUSH_SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    subscription TEXT NOT NULL,
    vapid_key TEXT NOT NULL,
    auth_version INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS push_subscriptions_user ON push_subscriptions(user_id);
CREATE TABLE IF NOT EXISTS push_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    subscription_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    mentor_id INTEGER NOT NULL,
    mentee_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt INTEGER NOT NULL,
    UNIQUE(message_id, subscription_id)
);
CREATE INDEX IF NOT EXISTS push_outbox_due ON push_outbox(next_attempt);
"""


def public_key():
    try:
        key = serialization.load_pem_private_key(Path(current_app.config['PUSH_PRIVATE_KEY_FILE']).read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            return None
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    except (OSError, ValueError, TypeError):
        return None


def ready():
    return bool(current_app.config['PUSH_ENABLED'] and public_key())


def init_push(app):
    app.config.update(
        PUSH_ENABLED=os.environ.get('KYNNECTED_PUSH_ENABLED', '1') == '1',
        PUSH_PRIVATE_KEY_FILE=os.environ.get('KYNNECTED_PUSH_PRIVATE_KEY_FILE', str(Path(app.instance_path) / 'push-private.pem')),
        PUSH_SUBJECT=os.environ.get('KYNNECTED_PUSH_SUBJECT', 'mailto:kynnected.org@gmail.com'),
        PUSH_BACKGROUND_WORKER=os.environ.get('KYNNECTED_PUSH_BACKGROUND_WORKER', '1') == '1',
    )
    app.register_blueprint(push)
    app.cli.add_command(setup_push)
    app.cli.add_command(deliver_push)
    worker_lock = threading.Lock()

    @app.teardown_appcontext
    def close_worker_database(error=None):
        db = g.pop('learning_db', None)
        if db is not None:
            db.close()

    @app.before_request
    def start_worker():
        if app.testing or not app.config['PUSH_BACKGROUND_WORKER'] or not ready():
            return
        with worker_lock:
            previous = app.extensions.get('push_worker')
            if previous and previous[0] == os.getpid() and previous[1].is_alive():
                return
            thread = threading.Thread(target=worker_loop, args=(app,), name='web-push', daemon=True)
            app.extensions['push_worker'] = (os.getpid(), thread)
            thread.start()


@click.command('setup-push')
@with_appcontext
def setup_push():
    """Create a private VAPID key once; never overwrite an existing identity."""
    path = Path(current_app.config['PUSH_PRIVATE_KEY_FILE'])
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as target:
            target.write(data)
    if not public_key():
        raise click.ClickException('The configured push key is invalid. Preserve the existing file and check its configuration.')
    click.echo('Web Push key is ready. Preserve this private file in server backups: ' + str(path))


@click.command('deliver-push')
@click.option('--watch', is_flag=True, help='Keep draining the queue every 15 seconds.')
@with_appcontext
def deliver_push(watch):
    """Drain queued notifications; optionally run as a supervised worker."""
    if not ready():
        raise click.ClickException('Run setup-push and enable push delivery first.')
    while True:
        while deliver_one():
            pass
        if not watch:
            break
        time.sleep(15)


@push.before_request
def require_account():
    if g.current_user is None:
        return jsonify(error='Please log in to manage notifications.'), 401
    if g.current_user.get('role') not in {'mentor', 'mentee'}:
        abort(403)


@push.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get('user_id') != g.current_user['id']:
        abort(409, description='Your account has changed. Reload this page.')
    return data


def validated_subscription(value):
    if not isinstance(value, dict):
        raise ValueError()
    endpoint = value.get('endpoint', '')
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or not endpoint.isascii() or any(c.isspace() for c in endpoint):
        raise ValueError()
    url = urlsplit(endpoint)
    host = url.hostname or ''
    allowed = host in {'fcm.googleapis.com', 'updates.push.services.mozilla.com'} or host.endswith('.push.apple.com')
    if not allowed or url.scheme != 'https' or url.port not in (None, 443) or url.username or url.password or url.fragment:
        raise ValueError()
    keys = value.get('keys')
    if not isinstance(keys, dict):
        raise ValueError()
    decoded = {}
    for name, size in [('p256dh', 65), ('auth', 16)]:
        item = keys.get(name, '')
        if not isinstance(item, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}={0,2}', item):
            raise ValueError()
        decoded[name] = base64.urlsafe_b64decode(item + '=' * (-len(item) % 4))
        if len(decoded[name]) != size:
            raise ValueError()
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
    return {'endpoint': endpoint, 'keys': {name: keys[name] for name in ('p256dh', 'auth')}}


@push.get('/api/push/config')
def config():
    return jsonify(user_id=g.current_user['id'], available=ready(), public_key=public_key() if ready() else None)


@push.post('/api/push/status')
def subscription_status():
    from learning import get_db
    data = payload()
    row = get_db().execute('SELECT * FROM push_subscriptions WHERE endpoint = ? AND user_id = ?',
                           (str(data.get('endpoint', ''))[:2049], g.current_user['id'])).fetchone()
    enabled = bool(row and row['vapid_key'] == public_key() and row['auth_version'] == g.current_user.get('auth_version', 0))
    if enabled:
        session['push_subscription_id'] = row['id']
    return jsonify(enabled=enabled)


@push.post('/api/push/subscribe')
def subscribe():
    from learning import get_db
    data = payload()
    if not ready():
        return jsonify(error='Notifications are not available yet. Please try again later.'), 503
    try:
        subscription = validated_subscription(data.get('subscription'))
    except (ValueError, TypeError):
        return jsonify(error='This browser provided an unsupported push subscription.'), 400
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM push_subscriptions WHERE endpoint = ?', (subscription['endpoint'],)).fetchone()
        same_owner = existing and existing['user_id'] == g.current_user['id']
        if (same_owner and existing['subscription'] == json.dumps(subscription)
                and existing['vapid_key'] == public_key()
                and existing['auth_version'] == g.current_user.get('auth_version', 0)):
            session['push_subscription_id'] = existing['id']
            return jsonify(enabled=True)
        count = db.execute('SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?', (g.current_user['id'],)).fetchone()[0]
        if count - int(bool(same_owner)) >= 20:
            return jsonify(error='You have reached the 20-device notification limit. Turn off notifications on an unused device.'), 400
        if existing:
            remove_subscription(db, existing['id'])
        cursor = db.execute('INSERT INTO push_subscriptions(endpoint, user_id, subscription, vapid_key, auth_version, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                            (subscription['endpoint'], g.current_user['id'], json.dumps(subscription), public_key(),
                             g.current_user.get('auth_version', 0), int(time.time())))
        session['push_subscription_id'] = cursor.lastrowid
    return jsonify(enabled=True)


def remove_subscription(db, subscription_id):
    db.execute('DELETE FROM push_outbox WHERE subscription_id = ?', (subscription_id,))
    db.execute('DELETE FROM push_subscriptions WHERE id = ?', (subscription_id,))


@push.post('/api/push/unsubscribe')
def unsubscribe():
    from learning import get_db
    data = payload()
    with get_db() as db:
        row = db.execute('SELECT id FROM push_subscriptions WHERE endpoint = ? AND user_id = ?',
                         (str(data.get('endpoint', ''))[:2049], g.current_user['id'])).fetchone()
        if row:
            remove_subscription(db, row['id'])
    session.pop('push_subscription_id', None)
    return jsonify(enabled=False)


def revoke_session_device():
    from learning import get_db
    subscription_id = session.get('push_subscription_id')
    if subscription_id is not None and session.get('user_id') is not None:
        with get_db() as db:
            row = db.execute('SELECT id FROM push_subscriptions WHERE id = ? AND user_id = ?',
                             (subscription_id, session['user_id'])).fetchone()
            if row:
                remove_subscription(db, row['id'])


def enqueue_message(db, message_id, mentor_id, mentee_id, sender_id):
    recipient_id = mentee_id if sender_id == mentor_id else mentor_id
    now = int(time.time())
    db.execute('''INSERT OR IGNORE INTO push_outbox
        (message_id, subscription_id, recipient_id, mentor_id, mentee_id, expires_at, next_attempt)
        SELECT ?, id, user_id, ?, ?, ?, ? FROM push_subscriptions WHERE user_id = ?''',
        (message_id, mentor_id, mentee_id, now + DELIVERY_SECONDS, now, recipient_id))


class NoRedirectSession(requests.Session):
    def post(self, url, **kwargs):
        return super().post(url, allow_redirects=False, **kwargs)


def deliver_one():
    from learning import get_db, active_pair
    if not ready():
        return False
    now = int(time.time())
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM push_outbox WHERE expires_at <= ?', (now,))
        job = db.execute('SELECT * FROM push_outbox WHERE next_attempt <= ? ORDER BY id LIMIT 1', (now,)).fetchone()
        if job is None:
            return False
        # A lease allows recovery after a worker exits without duplicate concurrent sends.
        db.execute('UPDATE push_outbox SET attempts = attempts + 1, next_attempt = ? WHERE id = ?', (now + 60, job['id']))
    subscription = db.execute('SELECT * FROM push_subscriptions WHERE id = ?', (job['subscription_id'],)).fetchone()
    users = {u['id']: u for u in read_accounts(current_app.config['USERS_FILE'])}
    recipient = users.get(job['recipient_id'])
    if (not subscription or not recipient or subscription['user_id'] != job['recipient_id']
            or subscription['auth_version'] != recipient.get('auth_version', 0)
            or subscription['vapid_key'] != public_key()
            or not active_pair(users.get(job['mentor_id']), users.get(job['mentee_id']))):
        with db:
            db.execute('DELETE FROM push_outbox WHERE id = ?', (job['id'],))
        return True
    tag = hashlib.sha256(f"{job['recipient_id']}:{job['mentor_id']}:{job['mentee_id']}".encode()).hexdigest()[:32]
    notice = {'title': 'New KynnectED message', 'body': 'You have a new message in your pairing chat.',
              'tag': tag, 'url': f"/pairings/{job['mentor_id']}/{job['mentee_id']}#chat"}
    try:
        with NoRedirectSession() as transport:
            webpush(subscription_info=json.loads(subscription['subscription']), data=json.dumps(notice),
                    vapid_private_key=current_app.config['PUSH_PRIVATE_KEY_FILE'],
                    vapid_claims={'sub': current_app.config['PUSH_SUBJECT']}, timeout=10,
                    ttl=max(1, job['expires_at'] - now), headers={'Urgency': 'high', 'Topic': tag},
                    requests_session=transport)
    except Exception as error:
        status = error.response.status_code if isinstance(error, WebPushException) and error.response is not None else None
        with db:
            if status in (404, 410):
                remove_subscription(db, subscription['id'])
            else:
                delay = min(3600, 30 * 2 ** min(job['attempts'], 7))
                db.execute('UPDATE push_outbox SET next_attempt = ? WHERE id = ?', (now + delay, job['id']))
        current_app.logger.warning('Web Push delivery deferred or subscription expired (HTTP %s).', status or 'unavailable')
    else:
        with db:
            db.execute('DELETE FROM push_outbox WHERE id = ?', (job['id'],))
    return True


def worker_loop(app):
    while True:
        try:
            with app.app_context():
                while deliver_one():
                    pass
        except Exception:
            app.logger.warning('Web Push worker will retry after an internal error.')
        time.sleep(15)
