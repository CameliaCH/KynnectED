# KynnectED

Install dependencies and configure recording storage using the commands below, then visit http://localhost:5001.

## Public website and resource library

The public site includes Home (`/`), About (`/about`), For Students & Centres
(`/students-and-centres`), Volunteer (`/volunteer`), Resources
(`/learning-resources`), and Contact (`/contact`). Mentor and student calls to
action use the existing signup flow. Team applications open the public
KynnectED Google Form. Partnership enquiries use the contact page and email.
Contact email and message icons open `kynnect.org@gmail.com`; no phone number
is displayed. Shared contact actions live in `templates/contact_actions.html`.

Visitors are prompted to log in to open resource folders; folder URLs and slide
downloads also require authentication on the server. Signed-in mentors and
mentees can open every folder. Safeguarding guidance contains the website Mentor
Code of Conduct, with links to read and download it. Folder definitions live in
`RESOURCE_GROUPS` in `app.py`.

Approved mentors can submit PDF or PowerPoint (.pptx) slide materials up to 25 MB
to any **For students** folder through `/learning-resources/submit`. Only admins
(accounts with coordinator access) can upload to **For mentors** folders; admins
can also upload to student folders. These restrictions apply to both the folder
buttons and upload requests on the server. Each submission has a title and destination folder,
starts as pending, and is accessible only to its author and coordinators until
approved. Coordinators review files at `/coordinator/materials` and approve or
decline them with optional feedback. Only approved materials appear in folders;
declined materials remain available to the author with feedback. Files live in
the private `MATERIALS_DIR` directory (default `instance/materials`, configurable
with `KYNNECTED_MATERIALS_DIR`), and metadata and approval records live in SQLite's
`resource_materials` table. Existing databases add this table on their next
connection. Deployment must preserve the database and material directory.

The visual refresh lives in `static/css/redesign.css`, layered over existing
platform component styles. The original logo and classroom image are reused.
Instagram links to `https://www.instagram.com/kynnected_org/`; LinkedIn links to
`https://www.linkedin.com/in/kynnected-malaysia-9672bb442`. Team portraits are still
pending. Contact details appear in
`templates/base.html`, `templates/contact.html`, and the safeguarding section
of `templates/students_centres.html`.

The About page's **Our team** section is temporarily hidden. Its original markup
is saved in `templates/about_team.html`; restore it by including that template
where the saved-section comment appears in `templates/about.html`.

## Mentor Code of Conduct

`/code-of-conduct` displays the full website document with links to open or
download the original PDF. It is linked from the footer, the mentoring section, and mentor
dashboards. Both original PDFs live in `static/documents/`; the extracted text
used for the inline reader lives in `content/mentor-conduct.json`.

At the last signup step, mentors must review the signup PDF and tick the
Mentor Declaration checkbox before completing signup. Mentees and the
availability preview do not require this declaration. The server validates
acceptance, the document version, and the signup PDF's SHA-256 digest. New
mentor records store `mentor_code_of_conduct` with the version, document digest,
and UTC acceptance time. Existing accounts are unchanged.

When replacing the documents, update `MENTOR_CONDUCT_VERSION` in `app.py`,
refresh the inline document text from the PDFs, and restart the app. The current PDF
digest is calculated on startup so an old signup page cannot silently accept
a replacement document.

## Accounts and coordinator access

Lessons move into **Finished lessons** after their scheduled end time, even
before a recording is uploaded. Mentees see every finished lesson and can open
available recordings; mentors see **Awaiting upload** and an upload link for
missing recordings. The dashboard and pairing room use the same timing rule.
Finishing a scheduled lesson does not automatically add attendance or hours.
After the scheduled start time, lessons cannot be edited or cancelled, including
through direct requests. Recording uploads remain available.

Mentors can submit **Don't have a recording?** explanations for finished lessons
without a video. The reasons include forgotten, failed, lost, or unuploadable
recordings, lessons that did not take place, and Other with a required explanation.
The database keeps one report per lesson, and repeat submissions cannot increase
the counter. Reports are attributed to the mentor, across all pairings, and are
retained if a recording is uploaded later. Explanations never mark attendance.
Coordinators can see total and per-reason counts in lesson monitoring. The mentor
CSV contains those counts, a flag, and the full responses with lesson IDs, dates,
notes, and upload status. Mentor names appear red in monitoring and records after
more than three reports; removal from the programme is not automatic.

The site states that recordings are due within one week of the scheduled lesson
end and displays due dates and overdue notices. It accepts late uploads to allow
recovery. New recordings upload directly to a private Cloudflare R2 bucket. The upload
retention period starts when the video finishes uploading, separately from the
one-week deadline after the scheduled lesson.

New mentor signups require a contact email and start with
`mentor_approval_status: pending`. Admins (existing Coordinator accounts) can
review them in the separate pending mentor table at
`/coordinator/mentor-approvals` or **Records & CSVs**, contact them using their
email, and choose **Approve mentor**. Pending applications also have a separate
CSV at `/export/pending-mentors.csv`. After approval, the mentor appears in the
main mentor table, CSV, and pairing choices. Mentor CSVs include contact email
and application/approval metadata.

Approval uses the atomic account store and records the approving admin and UTC
approval time. Signup cannot set its own approval state or admin permissions.
Existing mentors without an approval field remain approved. Pending mentors
can sign in to an **Awaiting approval** dashboard and read resources and the Code
of Conduct, but cannot be paired, use pairing rooms, or submit slide materials.
An existing login sees the new access on its next request after approval.

Log in with an existing username and password. Approved mentors and mentees land
on `/dashboard`, where they see the accounts linked to them. Coordinators land on
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

- Mentors can add, edit, and cancel their lessons. Admins have read-only room
  access, including accounts that also have the mentor role or are paired.
  Admins cannot schedule, edit, cancel, upload recordings, submit missing-recording
  explanations, or send chat messages. Dates use
  `Asia/Kuala_Lumpur` (UTC+8). Paste an existing HTTPS Zoom meeting link;
  the app does not create Zoom meetings or start recording automatically.
- Record the session in Zoom, then upload MP4/MOV (H.264) or WebM (VP8/VP9/AV1)
  to that lesson. Past lessons can also be added. New uploads go directly from
  the browser to R2, up to 2 GiB (shown as 2 GB). The server requires **FFmpeg's
  `ffprobe`** (`brew install ffmpeg` on macOS) to validate video metadata remotely.
  Only the approved mentor assigned to the session can upload.
- A successful upload marks both participants present. Completed learning
  hours use the video's duration. Replacing a recording updates that same
  attendance entry and resets its quality review; it does not count twice.
- Both participants can watch available recordings and exchange messages
  inside their room. Chat is saved and checks for new messages automatically.
  Coordinators can read chat for support and watch recordings. Quality reviews
  and safeguarding retention are managed from
  **Lessons & recordings**, outside the read-only pairing room.
  Admins select an admin reviewer and add comments before marking reviewed.
  The final reviewer name, date, and comments are visible to the paired mentor
  and student. Marking reviewed is permanent: the form disappears and the server
  rejects later changes. The selected reviewer's name is saved with the review;
  the admin who submitted it is also recorded separately. A recording with a
  final review cannot be replaced, including by an upload already in progress.
  Safeguarding notes remain admin-only; flagging never changes the final review.
- Unlinking a pair removes participants' room access and prevents new activity.
  Completed hours remain in their totals; coordinators retain access to the
  archived history. Relinking the same accounts restores their room.

Activity and recording metadata stay in `instance/learning.sqlite3`; new videos
live in R2. Older disk recordings in `instance/recordings/` remain readable
through the existing authenticated route, including seeking and download.
They are not moved to R2 or automatically given a seven-day expiry. Preserve
that directory for existing files, along with SQLite, slide materials and
`users.json`, when moving the app. The old video POST endpoint is disabled;
there is no fallback that uploads new videos through Flask.

For isolated test instances, set `KYNNECTED_LEARNING_DATABASE` and
`KYNNECTED_RECORDINGS_DIR` alongside `KYNNECTED_USERS_FILE`. `FFPROBE_BIN` can
point to the ffprobe executable if it is not on the server's PATH.

## Private Cloudflare R2 recordings

The integration uses the existing Flask login, CSRF checks, mentor approval,
reciprocal pairings, SQLite lesson IDs and coordinator permission. No separate
accounts or database service are needed. The UI preserves the existing palette.

### Install and run locally

Use Python 3.10 or newer for ongoing SDK support. From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
brew install ffmpeg
cp .env.example .env
```

Fill `.env` with the values below, then load it and run the app:

```sh
set -a
source .env
set +a
python3 app.py
```

The app reads environment variables; it does not automatically read `.env`.
Restart the app after changes. `.env`, `.venv` and `instance/` are git-ignored.
Never put actual credentials in source, templates or commits.

| Variable | Value |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 S3 token's Access Key ID |
| `R2_SECRET_ACCESS_KEY` | R2 S3 token's Secret Access Key |
| `R2_BUCKET_NAME` | Private bucket name, e.g. `kynnected-recordings` |
| `R2_ENDPOINT` | S3 endpoint shown by Cloudflare, e.g. `https://ACCOUNT_ID.r2.cloudflarestorage.com` |

If `R2_ENDPOINT` is empty, the standard endpoint is derived from the account ID.
Use the exact jurisdiction-specific endpoint for a jurisdictional bucket.
Optional existing settings: `KYNNECTED_SECRET_KEY` (a stable random secret),
`KYNNECTED_HTTPS=1` when serving HTTPS (leave `0` locally),
`KYNNECTED_USERS_FILE`, `KYNNECTED_LEARNING_DATABASE`, `KYNNECTED_RECORDINGS_DIR`
(for legacy files), `KYNNECTED_MATERIALS_DIR`, and `FFPROBE_BIN`.

### Cloudflare dashboard setup (manual)

1. Open **Cloudflare dashboard → R2 object storage → Overview**. Activate R2
   if needed, select **Create bucket**, name it `kynnected-recordings` (or your
   chosen name), use Standard storage, and select **Create bucket**.
2. Open the bucket's **Settings**. Leave the **Public Development URL (r2.dev)**
   disabled and do not connect a public custom domain or a public-serving Worker.
3. Return to R2 Overview. Under **Account Details → API Tokens**, select
   **Manage → Create Account API token** (or **Create User API token**).
   Choose **Object Read & Write**, scope it to **this bucket only**, and create
   it. Copy the Access Key ID and Secret Access Key into `.env`. Set the bucket,
   account ID and S3 endpoint from Cloudflare. These stay on the server.
4. Open **bucket → Settings → CORS Policy → Add CORS policy → JSON**. Paste:

   ```json
   [
     {
       "AllowedOrigins": ["http://localhost:5001", "http://127.0.0.1:5001"],
       "AllowedMethods": ["PUT", "GET", "HEAD"],
       "AllowedHeaders": ["Content-Type", "Range", "Content-Length"],
       "ExposeHeaders": ["ETag", "Content-Length", "Content-Range", "Accept-Ranges"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

   Select **Save**. Add your exact HTTPS production origin when deploying;
   remove local origins from production-only buckets. CORS permits the browser
   request; it does not make objects public. Exposing `ETag` is required by the
   upload interface. Do not use a wildcard origin.
5. Open **Settings → Object Lifecycle Rules → Add rule**. Name the rule
   `session-recordings-seven-days`, set **prefix `sessions/`**, choose deletion
   after **7 days**, and **Save changes**. Do not make it a whole-bucket rule.
   **No object-deletion rule may cover `flagged/`, including a blanket rule.**
6. Keep the default rule that aborts incomplete multipart uploads after seven
   days, or add an **abort incomplete multipart uploads** rule for `sessions/`.
   This cleans abandoned chunks; it is distinct from deletion of saved objects.

Provider references: [bucket creation](https://developers.cloudflare.com/r2/buckets/create-buckets/),
[private/public access](https://developers.cloudflare.com/r2/buckets/public-buckets/),
[R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/),
[CORS](https://developers.cloudflare.com/r2/buckets/cors/), and
[object lifecycle rules](https://developers.cloudflare.com/r2/buckets/object-lifecycles/).

### Upload, playback and safeguarding

- The mentor's finished lesson shows **Upload Session Recording**, its date
  and mentee, the file input and the required, initially unchecked confirmation.
  The backend checks the same confirmation, mentor approval, assignment, pairing,
  session timing, content type and declared size before creating a multipart upload.
- Browser uploads use 16 MiB chunks, three at a time, with progress, cancellation
  and up to three retries per failed chunk. Each retry gets a fresh 15-minute
  signed part URL. Part URLs bind the exact chunk size. Videos never upload to
  Flask; only small authenticated control requests do.
- The backend checks R2's actual part sizes/ETags, completes the upload, checks
  object size/type/upload identity and probes duration/codecs with `ffprobe`
  using a trusted, temporary R2 URL. This reads video metadata remotely, with
  timeouts; it does not save a full video on the application server. It ignores
  browser-reported durations. Only then are recording metadata and attendance
  saved together. Failed attempts do not change an existing recording or hours.
- Completion is idempotent: a lost response can be retried without a second
  attendance entry. Starting again cancels an unfinished upload for that session.
  Upload permissions last at most 24 hours. Closing a tab loses browser progress;
  reopening starts again, while R2's multipart lifecycle cleans abandoned parts.
- `recordings` and `recording_uploads` are **additive SQLite tables**, created on
  the next database connection. **No migration command is required.** Existing
  lesson/chat/material/report tables, JSON accounts and attendance remain intact.
  Records store object keys, filenames, sizes, upload/expiry times, review status,
  reviewer metadata and safeguarding metadata. No permanent R2 URLs are stored.
  Upload state also records consent time and the version being replaced.
- Uploaded videos use `sessions/{session_id}/{random_uuid}.{extension}`.
  Expiry is R2's upload-completion timestamp plus seven days. Dashboards retain
  session history/hours after expiry and stop offering playback. R2 performs
  actual deletion asynchronously; the application enforces expiry independently.
- **Watch recording** opens the site player. Every request for a playback URL
  checks assignment/pairing or coordinator status on the server. URLs last at
  most 20 minutes, shortened near expiry, and refresh through authenticated
  requests during long playback. Unlinking revokes new playback requests;
  an already-issued URL remains usable until its short expiry. Signed URLs are
  temporary bearer access, so they should not be shared or logged.
- Coordinators open **Coordinator → Lessons & recordings → Recording review**
  to watch, select an admin reviewer, finalise comments, or flag a safeguarding
  concern. Final review comments are visible to the paired mentor and student;
  safeguarding notes remain visible only to coordinators. Reviews cannot be
  edited after marking reviewed. Existing databases automatically add reviewer
  attribution columns while preserving previous reviews. Flagging requires a second confirmation,
  copies the video to `flagged/{session_id}/{random_uuid}.{extension}`, verifies
  the retained copy, and only then saves the flag, coordinator identity and time.
  A failed copy cannot falsely mark a video retained. Flag before normal expiry.
- Flagged videos have no automatic expiry. The original `sessions/` copy can
  expire normally. Replacements require explicit confirmation/version matching,
  reset quality review, and update the same session's hours. Older flagged
  versions remain in the coordinator review list; the application never
  automatically deletes a `flagged/` object.
- New recordings require R2 configuration. Existing pages and legacy recordings
  still work without it; a new upload reports that storage needs configuration.
  Do not enable SDK debug logging or log temporary signed URLs.

### Checks and live access test

With the virtual environment active:

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -p 'test_recordings.py' -v
```

Automated tests use temporary JSON/SQLite stores and a simulated R2 client,
plus actual SDK signing and FFmpeg validation. They do not need live credentials
or modify real registrations. They cover assignment/approval, CSRF, required
consent, type/size checks, part permissions, incomplete/invalid uploads,
idempotent completion, attendance, playback authorization, expiry/missing objects,
shared final reviews, private safeguarding notes, flag copy failures, retained replacements and legacy files.

After completing Cloudflare setup, test a real short Zoom recording:

1. Create an approved mentor, an assigned mentee, an unrelated mentor/mentee and
   a coordinator. Grant coordinator access if needed:
   `python3 -m flask --app app set-coordinator USERNAME`. Approve the mentor and
   link the assigned pair through the existing coordinator pages.
2. Add a past session. As its mentor, choose an MP4/WebM/MOV. Confirm upload is
   disabled until the checkbox is checked. Upload and check progress/success.
   Browser Network tools should show video chunks going **directly to the R2
   S3 endpoint**, with only JSON control requests to Flask. Check attendance/hours.
3. As the assigned mentee, open **Finished lessons → Watch recording**. Confirm
   video playback/seek and the expiry date. In separate logged-in browsers, open
   that same watch/API address as an unrelated mentor, unrelated mentee and
   logged-out visitor: all must be denied. Coordinator playback must work.
4. As a coordinator, choose an admin reviewer, add comments, and mark reviewed.
   Check the selected name and comments appear in both participant views, and
   the review form disappears. Repeating the review POST must return 409 and
   leave the review unchanged. Confirm ordinary users cannot submit
   `/recordings/RECORDING_ID/review` or `/recordings/RECORDING_ID/flag`.
5. Flag a test concern and confirm it. Check R2 contains a verified `flagged/`
   copy and the coordinator list shows **Flagged / Retained**. Check that no
   deletion rule covers that prefix. Replace the lesson recording as its mentor:
   attendance must count once and the flagged previous version must remain.
6. Check a normal recording after seven days: playback should stop while session
   history remains. The automated expiry test provides an immediate check without
   changing production timestamps. Finally test a representative 500 MB–2 GB
   file, throttling or briefly interrupting the connection to check chunk retries.

R2 account/bucket setup and a real provider upload cannot be verified by the
credential-free automated suite; complete the live checks before rollout.
