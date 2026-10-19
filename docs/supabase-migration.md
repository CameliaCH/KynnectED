# Full Supabase migration

The agreed target is Supabase Postgres, Supabase Auth, and private Supabase Storage.
Preserve the remaining `cameliach` account and existing records, including numeric account IDs,
pairings, approval/consent history, messages, attendance calculations, reviews, and
safeguarding retention. Flask continues serving the website. Password recovery
continues to show **Contact the KynnectED team** until email delivery is ready.

At the user's request, the seven other accounts were removed; their history remains.
Do not import an older snapshot that would restore the removed accounts.

The database integration and importer are implemented. Adding credentials alone
does not switch the app: `KYNNECTED_DATA_BACKEND=supabase` selects Postgres after
the import. The default `local` mode uses JSON and SQLite. Authentication still
runs through Flask and recordings still use R2; Supabase Auth and private Storage
remain separate stages of the full migration.

## Completed database cutover — 18 October 2026

The seven public tables have been created and the local `.env` now selects
`KYNNECTED_DATA_BACKEND='supabase'`. The imported snapshot is
`instance/supabase-migration/20261018T125340Z-v0yhvslh/`: one account (`cameliach`),
four lessons, fourteen messages, two recording records and two upload records.
Materials and missing-recording reports are empty. Every imported field was
compared with the snapshot, and RLS and Data API restrictions were verified.
The recording records are metadata; the video objects remain in their original storage.

Refresh Supabase's **Table Editor**, select the **public** schema, then open
**profiles** to see the retained account. Supabase **Authentication → Users**
will remain empty until the separate Auth migration is completed.

The local server was restarted using the project virtual environment. To start it
again later, run `.venv/bin/python app.py` from the project directory, then open
`http://localhost:5001` and sign in with the existing username and password.
Keep the backup and original local stores until the remaining migration is verified.

## Database import and switch

`supabase/migrations/202610180001_database.sql` creates these public tables:
`profiles`, `lessons`, `messages`, `missing_recording_reports`, `resource_materials`,
`recordings`, and `recording_uploads`. Profiles retain all account details in JSONB,
including existing partner IDs, with common fields exposed as columns for the
Table Editor. Password hashes and reset tokens are stored separately in
`kynnected_private.account_credentials`. They are never stored in public profiles.

All application tables have RLS enabled and Data API grants revoked. Flask uses
its server-only Postgres connection and retains existing permission checks. The
publishable and secret API keys cannot access these tables until explicit grants
and Auth policies are implemented. Supabase's Table Editor can view public tables
as an administrator; select `public` in its schema selector.

Create a fresh backup while app writes are paused, then validate/import it:

```sh
python3 scripts/prepare_supabase_migration.py
python3 scripts/migrate_supabase_database.py instance/supabase-migration/NEW_SNAPSHOT
python3 scripts/migrate_supabase_database.py instance/supabase-migration/NEW_SNAPSHOT --apply
```

The importer verifies the backup checksums, creates the schema, imports data,
checks every table's row count, and commits atomically. Existing application tables
cause an abort. Re-running the same completed import reports `already_imported`
and does not overwrite later activity. A different snapshot requires a separate,
reviewed migration; it cannot overwrite this import.

After validation, set `KYNNECTED_DATA_BACKEND='supabase'` in `.env` and restart
`.venv/bin/python app.py`. The app now loads `.env` automatically (exported environment
variables take precedence). Switching database backends signs out old sessions;
the retained account's username and password continue to work. In Supabase mode,
signup, pairing, approval, lessons, chat, CSVs and reset credentials use Postgres.
There is no silent fallback to local storage on a database error.

Postgres transactions serialize account edits and application writes, preserving
the SQLite/file-lock check-then-write behaviour. Numeric IDs and ordering IDs are
preserved, and sequences are advanced beyond retained historical references.

For rollback, stop app writes and reconcile any records created since the switch
before changing back to `KYNNECTED_DATA_BACKEND=local`. The original local stores
are retained, but become stale once the site starts writing to Supabase.

The default automated suite uses temporary local fixtures. Live Postgres tests
require `KYNNECTED_POSTGRES_TEST_URL`; they create randomly named temporary schemas,
use synthetic accounts, and remove those schemas afterward. They never touch the
real application tables or Supabase Auth users.

## Preparation

Install the optional migration tools in the project virtual environment:

```sh
python3 -m pip install -r requirements-supabase.txt
python3 scripts/prepare_supabase_migration.py
```

The script creates a private, git-ignored directory under
`instance/supabase-migration/` containing:

- An unchanged copy of `users.json`.
- A consistent SQLite snapshot, including committed WAL data, with an integrity check.
- Copies of local materials and legacy recording files.
- A manifest with record counts and SHA-256 checksums.
- `account-emails.csv`, a worksheet containing account IDs, usernames, existing
  emails, proposed migration emails, and readiness status. It contains no passwords.

The snapshot contains private account data, including existing password hashes;
keep it local and out of Git. Remote R2 objects require a separate inventory and
copy during the storage migration. Take a fresh snapshot with writes paused before
the final import; an early preparation snapshot does not include subsequent activity.

## Resolve account identities

Fill the worksheet's `migration_email` column with a real, accessible, unique
email address for every account. Missing email addresses and duplicate addresses
must be resolved without deleting or combining accounts. Leave `account_id` and
`username` unchanged. Editing the worksheet does not edit live accounts.

Existing numeric IDs will remain application IDs. Add a unique Supabase Auth UUID
mapping to each account, so lessons and pairings retain their existing references.
Supabase Auth supports importing Argon2 hashes through the admin API; confirm the
existing hash parameters in an isolated migration test before importing users.
Do not mark an email verified just because it appears in the local JSON store.

## Remaining work for full Supabase Auth and Storage

1. Integrate Supabase Auth signup/login and server-side user verification. Preserve
   the existing username-facing flow through an explicit, private account mapping.
   Import users via the admin API and keep password hashes out of public profiles.
2. Add explicit Data API grants and RLS policies. Automatic RLS does not create
   participant/coordinator permissions. Secret-key access bypasses RLS, so server
   operations must retain the app's authorisation checks.
3. Create private Storage buckets for materials and recordings. Port multipart or
   resumable uploads, video validation, authenticated playback, replacement checks,
   seven-day expiry, and safeguarding retention. Ordinary and flagged recordings
   must not share a blanket deletion rule.
4. Import Auth users and Storage objects with repeatable checkpoints; verify IDs, row
   counts, account mappings and file checksums. Run permission tests as mentor,
   mentee, unrelated user, pending mentor, and coordinator.
5. During a maintenance window, take a final backup, import remaining changes,
   verify the destination, and switch configuration. Keep original stores and R2
   objects available for rollback until the migrated site is verified.

## Project configuration

Enable Data API and automatic RLS; disable automatic exposure of new tables.
Keep `SUPABASE_SECRET_KEY` and `SUPABASE_DATABASE_URL` server-side. Do not commit
`.env`. Use the exact session pooler connection string from the Connect dialog,
with the database password substituted and URL-encoded where necessary.

Set the local Auth Site URL to `http://localhost:5001`. Add the exact authentication
callback paths after they are implemented, and use the deployed HTTPS URL in
production. Keep email confirmation and email recovery rollout dependent on a
working mail provider. Supabase's default email service is restricted to project
team addresses and is not suitable for general user delivery.

The existing app accepts recordings up to 2 GB. Supabase Free only supports files
up to 50 MB, so preserving the current recording limit requires Pro or above with
appropriate global and bucket limits. File-size limits, storage capacity and
download bandwidth all need to be considered before moving real recordings.

References: [database connections](https://supabase.com/docs/guides/database/connecting-to-postgres),
[Auth password import](https://supabase.com/docs/guides/platform/migrating-to-supabase/auth0),
[Storage limits](https://supabase.com/docs/guides/storage/uploads/file-limits),
[SMTP requirements](https://supabase.com/docs/guides/auth/auth-smtp).
