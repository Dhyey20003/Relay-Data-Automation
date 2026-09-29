# Design notes and assumptions

## Assumptions made

- **Platform**: Windows 10/11 64-bit, Python 3.12 for the packaged build. Classic Outlook
  (Microsoft 365 Apps / Outlook 2016+) configured for the operator's Windows account.
- **RHC IDs** are stored as text, trimmed and upper-cased (`rhc1234` becomes `RHC1234`),
  letters, digits, `-`, `_`, `.` only; leading zeros are kept. Matching is case-insensitive.
- **Dates**: numeric dates in imports are read day-first (`03/04/2026` = 3 April 2026),
  configurable in Settings. Excel date cells and Excel serial numbers are converted exactly.
  Business dates are plain dates; timestamps are stored in UTC and shown in Asia/Kolkata.
- **Default view** shows Active employees only. Pending Review employees are shown via the
  view list and are excluded from packages (listed as exclusions).
- **Official email** must be unique across all records when present.
- **Historical enrichment** fills only blank approved fields by default
  (date of joining, date of birth, department, official email).
- **Roster import** also reactivates matched employees who were Left or historical-only
  (shown as changes in the preview before commit).
- **Packages**: employees without a location, RHC ID or readable photo are excluded and
  listed. Incomplete packages are labelled incomplete and are not sent automatically
  unless an administrator allows it for that task. With "only new or changed", an
  employee is included when no successful submission recorded them, or their photo
  version, location or RHC ID changed since.
- **HR JPG**: JPEG originals are copied byte for byte; other formats are re-encoded to
  real JPEG (quality 92, EXIF orientation applied, transparency on white).
- **IT duplicate names**: all colliding names get `_<RHC ID>` before the extension
  (sorted by RHC ID, so the result is deterministic); a numeric suffix is added only if
  a collision would remain.
- **Manifests** are kept next to each ZIP (JSON and CSV), not inside the ZIP sent to IT/HR.
- **Backups** exclude package ZIP files by default (they can be regenerated as a new
  revision; manifests are included). This can be switched on in Settings.
- **Restore** keeps the current computer's `config.json` (network settings), because
  network settings belong to the computer, not to the data.

## Bench-day calculation

`bench_days = today - bench_start` in calendar days, where today is the date in the
application timezone. It is 0 on the start date. It is shown only when the assignment
status is Bench, the start date is not in the future and the bench end date (if any) is
today or later. Otherwise the field is blank.

## Concurrency and safety

- Every write uses `BEGIN IMMEDIATE`; SQLite runs in WAL mode with a busy timeout.
- Employee edits carry the record version; a stale version returns 409 with the current
  values and nothing is overwritten.
- Imports re-run the analysis inside the commit transaction and refuse to commit if any
  affected record changed since the approved preview.
- One active email per task period is enforced by a partial unique index on the
  dispatches table, so concurrent clicks or workers cannot send twice.
- A send interrupted by a crash becomes Delivery Uncertain on restart and is never resent.
- `hub.lock` (with heartbeat) prevents two running copies on one data folder; a host
  change is detected and pauses automatic sending until revalidated.

## Security model

- Passwords: scrypt (N=32768, r=8, p=1) with per-user salt. Sessions: random tokens stored
  hashed, HttpOnly SameSite=Strict cookie, idle and absolute expiry.
- CSRF: custom header required on every mutating request, per-session CSRF token, and
  Origin check. Host header allow-list defeats DNS rebinding. Strict CSP (scripts from
  self only).
- Permissions are enforced by the server for every endpoint, download, photo and export.
- Fetch tokens are hashed, revocable, optionally expiring, limited by location and field,
  and can never read restricted fields.

## Extension points (future work, not implemented)

- **Birthday / work-anniversary emails**: add a task `kind` with its own package builder
  (`packages.collect`) and template fields; scheduling, periods, dispatch locking,
  reconciliation and audit are generic.
- **Additional locations**: Settings > General; tasks create one package per configured
  location.
- **Office LAN access**: Settings > Network (off by default).
- **Read-only automation**: `/api/fetch/v1/employees` with integration tokens.
- **OCR** for scanned PDFs could be added locally (for example Tesseract); not included.
