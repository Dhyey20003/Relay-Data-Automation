# Relay Employee Hub

A local Windows application for Relay Human Cloud that keeps employee records and
headshots, imports HR spreadsheets, and prepares and sends the recurring IT and HR
headshot packages through classic Outlook.

- Python + FastAPI + SQLite, with a browser interface served from the same computer.
- Everything runs offline. All frontend files are bundled locally (no CDNs, no cloud
  database, no telemetry, no external AI, no paid APIs). The only network use is
  sending email through the operator's own classic Outlook.
- One designated Windows computer runs the application; other office computers can
  optionally connect over the LAN after an administrator switches that on. Everyone
  uses the same central database.

## Quick start on Windows

**Packaged build (recommended, no Python needed on the office computer)**

1. Get `RelayEmployeeHub-windows.zip` (built by `scripts\windows\build_exe.bat`, or the
   `RelayEmployeeHub-windows` artifact of the GitHub Actions "Windows build and tests" run).
2. Unzip it to a local folder, for example `C:\Program Files\RelayEmployeeHub` or
   `C:\RelayEmployeeHub` (not inside OneDrive).
3. Double-click `RelayEmployeeHub.exe`. The browser opens at `http://127.0.0.1:8765`.
4. Create the first administrator account (there are no default credentials).
5. Optional: run `create_desktop_shortcut.bat` for a desktop icon, and
   `install_logon_startup.bat` to start it in the background when you sign in to Windows.

**From source** (needs Python 3.12 64-bit; downloads the pinned dependencies from PyPI once):

```bat
scripts\windows\setup_from_source.bat
scripts\windows\run_from_source.bat
```

Data is stored in `%LOCALAPPDATA%\RelayEmployeeHub\Data` unless you choose another folder:
`RelayEmployeeHub.exe --set-data-dir D:\RelayHubData`. The application refuses data folders
inside OneDrive, SharePoint, Dropbox or similar synchronised folders.

See [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) for day-to-day use.

## What is included

| Area | Highlights |
|---|---|
| Employees | Excel-like grid: inline editing, multirow paste with preview/validation, search (name, RHC ID, email), sorting, filters, resizable/selectable/reorderable columns, pagination, Add Employee form, profile with photo and history, export of the current view to Excel. Record-version conflict detection. Archive (Left) instead of delete. Assignment, bench and employment history. Calculated bench days. Custom fields (text, number, date, dropdown, yes/no). |
| Import Data | XLSX/CSV by drag-and-drop; worksheet and header-row selection; column mapping with ignore; reusable mapping templates; five purposes (historical, complete roster, partial, designation, selected fields); location scope; RHC ID matching with optional exact-unique-email fallback; name matches are suggestions only; preview of new/changed/unchanged/duplicate/unmatched/invalid/out-of-scope rows and proposed deactivations; atomic commit; downloadable issue report; file hash and repeat detection; safe batch undo that refuses to overwrite later changes. |
| Photos | Single/multiple images, folders, ZIPs, PDFs, and a mapping CSV. Managed storage under permanent employee and photo-version IDs (original file name kept as metadata), separate thumbnails, versions kept. Matching: RHC ID mapping/file name, then confirmable name suggestions, then explicit confirmed sequence mapping. PDF image and text extraction with page context, page rendering and manual crop. Duplicate and same-as-current detection, safe ZIP extraction and limits. No facial recognition. |
| Tasks and Exports | IT task every 14 days from an anchor date; HR task monthly (day past month end uses the last day). Separate Ahmedabad and Vadodara ZIPs, attached together to one email. IT names default to original file names with deterministic RHC ID duplicate resolution; HR names are `RHC1234.jpg` with real JPG conversion. Inclusion choice (all active, or new/changed since last completed submission, tracked by employee, photo version, location and RHC ID). Manifests (JSON and CSV), immutable revisions, statuses Due to Manually Completed, restart recovery, backlog hold, dispatch locking. |
| Outlook | Classic Outlook COM: availability check, sender account, To/CC, subject/body templates, preview, Create Draft with attachments, Send Now, scheduled automatic sending (explicitly enabled per task), attachment size ceiling, dispatch ID reconciliation with Sent Items, uncertain results never resent automatically. Manual mode for new Outlook / Outlook on the web (ready folder + copyable text + audited manual completion). Dry-run adapter for development. |
| Accounts | Local email-username accounts (scrypt-hashed passwords), roles Administrator, Editor, Viewer, Export Operator, plus revocable fetch-only integration tokens. Per-role and per-user permissions, location scope, field restrictions (date of birth full / day-month / hidden). First-run admin setup, login lockout, session expiry, admin password reset, CSRF and origin checks, host-header allow-list, audit log. |
| Backups | One-click and automatic daily verified backups (SQLite online backup API, photos, manifests, settings), configurable destination (external drive), retention (30 daily by default), restore validation with path and hash checks, pre-restore backup, backup before schema migrations, and revalidation of email settings after a restore or computer transfer. |

## Project layout

```
relay_hub/            application package
  api/                HTTP endpoints (auth, employees, imports, photos, tasks, admin, fetch)
  mail/               Outlook COM, dry-run and manual adapters
  static/             local browser interface (HTML/CSS/JS), icon, sample templates
  employees.py imports.py photos.py pdf_util.py packages.py tasks.py dispatch.py backup.py ...
tests/                automated tests (pytest, synthetic data, mock mail)
samples/              synthetic XLSX/CSV, placeholder photos, ZIP and PDF
scripts/              sample generator, smoke test, Windows .bat scripts
packaging/            PyInstaller spec and entry point
docs/                 operator guide, manual Windows checklist, test results, design notes
```

## Running the tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Results and the checks that could not run in the development environment are in
[docs/TEST_RESULTS.md](docs/TEST_RESULTS.md). The Outlook and packaged-app checks that
need a real Windows computer are in [docs/WINDOWS_MANUAL_CHECKLIST.md](docs/WINDOWS_MANUAL_CHECKLIST.md).

## Important limitations

- Classic Outlook automation was written against the documented Outlook object model
  but **could not be executed** in the development environment (Linux, no Outlook).
  It must be validated with the manual checklist before automatic sending is enabled.
- Scheduled work only runs while the application is running on the designated computer
  and the operator is signed in to Windows. It is not a Windows service, by design
  (Outlook COM must not run unattended as a service).
- Recipient delivery is never confirmed; the strongest confirmed state is "Confirmed in
  Sent Items".
- `.xls` (Excel 97-2003) files must be saved as `.xlsx`. HEIC photos are not supported.
  OCR is not included.
- Application permissions do not protect against a Windows administrator reading the
  data folder directly; protect the computer and backup drive accordingly.

More design details and assumptions: [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md).
Licenses: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
