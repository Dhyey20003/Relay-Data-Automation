# Manual Windows validation checklist (Outlook and packaged app)

These checks need a real Windows 10/11 computer with classic Outlook and could **not** be
run in the development environment (Linux container, no Outlook, no Windows desktop).
Use synthetic data (the `samples\` folder) and test recipients you control. Record the
date, Windows/Outlook versions and the tester's name for each item.

## A. Packaged application

- [ ] Unzip `RelayEmployeeHub-windows.zip` to a local folder; double-click `RelayEmployeeHub.exe`.
      Browser opens at `http://127.0.0.1:8765`; no console window stays open.
- [ ] Disconnect from the network (Wi-Fi off). Restart the app. Setup, sign-in, all five
      sections, imports, photo upload (ZIP and PDF), package preparation and backups still work.
- [ ] Double-click the exe again while running: it only opens the browser (no second copy).
- [ ] `create_desktop_shortcut.bat` creates a working desktop icon (also with a
      OneDrive-redirected Desktop).
- [ ] `install_logon_startup.bat`, sign out and in: the app starts in the background
      (browser not opened); `remove_logon_startup.bat` removes it.
- [ ] Settings > Network > Stop application stops the server; the exe restarts it.
- [ ] `--set-data-dir` into a OneDrive folder is refused; a local folder is accepted.
- [ ] Windows Defender / SmartScreen: note any warning for the unsigned exe (code signing is
      recommended for company rollout).
- [ ] Logs are written to `<data>\logs\relay_hub.log`.

## B. Classic Outlook availability and accounts

- [ ] With classic Outlook installed and a profile configured: Tasks page shows
      "Classic Outlook ... is available" and lists the accounts.
- [ ] Outlook closed before the check: it is started in the background and the check succeeds.
- [ ] With only the new Outlook for Windows (or classic uninstalled): Email shows
      "not available" with the explanation; packages, ready folder and manual completion work.
- [ ] Outlook showing a profile-selection or sign-in dialog: the check reports that Outlook
      did not respond (no hang of the whole app).
- [ ] App and Outlook started at different elevation (one "as administrator"): a clear error
      is shown.

## C. Drafts and sending (use test recipients only)

- [ ] Configure the IT task with a test To/CC address; choose a non-default sender account.
- [ ] Prepare packages; **Preview email** shows both city ZIPs and sizes.
- [ ] **Create Outlook draft**: draft appears in Drafts of the chosen account, with both
      ZIP files attached, correct To/CC/subject/body and reference number.
- [ ] Send the draft from Outlook; **Check Sent Items** (or wait for the 5-minute check)
      changes the period to "Confirmed in Sent Items".
- [ ] **Discard draft** removes the draft from Outlook and allows a new email.
- [ ] **Send now**: status "Submitted to Outlook", then "Confirmed in Sent Items".
- [ ] Offline Outlook (Work Offline): message stays in the Outbox; after the configured
      timeout the period becomes "Delivery Uncertain"; nothing is resent automatically.
- [ ] Attachment size above the task limit: sending is refused with a clear message.
- [ ] Invalid sender account in the task: clear "account not in profile" error, status Failed.
- [ ] If an Outlook security prompt appears (Programmatic Access), answering **Deny** gives
      Failed (not sent); leaving it unanswered past the timeout gives Delivery Uncertain.
      The application never bypasses the prompt.
- [ ] Company policy blocking programmatic send: clear error; manual path still works.

## D. Scheduled automatic sending

- [ ] Enable a task schedule a few minutes ahead; enable automatic sending (confirmation
      required). At the due time the period is prepared and sent once.
- [ ] Close the app across a due time, reopen within the catch-up window: it is processed;
      reopen after the window or after several missed periods: they are held as Needs Attention.
- [ ] Kill the app (Task Manager) during a send: on restart the period is Delivery Uncertain.
- [ ] Restore a backup: automatic sending pauses until Settings > Email and Outlook > Revalidate.
- [ ] Copy the data folder to a second computer and start there: transfer is detected and
      automatic sending pauses; starting while the first copy is still running on the same
      folder is refused.

## E. LAN access (only if it will be used)

- [ ] Default: another PC cannot connect.
- [ ] Enable LAN access, list the computer name/IP, allow the port in Windows Firewall for the
      Private/Domain profile only, restart. Another PC can sign in; unlisted host names are refused.
- [ ] Optional TLS certificate configured: HTTPS works.
