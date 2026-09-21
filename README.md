# KynnectED

Run locally with `python3 app.py`, then visit http://localhost:5001.

## Accounts and coordinator access

Log in with an existing username and password. Mentors and mentees land on
`/dashboard`, where they see the accounts linked to them. Coordinators land on
`/coordinator`, with links to manual pairings and participant CSVs.

Grant an existing account Coordinator access from the project directory:

```sh
python3 -m flask --app app set-coordinator USERNAME
```

Use `--revoke` to remove that permission. Coordinator access is independent of
the account's mentor/mentee role; signup cannot grant it. Records, CSV downloads,
and all pairing changes require a logged-in coordinator.

On the Pairings page, choose a mentor and mentee, review their profiles, and
select **Save pairing**. Both sides can have multiple connections. **Unlink**
removes only the selected connection. The existing `partners` arrays in
`users.json` store the other account's numeric ID on both sides. CSVs include
these IDs. Pairing updates and signups use a file lock and atomic replacement
to avoid partial writes and lost updates between app requests.

Session signing uses `KYNNECTED_SECRET_KEY` if set; otherwise a local key is
created in the git-ignored `instance/session-secret` file. Keep that key stable
across restarts. Sessions expire after eight hours. Set `KYNNECTED_HTTPS=1` when
serving over HTTPS to require secure session cookies. `KYNNECTED_USERS_FILE`
can point to a separate account store for isolated testing.

## Pairing rooms and lessons

Click a pairing on your dashboard to open its private room. Coordinators can
also open rooms from **Pairings** or **Lessons & recordings**.

- Mentors and coordinators can add, edit, and cancel lessons. Dates use
  `Asia/Kuala_Lumpur` (UTC+8). Paste an existing HTTPS Zoom meeting link;
  the app does not create Zoom meetings or start recording automatically.
- Record the session in Zoom, then upload the MP4 (H.264) or WebM to that lesson.
  Past lessons can also be added. Uploads are limited to 1 GB and require
  **FFmpeg's `ffprobe`** on the server (`brew install ffmpeg` on macOS).
- A successful upload marks both participants present. Completed learning
  hours use the video's duration. Replacing a recording updates that same
  attendance entry and resets its quality review; it does not count twice.
- Both participants can watch and download recordings and exchange messages
  inside their room. Chat is saved and checks for new messages automatically.
  Coordinators can read chat for support, watch recordings, and save private
  quality-review notes.
- Unlinking a pair removes participants' room access and prevents new activity.
  Completed hours remain in their totals; coordinators retain access to the
  archived history. Relinking the same accounts restores their room.

Activity is stored in `instance/learning.sqlite3` and private videos in
`instance/recordings/`; neither is publicly served from `static/` or committed
to git. Back up these together with `users.json` when moving the app. Video
playback, including seeking, passes through authenticated routes.

For isolated test instances, set `KYNNECTED_LEARNING_DATABASE` and
`KYNNECTED_RECORDINGS_DIR` alongside `KYNNECTED_USERS_FILE`. `FFPROBE_BIN` can
point to the ffprobe executable if it is not on the server's PATH.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

The tests use temporary account stores rather than changing real registrations.
