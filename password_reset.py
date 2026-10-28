"""Single-use recovery links, delivered only to the account's saved email."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email.message import EmailMessage
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import ssl
from threading import BoundedSemaphore
import time
from urllib.parse import urlsplit

import argon2
import click
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask.cli import with_appcontext

from account_store import edit_accounts, read_accounts, valid_contact_email, account_email

password_reset = Blueprint('password_reset', __name__)
RESET_SECONDS = 30 * 60
GENERIC_MESSAGE = 'If that username has an email address on file, a reset link will be sent. Check your inbox and spam folder. If it does not arrive, contact the KynnectED team.'
_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='password-email')
_pending = BoundedSemaphore(20)


def init_password_reset(app):
    app.config.update(
        EMAIL_RESETS_ENABLED=os.environ.get('KYNNECTED_EMAIL_RESETS_ENABLED') == '1',
        PUBLIC_URL=os.environ.get('KYNNECTED_PUBLIC_URL', ''),
        SMTP_HOST=os.environ.get('KYNNECTED_SMTP_HOST', ''),
        SMTP_PORT=int(os.environ.get('KYNNECTED_SMTP_PORT', '587')),
        SMTP_USERNAME=os.environ.get('KYNNECTED_SMTP_USERNAME', ''),
        SMTP_PASSWORD=os.environ.get('KYNNECTED_SMTP_PASSWORD', ''),
        SMTP_FROM=os.environ.get('KYNNECTED_SMTP_FROM', ''),
        SMTP_SSL=os.environ.get('KYNNECTED_SMTP_SSL') == '1',
    )
    app.cli.add_command(create_reset_link)


def public_url(config):
    value = config['PUBLIC_URL'].rstrip('/')
    parsed = urlsplit(value)
    local = parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}
    if (not parsed.hostname or (parsed.scheme != 'https' and not local)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path or any(char.isspace() for char in value)):
        raise ValueError('Set KYNNECTED_PUBLIC_URL to your HTTPS site origin (or http://localhost:5001 locally).')
    return value


def email_ready(config):
    if not config.get('EMAIL_RESETS_ENABLED', False):
        return False
    try:
        public_url(config)
    except ValueError:
        return False
    return bool(config['SMTP_HOST'] and valid_contact_email(config['SMTP_FROM'])
                and (not config['SMTP_USERNAME'] or config['SMTP_PASSWORD']))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(user):
    token = secrets.token_urlsafe(32)
    user['password_reset'] = {'digest': digest(token), 'expires_at': time.time() + RESET_SECONDS}
    return token


def token_user(users, token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        return None
    hashed = digest(token)
    return next((user for user in users
                 if user.get('password_reset', {}).get('expires_at', 0) > time.time()
                 and hmac.compare_digest(user.get('password_reset', {}).get('digest', ''), hashed)), None)


def allow_request(config, username, address):
    """Shared across workers; retain only keyed hashes and one hour of counters."""
    path = Path(config['USERS_FILE']).with_suffix('.reset-limits.sqlite3')
    now = time.time()
    keys = [(hmac.new(str(config['SECRET_KEY']).encode(), value.encode(), hashlib.sha256).hexdigest(), limit)
            for value, limit in [('user:' + username, 5), ('ip:' + address, 20)]]
    with closing(sqlite3.connect(path, timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS reset_limits (key TEXT PRIMARY KEY, started REAL, count INTEGER)')
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM reset_limits WHERE started <= ?', (now - 3600,))
            allowed = True
            for key, limit in keys:
                row = db.execute('SELECT count FROM reset_limits WHERE key = ?', (key,)).fetchone()
                if row and row[0] >= limit:
                    allowed = False
            # Still count rejected account requests against the IP allowance.
            for key, limit in keys:
                db.execute('INSERT INTO reset_limits VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET count = MIN(reset_limits.count + 1, ?)',
                           (key, now, limit))
    return allowed


def send_email(config, recipient, link):
    message = EmailMessage()
    message['Subject'] = 'Reset your KynnectED password'
    message['From'] = config['SMTP_FROM']
    message['To'] = recipient
    message.set_content(f'Use this link to choose a new KynnectED password:\n\n{link}\n\n'
                        'This link expires in 30 minutes and can be used once. '
                        'If you did not request it, ignore this email; your password has not changed.\n')
    context = ssl.create_default_context()
    transport = smtplib.SMTP_SSL if config['SMTP_SSL'] else smtplib.SMTP
    options = {'context': context} if config['SMTP_SSL'] else {}
    with transport(config['SMTP_HOST'], config['SMTP_PORT'], timeout=15, **options) as server:
        if not config['SMTP_SSL']:
            server.starttls(context=context)
        if config['SMTP_USERNAME']:
            server.login(config['SMTP_USERNAME'], config['SMTP_PASSWORD'])
        server.send_message(message)


def deliver_reset(config, username, logger):
    token = None
    try:
        with edit_accounts(config['USERS_FILE'], config=config) as users:
            user = next((u for u in users if u['username'].lower() == username), None)
            if user is None:
                return
            recipient = account_email(user)
            if not valid_contact_email(recipient):
                return
            # Do not let repeated requests invalidate a link already in someone's inbox.
            if user.get('password_reset', {}).get('expires_at', 0) > time.time():
                return
            token = issue_token(user)
        send_email(config, recipient, public_url(config) + '/reset-password/' + token)
    except Exception:
        # SMTP exceptions may include credentials or a link: do not log their contents.
        logger.error('Password reset email delivery failed. Check mail configuration and service availability.')
        if token:
            with edit_accounts(config['USERS_FILE'], config=config) as users:
                user = token_user(users, token)
                if user:
                    user.pop('password_reset', None)


def queue_reset(config, username, logger):
    """Keep email/account lookups off the response path and bound outstanding work."""
    if not _pending.acquire(blocking=False):
        return
    try:
        future = _workers.submit(deliver_reset, config, username, logger)
    except Exception:
        _pending.release()
        raise
    future.add_done_callback(lambda completed: _pending.release())


@password_reset.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@password_reset.route('/forgot-password', methods=['GET', 'POST'])
def forgot():
    ready = email_ready(current_app.config)
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()[:128]
        if ready and allow_request(current_app.config, username, request.remote_addr or ''):
            queue_reset(dict(current_app.config), username, current_app.logger)
        return render_template('forgot_password.html', email_ready=ready, message=GENERIC_MESSAGE)
    return render_template('forgot_password.html', email_ready=ready)


@password_reset.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset(token):
    if token_user(read_accounts(current_app.config['USERS_FILE']), token) is None:
        return render_template('reset_password.html', invalid=True), 400
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if not 12 <= len(password) <= 128:
            error = 'Choose a password between 12 and 128 characters.'
        elif password != request.form.get('confirm_password', ''):
            error = 'Your passwords do not match. Please enter them again.'
        if not error:
            hashed = argon2.PasswordHasher().hash(password)
            with edit_accounts(current_app.config['USERS_FILE']) as users:
                user = token_user(users, token)
                if user is None:
                    return render_template('reset_password.html', invalid=True), 400
                user['password'] = hashed
                user['auth_version'] = user.get('auth_version', 0) + 1
                user.pop('password_reset', None)
            session.clear()
            flash('Your password has been reset. Log in with your new password.', 'success')
            return redirect(url_for('login'))
    return render_template('reset_password.html', error=error), 400 if error else 200


@click.command('create-password-reset')
@click.argument('username')
@with_appcontext
def create_reset_link(username):
    """Create a recovery link after independently verifying the account owner."""
    try:
        origin = public_url(current_app.config)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    with edit_accounts(current_app.config['USERS_FILE']) as users:
        user = next((u for u in users if u['username'].lower() == username.lower()), None)
        if user is None:
            raise click.ClickException('No account has that username.')
        token = issue_token(user)
    click.echo('Share privately with the verified account owner. Expires in 30 minutes:')
    click.echo(origin + '/reset-password/' + token)
