# Local storage

The application uses its original storage setup:

| Data | Default location |
| --- | --- |
| Accounts, password hashes, partners, approval and consent | `users.json` |
| Lessons, messages, reports, material and recording metadata | `instance/learning.sqlite3` |
| Password reset rate limits | `users.reset-limits.sqlite3` |
| Uploaded lesson materials | `instance/materials/` |
| Legacy recording files | `instance/recordings/` |
| New recording videos | Existing private Cloudflare R2 bucket |
| Session signing key | `instance/session-secret` or `KYNNECTED_SECRET_KEY` |

`KYNNECTED_USERS_FILE`, `KYNNECTED_LEARNING_DATABASE`, `KYNNECTED_MATERIALS_DIR`
and `KYNNECTED_RECORDINGS_DIR` can override the local paths. All workers must
share these files on the same server. The app no longer selects a remote database
backend. Existing R2 and email settings still apply.

Account updates use a separate file lock and an atomic replacement, preserving
passwords and other fields. Stop the app before editing JSON manually; do not
change account IDs without also updating their references. SQLite keeps activity
writes transactional. Keep all data files outside publicly served directories.

## Backups

Stop all application workers and background jobs before taking a backup that must
be consistent across accounts, activity and uploaded files. Run:

```sh
.venv/bin/python scripts/backup_local_data.py
```

This creates a private timestamped directory under `instance/backups/`, including
accounts, a SQLite backup (including committed WAL data), material files, legacy
recordings, row counts and SHA-256 checksums. A failed backup has an `INCOMPLETE`
marker and must not be used for restoration. These backups contain personal data
and password hashes; keep them private and outside version control.

Back up `.env`, the session signing key and the password reset counter database
separately while the app is stopped. R2 objects remain in R2 and are not downloaded
by this script. Preserve the existing bucket configuration and retention rules.

## Restoration completed on 28 October 2026

The local account file contains all six current accounts with their existing
password hashes and complete profile data. The five previously local accounts
were matched by username; four now retain their current account IDs (10–13),
avoiding a collision with account 9. IDs and fields match the exported live
account records exactly. Approval values were preserved without reinterpretation.

All four lessons, fourteen messages, two recordings and two upload records were
already identical in local SQLite and the remote export. They were left intact,
as were local password reset counters. Materials and missing-recording reports
were empty in both databases. R2 files were not moved or deleted.

Private pre-change files and the remote snapshot are retained under
`instance/local-restore/20261028T115625Z-my7whfro/`. The local session signing key
was rotated because older sessions may reference reassigned local account IDs;
users must sign in again. Account passwords and reset links remain unchanged.

## Deploying or restoring another server

These changes restore this checkout and its local data. They do not update a
separate production server. Before deploying, stop its writers and reconcile any
accounts or activity created there since this snapshot. Back up that server first.
Deploy the code with its verified account file, activity database and uploaded
files together, preserving the configured paths and R2/email settings. Remove old
database credentials and rotate the session signing key at the switch to prevent
old sessions from resolving to different accounts.

Restore SQLite while all workers are stopped, using SQLite's backup API rather
than replacing a database beneath live `-wal`/`-shm` files. Restore the account
file and uploads from the same backup, then restart and verify login, coordinator
records, pairing rooms and recording access. Keep the pre-change backup until
the restored service is verified.
