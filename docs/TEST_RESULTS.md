# Test results

Environment: Linux container (Python 3.11.15, headless Chromium via Playwright), 30 Sep 2026.
All data synthetic; email used a mock adapter or the dry-run adapter. No real email was sent.

## 1. Automated test suite (`python -m pytest -q`): 64 passed, 0 failed

| Acceptance area | Tests |
|---|---|
| Employee CRUD, inline editing (PATCH with version), archive/reactivate, history | `test_employees.py::test_create_read_update_archive`, `test_version_conflict_prevents_silent_overwrite`, `test_bench_days_in_api_and_assignment_history` |
| Leading-zero IDs | `test_leading_zeros_preserved`, `test_imports.py::test_leading_zeros_from_csv_and_text_xlsx` |
| Validation, bench days, search/sort/filter/pagination, paste, export + formula injection | remaining `test_employees.py` tests |
| Custom fields (forms, imports, filters, exports, tokens) | `test_security.py::test_custom_fields_everywhere` |
| Role, location and field permissions; DOB restrictions; tokens; CSRF/origin/host; lockout; password reset; audit | `test_security.py` |
| Duplicate and ambiguous imports, email fallback, name suggestions | `test_imports.py` (duplicates, email fallback, name match) |
| Historical data then current roster, enrichment | `test_historical_then_current_roster` |
| Partial updates without deactivation, blank-cell handling | `test_partial_update_never_deactivates_and_blank_cells_do_not_erase` |
| Roster deactivations need explicit confirmation | `test_roster_missing_employees_need_explicit_confirmation` |
| Import rollback/conflicts, repeat detection, stale preview | `test_undo_and_undo_conflicts`, `test_repeat_import_detected`, `test_commit_refused_if_records_change_after_preview` |
| Formulas not executed, scope, header rows, templates, error report, date parsing | remaining `test_imports.py` tests |
| Image matching and manual review, mapping CSV, sequence mapping, duplicates | `test_photos.py` |
| Safe ZIP extraction (path traversal, compression ratio, nested/non-image) | `test_zip_safety` |
| PDF extraction and cropping, scanned-page flag | `test_pdf_extraction_is_reviewed_and_crop_works` |
| Correct city ZIP contents, IT/HR filename rules, real JPG conversion, duplicate names | `test_tasks.py` (city zips, HR package, IT jpg option) |
| Missing-photo reporting and incomplete labelling | `test_city_zips_names_and_missing_photos`, `test_replace_photo_keeps_versions_and_missing_report` |
| Immutable packages and revisions | `test_packages_are_immutable_snapshots_with_revisions` |
| Changed-photo baseline (photo version and location), no advance before confirmation | `test_send_then_reconcile_advances_baseline_and_changed_mode`, `test_failed_and_uncertain_sends` |
| Duplicate and uncertain email handling (mocks), concurrency lock | `test_failed_and_uncertain_sends`, `test_duplicate_send_prevention_under_concurrency` |
| Drafts, discard, ready folder, manual completion | `test_draft_discard_manual_completion_and_ready_folder` |
| Restart-safe schedules, catch-up, backlog hold, monthly end-of-month rule, timezone | `test_schedule_math`, `test_scheduler_auto_sends_once_enabled_and_is_restart_safe`, `test_backlog_and_overdue_are_held_for_review`, `test_restart_recovery` |
| Full backup/restore with photos, unsafe/damaged archives, retention, destination rules | `test_backup_offline.py` |
| Offline operation, all assets local, CSP | `test_all_frontend_assets_are_local` |
| Instance lock, computer transfer, sync-folder refusal, LAN off by default, pre-migration backup | `test_system.py` |

## 2. Browser end-to-end checks (Playwright + Chromium against the running server)

Passed with no JavaScript errors: first-run setup; sign-in; switching email to dry run;
historical import then complete roster import (header row 4) through the UI wizard;
inline cell edit; profile; photo upload of ZIP + PDF + mapping CSV, confirming a suggestion
and saving; manual PDF crop; task configuration; start period; prepare packages; dry-run
Send Now (status "Submitted to Outlook"); every Settings tab including adding a user and a
custom field and creating a verified backup; import history undo refused with a conflict
list; missing photos list.

## 3. Packaged build

- PyInstaller 6.22.3 build of `packaging/relay_hub.spec` on **Linux**: builds; the frozen
  app starts, serves the UI, processes PDFs and XLSX, and a second launch detects the
  running copy. `scripts/smoke_test_server.py` passed against it (roster import of 50,
  6 photos saved, dry-run HR send, verified backup).
- **Windows** build (GitHub Actions `windows-latest`, Python 3.12.10, runs
  [#1](https://github.com/Dhyey20003/Relay-Data-Automation/actions/runs/36614217738) and
  [#2](https://github.com/Dhyey20003/Relay-Data-Automation/actions/runs/36614349299), both successful):
  the automated test suite passed on Windows; PyInstaller built `RelayEmployeeHub.exe`
  (pywin32/pythoncom hooks included); the packaged exe was started with a fresh data folder
  and `scripts/smoke_test_server.py` passed against it ("SMOKE TEST PASSED: imported 50,
  saved 6 photos, dry-run send ..., backup ..."). On that runner (no Outlook installed) the
  Outlook check correctly reported: "Classic Outlook is not installed on this computer ...".
  The zipped build is uploaded as the `RelayEmployeeHub-windows` artifact (kept 30 days).
  The runner has no interactive desktop, so the browser launch, desktop shortcut and logon-startup
  behaviour were not exercised there.

## 4. Not run in this environment (see WINDOWS_MANUAL_CHECKLIST.md)

- Classic Outlook COM automation (availability check, accounts, drafts, Send, Sent Items
  reconciliation, security prompts, policy restrictions): **not executed**; no Windows or
  Outlook available. Covered only by mock-adapter tests of the surrounding logic.
- Desktop shortcut and logon-startup creation (PowerShell / WScript.Shell).
- Windows-specific file behaviour (read-only package attribute, Explorer "Open folder").
- LAN access from a second computer and TLS.
- Behaviour under Windows Defender / SmartScreen for the unsigned executable.
