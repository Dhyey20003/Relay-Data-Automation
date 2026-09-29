# Relay Employee Hub: Operator Guide

## 1. Setup

1. Unzip the packaged build to a local folder (not OneDrive) on the designated computer.
2. Double-click `RelayEmployeeHub.exe`. The browser opens automatically.
3. Create the first administrator (your work email and a password of at least 10 characters).
4. Settings > Email and Outlook: choose **Classic Outlook** (or leave **Automatic**) and
   click Save. The Tasks page shows whether Outlook is available and lists its accounts.
5. Optional: `create_desktop_shortcut.bat` (desktop icon) and `install_logon_startup.bat`
   (start in the background at Windows sign-in).
6. Settings > Backups and transfer: set the backup folder (for example an external drive)
   and click **Back up now** once to confirm it works.

Double-clicking the icon again while it is running just opens the browser. To stop it:
Settings > Network > Stop application.

**Data folder.** Default `%LOCALAPPDATA%\RelayEmployeeHub\Data`, containing `employees.db`,
`images\`, `thumbnails\`, `exports\`, `import_staging\`, `backups\`, `logs\` and
`config.json`. Change it with `RelayEmployeeHub.exe --set-data-dir <folder>` while stopped.

## 2. Employees

- The grid shows **Active** employees by default. Use the view list for Pending Review,
  Left (archived), historical-only records or everything.
- Double-click a cell (or select it and press Enter or start typing) to edit. Enter saves
  and moves down, Tab moves right, Esc cancels.
- To update many cells: copy a block from Excel, click the first cell in the grid, press
  **Ctrl+V**, review the preview, then **Apply**. Nothing is saved if any row has an error.
- If someone else changed the same record, you are shown both values and choose which to keep.
- **Columns**: choose, order and reset columns; drag a heading edge to resize.
- **Export view to Excel** exports exactly the rows and columns you are looking at.
- Click a name for the profile: all fields you are allowed to see, the photo (click to
  enlarge), replace photo, earlier photo versions, assignment/status history and the change log.
- **Archive (mark as Left)** keeps the record and its history; **Reactivate** reverses it.
- **Current bench days** = calendar days since the bench start date (0 on the start date),
  shown only while the assignment status is Bench and any bench end date has not passed.

## 3. Importing spreadsheets

1. Import Data > New import. Choose what the file is:
   - **Historical employee data**: past records. Unmatched rows become historical-only
     records (not in the active directory). Rows matching current employees only fill
     approved blank fields (Settings > General > Approved historical fields).
   - **Complete current employee roster**: defines who is Active in the chosen locations.
   - **Partial employee update**, **Designation update**, **Selected field update**:
     change matched employees only; nobody is ever deactivated.
2. Drop the `.xlsx` or `.csv` file. Pick the worksheet and header row, check the column
   mapping (or apply a saved template), choose the location scope and options.
3. **Preview changes.** Review the tabs: New records, Changes, Unchanged, Errors,
   Duplicates, Name matches to decide, Unmatched, Outside scope, Missing from roster.
4. Resolve every blocking item (skip the row, or for name matches choose Create as new or
   Skip), then **Update preview with my decisions**.
5. For a complete roster, tick only the missing employees who really left, then confirm.
6. **Confirm and import.** Download the issues report if needed.

Rules: matching is by RHC ID; the email fallback is off unless you tick it; names are
never used to merge records; blank cells never erase data unless you tick that field under
"Blank cells"; formulas are never run. Save the mapping as a template for next month.
Import history shows every import (file, hash, purpose, operator, time, result) and
offers **Undo**, which is refused if anything changed afterwards.

## 4. Photos

1. Photos > Upload and review > Start new upload. Drop images, a folder, ZIP files, PDFs,
   and optionally a mapping CSV (`filename,RHC ID`).
2. Review each card:
   - **Ready to save**: RHC ID found in the file name or given by the mapping file.
   - **Needs decision**: a name look-alike or PDF text suggestion; click **Confirm** or
     **Choose employee**. PDF photos always need a decision.
   - Flags warn about duplicates, identical-to-current photos and scanned pages.
3. PDFs: open a page to see what was extracted; drag a rectangle to crop a photo manually.
4. **Match by sequence** only when you have an ordered RHC ID list for the photos:
   arrange the photos, paste the IDs, preview every pair and tick the confirmation.
5. **Save ready photos.** Earlier photos are kept as versions. **Close batch** when done.
Missing photos lists active employees without a photo (exportable).

## 5. Tasks and exports

- **Configure** each task once: schedule, inclusion (all active employees, or only new or
  changed headshots), locations, file naming (IT), recipients, sender, subject and body.
  Keep `{dispatch_ref}` in the body so the app can find the message in Sent Items.
- Periods appear automatically when due (while the application is running). **Start period
  now** creates the current period manually.
- Open a period: **Prepare packages** (one ZIP per city plus manifests), check exclusions,
  **Preview email**, then **Create Outlook draft** or **Send now**.
- Status meanings: Draft created (in Drafts), Submitted to Outlook (handed to the Outbox),
  Confirmed in Sent Items (found by reference). Recipient delivery is never claimed.
- **Delivery Uncertain**: check Outlook's Outbox and Sent Items, then click
  **I confirmed it was sent** or **It was not sent**. The app never resends by itself.
- Without classic Outlook: **Ready folder (manual send)** gives the ZIPs and copyable
  subject/body; send from your email program, then **Mark manually completed**.
- **Automatic sending** is off until an administrator enables it per task after checking
  the recipients. Backlogs (several missed periods) and late periods are held for review.

## 6. Users and permissions

Settings > Users: add users with their email as username and a temporary password (they
must change it at first sign-in). Roles: Administrator (everything), Editor (employees,
imports, photos), Viewer (read-only; export only if granted), Export Operator (exports and
task operations). Per user: location scope, date-of-birth access (full, day and month, or
hidden), hidden fields and permission overrides. **Reset password** and **Unlock** are in
the same list. Settings > Roles and permissions changes role defaults. Integration tokens
give local automations read-only access without signing in (never includes restricted
fields). Settings > Audit log shows who did what and when.

## 7. Backup, restore and transfer

- Automatic daily backup (default 13:00, keep 30) runs while the application is running;
  **Back up now** any time. Each backup is verified after it is written.
- Restore: Settings > Backups and transfer > Restore (or drop a backup ZIP). Type RESTORE.
  A safety backup of the current data is taken first; everyone must sign in again.
- **Moving to another computer**: back up (or stop the app and copy the whole data folder),
  install the app on the new computer, restore (or `--set-data-dir` to the copied folder),
  then Settings > Email and Outlook > **Revalidate**. Automatic sending stays off until then.
  Stop using the old computer. Never run two active copies.
- Do not copy `employees.db` while the application is running; use a backup instead.
