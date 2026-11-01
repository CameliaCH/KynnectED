# Web Push notifications

## Server setup

Install the dependencies and create the server's private VAPID identity once:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m flask --app app setup-push
```

The command preserves an existing key and writes new keys with mode `0600` to
`instance/push-private.pem`. This file is ignored by Git. Keep it private and back
it up alongside the account/database backups. All application workers and the
delivery worker must use the same key and SQLite database. Changing the key means
devices need to enable notifications again. The public key is derived automatically;
no Apple Developer account, Firebase project, or paid push service is required.

Serve the site over HTTPS. Localhost is useful for browser testing but cannot serve
as a production website. The server must remain running and connected to send new
notifications. Deploying this code does not keep a sleeping development Mac online.

Configuration:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `KYNNECTED_PUSH_ENABLED` | `1` | Set to `0` to stop enrollment and delivery. |
| `KYNNECTED_PUSH_PRIVATE_KEY_FILE` | `instance/push-private.pem` | Private PEM key path, shared by all workers. |
| `KYNNECTED_PUSH_SUBJECT` | `mailto:kynnected.org@gmail.com` | Valid public contact URI used for VAPID. |
| `KYNNECTED_PUSH_BACKGROUND_WORKER` | `1` | Start a background delivery thread on the first request. |

The default is appropriate for an always-running Flask/WSGI process with background
threads enabled. It checks the persistent queue every 15 seconds. SQLite leases
prevent simultaneous workers from sending the same job. A process crash after the
provider accepts a push but before the database acknowledgment may cause a retry;
stable pairing notification tags replace duplicate displayed alerts.

For hosting that suspends web processes between requests, use an always-running,
supervised worker process with the same environment and database. Set
`KYNNECTED_PUSH_BACKGROUND_WORKER=0` for the web process and run:

```sh
.venv/bin/python -m flask --app app deliver-push --watch
```

Without `--watch`, the command drains currently due jobs once. The worker must have
outbound HTTPS access to Google, Mozilla and Apple push endpoints. These providers
are explicitly allowed by subscription validation; arbitrary URLs and redirects
are rejected. Other push providers need an explicit implementation update.

## Recipient experience

After signing in and completing required profile fields, recipients see a prompt
to enable notifications. The browser permission/subscription request happens only
when they tap **Enable notifications**. Existing tab-only notification permission
does not enroll a device automatically; those users also see the upgrade prompt.

On iPhone/iPad, use **Share → Add to Home Screen**, launch the installed site, sign
in, and enable notifications there. The manifest launches the site in standalone
mode. The browser determines installation and notification support; the interface
explains unsupported browsers and blocked permissions.

Notifications are per device and account. Enabling on another account in the same
browser replaces the prior binding and discards its unsent jobs. Signing out or
turning alerts off removes this browser's server subscription. Reopening an expired
login session does not prevent already-subscribed devices from receiving alerts;
password/authentication-version changes prevent further sends for stale subscriptions.

Each new message queues jobs in the same SQLite transaction as the saved message.
Retries and push-service storage expire 24 hours after that message was sent; the
chat message itself remains saved. A powered-off device cannot display alerts.
It may receive pending, unexpired alerts after reconnecting. Offline alerts for the
same pairing may be collapsed into a single generic notification. Notifications
already accepted by the provider cannot be recalled on logout or unlinking.

## Verification

For browser-specific display problems, reload the site and choose **Notifications →
Test notification** after enabling alerts. This directly asks that browser to show
an alert; it does not send a chat message or test the remote push service. If Safari
works but Chrome's local test does not appear, check Chrome's site permission and
**Mac System Settings → Notifications → Google Chrome**, including the alert style,
and Focus settings. Use a regular browser window; Chrome does not deliver
notifications in Incognito. If the local test works but messages still do not
produce alerts, investigate the push subscription and delivery path next.

```sh
.venv/bin/python -m unittest discover -s tests -v
node tests/message_notifications.test.js
```

On macOS without Node, the JavaScript checks also run with
`/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc tests/message_notifications.test.js`.
Automated tests verify actual payload encryption/signing against a mocked HTTP
transport, queue retries/expiry, account isolation, subscription lifecycle, and
permission/Home Screen prompt states. They never send a push to a real person.

Before a production rollout, test two linked test accounts on the HTTPS deployment:
enable notifications for the recipient, close their site tabs, send a message from
the mentor, and check that the alert arrives and opens the pairing. Repeat with a
Home Screen iPhone app, a temporarily disconnected device, and after disabling
notifications. Browser/OS delivery and permission dialogs require this real-device
check; unit tests cannot establish them.

References: [Apple Web Push requirements](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers),
[Push subscription user gestures](https://developer.mozilla.org/en-US/docs/Web/API/PushManager/subscribe),
[pywebpush](https://github.com/web-push-libs/pywebpush).
