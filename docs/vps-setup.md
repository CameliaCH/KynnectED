# VPS setup after pulling updates

A Git pull updates application code. It does not copy `.env`, install Python
dependencies, generate a notification key, or restart running application workers.
Use the production service's checkout, Python environment, OS user and environment
settings for these steps. Preserve the server's existing accounts and database.

## Resource uploads: 413 Request Entity Too Large

A **413** response means a request-size limit was exceeded; it does not mean the
disk is full. The app allows PDF/PPTX materials up to 25 MiB (shown as 25 MB).
If a smaller file, such as 1.9 MB, receives a plain Nginx 413 page, check the
reverse proxy first: Nginx's default `client_max_body_size` is `1m`.
See the [Nginx directive documentation](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size).

On the VPS, inspect the active Nginx configuration:

```sh
sudo nginx -T
```

Find the configuration file and `server` block serving the site's HTTPS domain
(typically under `/etc/nginx/sites-available/` on Ubuntu/Debian). Edit that
existing block and add or update this directive, allowing room for the upload
form around the 25 MiB file:

```nginx
client_max_body_size 26m;
```

If the `location` handling `/learning-resources/submit` defines its own
`client_max_body_size`, update that value too: it overrides the server setting.
Preserve the existing TLS and proxy settings. Validate the configuration, and
reload only if validation succeeds:

```sh
sudo nginx -t && sudo systemctl reload nginx
```

Retry the 1.9 MB upload. A successful submission appears under **Your submissions**
as **Pending** until coordinator approval. If it still returns 413, check the
Nginx error log configured by `error_log` and any other proxy in front of the app
to identify which layer rejected it. Moving files to R2 alone will not fix a
proxy limit while uploads still pass through that proxy. This Nginx setting is
applied on the VPS; pulling application code does not change it.

## Recording storage

The message **Recording storage is not configured yet** means at least one of
these settings is empty in the running app:

```dotenv
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=
```

Set all four to the real private bucket credentials in the VPS's `.env` beside
`app.py`, or in the production service's environment. The tracked `.env.example`
contains empty placeholders; it cannot enable uploads by itself. Keep credentials
private. Edit an existing `.env` rather than replacing it. Exported service
environment variables override `.env`, even when an exported value is empty.

Install dependencies with the **same Python interpreter used by Gunicorn/WSGI**.
The examples below assume that interpreter is `.venv/bin/python`:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

Install FFmpeg with the VPS's package manager so `ffprobe` is available. On
Ubuntu/Debian:

```sh
sudo apt-get update
sudo apt-get install ffmpeg
ffprobe -version
```

Set `FFPROBE_BIN` to the executable's absolute path if the service's PATH does not
include it. In the private R2 bucket's CORS policy, add the exact production HTTPS
origin and retain the methods, headers and exposed `ETag` described in the
[recording setup](../README.md#cloudflare-dashboard-setup-manual).

## Notifications

The notification unavailable message appears when push is disabled or its private
key is absent, unreadable or invalid. Set `KYNNECTED_PUSH_ENABLED=1` and run as the
application's OS user with the production environment loaded:

```sh
.venv/bin/python -m flask --app app setup-push
```

This creates `instance/push-private.pem` only if it does not exist. It preserves
existing keys. If `KYNNECTED_PUSH_PRIVATE_KEY_FILE` is configured, that path is used
instead. The application service must be able to read the key; all workers must
use the same persistent key and database. See [Web Push setup](web-push.md) for
background delivery and device requirements.

## Restart and verify

Set `KYNNECTED_HTTPS=1` for production HTTPS and `KYNNECTED_PUBLIC_URL` to the
site's HTTPS origin. Restart the actual application service after setup. For a
systemd-managed deployment, replace `YOUR_APP_SERVICE` with its real unit name:

```sh
sudo systemctl restart YOUR_APP_SERVICE
sudo systemctl status YOUR_APP_SERVICE --no-pager
```

Run this diagnostic under the same interpreter, OS user and environment as the
service. It prints presence/readiness only, never credentials:

```sh
.venv/bin/python - <<'PY'
import importlib.util
import shutil
import sys
from app import app
from push_notifications import ready

print('Python:', sys.executable)
for name in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME'):
    print(name + ':', 'set' if app.config.get(name) else 'MISSING')
print('boto3:', 'installed' if importlib.util.find_spec('boto3') else 'MISSING')
print('ffprobe:', 'found' if shutil.which(app.config['FFPROBE_BIN']) else 'MISSING')
with app.app_context():
    print('Notifications:', 'ready' if ready() else 'NOT READY')
PY
```

This checks local setup, not bucket permissions or browser delivery. Reload the
live site, upload two short test videos to a test lesson, and verify both playback
and learning hours. Enable notifications on a recipient device and send a message
from its paired test account. If the live site still reports missing settings
while the diagnostic passes, check that the service uses the same checkout,
Python environment and configuration, and that every worker was restarted.
