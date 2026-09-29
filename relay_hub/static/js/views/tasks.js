// Tasks and Exports: IT/HR headshot tasks, periods, packages, email drafts/sends and completion history.
import { download, get, patch, post } from '../api.js';
import {
  busy, can, confirmDialog, copyButton, errorText, fmtBytes, fmtDate, fmtDateTime, h, loading, modal, mount, notice,
  pill, promptDialog, showError, state, statusPill, toast,
} from '../ui.js';

let root = null;
let meta = null;
let outlook = null;

export function render(container) {
  root = container;
  draw();
}

async function draw() {
  mount(root, loading('Loading tasks...'));
  try { meta = await get('/api/tasks'); } catch (e) { mount(root, notice('error', errorText(e))); return; }
  const outlookBox = h('div', { class: 'card' }, loading('Checking email automation...'));
  mount(root,
    h('div', { class: 'page-head' }, h('div', h('h1', 'Tasks and Exports'), h('div', { class: 'sub' }, `Recurring headshot packages. Times are ${state.tz}. Scheduled work runs only while this application is running on the designated computer.`))),
    outlookBox,
    h('div', { class: 'spacer' }),
    h('div', { class: 'stack' }, meta.tasks.map(taskCard)));
  loadOutlook(outlookBox);
}

async function loadOutlook(box, fresh = false) {
  if (fresh || !outlook) {
    mount(box, loading('Checking email automation...'));
    try { outlook = await get('/api/outlook/status'); } catch (e) { mount(box, notice('error', errorText(e))); return; }
  }
  const o = outlook;
  mount(box,
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Email: ', o.adapter_label, ' ', o.available ? pill('available', 'green') : pill('not available', 'amber')),
      h('button', { class: 'small', onclick: () => loadOutlook(box, true) }, 'Check again')),
    h('p', { class: o.available ? 'muted' : '' }, o.message),
    o.adapter === 'manual' ? notice('info', 'Packages can still be prepared. Use "Ready folder" on a period to get the ZIP files with a copyable subject and body, attach them in your email program, then mark the period as manually completed. A mailto link cannot attach files, so none is offered.') : null,
    o.adapter === 'dry_run' ? notice('warn', h('b', 'Dry run: '), 'emails are written to the exports\\dry_run_mail folder and never sent. Switch to Outlook in Settings > Email for real use.') : null,
    o.accounts && o.accounts.length ? h('p', { class: 'small muted' }, 'Accounts in this Outlook profile: ', o.accounts.map((a) => `${a.name}${a.smtp && a.smtp !== a.name ? ` <${a.smtp}>` : ''}`).join(', ')) : null,
    h('p', { class: 'small faint' }, 'Statuses mean: Draft created (in Outlook Drafts) → Submitted to Outlook (handed to the Outbox) → Confirmed in Sent Items. Delivery to recipients cannot be confirmed by this application.'));
}

function scheduleText(t) {
  const s = t.schedule;
  if (s.type === 'interval') return `Every ${s.interval_days} days from ${fmtDate(s.anchor_date)} at ${s.time}`;
  return `Monthly on day ${s.day} at ${s.time}${s.day > 28 ? ' (last day of shorter months)' : ''}`;
}

function taskCard(t) {
  const periodsBox = h('div');
  const openP = t.open_periods;
  const recent = t.recent_periods.filter((p) => !openP.some((o) => o.id === p.id));
  const periodRow = (p) => h('tr', { class: 'clickable', onclick: () => openPeriod(p.id) },
    h('td', p.period_label), h('td', fmtDateTime(p.due_at), p.overdue && !['Confirmed in Sent Items', 'Manually Completed'].includes(p.status) ? h('span', ' ', pill('overdue', 'red')) : null),
    h('td', statusPill(p.status)), h('td', { class: 'small muted' }, p.status_note || ''));
  mount(periodsBox, (openP.length || recent.length) ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' },
    h('thead', h('tr', h('th', 'Period'), h('th', 'Due'), h('th', 'Status'), h('th', 'Details'))),
    h('tbody', openP.map(periodRow), recent.map(periodRow)))) : h('p', { class: 'muted' }, 'No periods yet. They appear when the schedule becomes due, or use "Start period now".'));
  const inclusion = t.inclusion === 'all' ? 'All active employees' : t.inclusion === 'changed' ? 'Only new or changed headshots since the last completed submission' : null;
  const autoState = t.auto_send_enabled ? (t.auto_send_blockers.length ? pill('automatic sending paused', 'amber') : pill('automatic sending on', 'purple')) : pill('manual sending');
  return h('div', { class: 'task-card' },
    h('div', { class: 'title' }, h('h2', t.name), t.enabled ? pill('scheduled', 'green') : pill('schedule off'), autoState, h('div', { class: 'grow' }),
      can('tasks.operate') ? h('button', { onclick: (e) => busy(e.currentTarget, async () => { try { const r = await post(`/api/tasks/${t.id}/start-period`); openPeriod(r.period_id); draw(); } catch (err) { showError(err); } }) }, 'Start period now') : null,
      can('tasks.configure') ? h('button', { onclick: () => openAutoSend(t) }, t.auto_send_enabled ? 'Automatic sending...' : 'Enable automatic sending...') : null,
      can('tasks.configure') ? h('button', { class: 'primary', onclick: () => openConfig(t) }, 'Configure') : null),
    !inclusion ? notice('warn', 'Choose and save an inclusion rule (all active employees, or only new/changed headshots) in Configure before this task can run.') : null,
    h('dl', { class: 'kv' },
      h('dt', 'Schedule'), h('dd', scheduleText(t)),
      h('dt', 'Next due'), h('dd', t.upcoming.length ? t.upcoming.map((u) => fmtDateTime(u.due_at)).join(' · ') : 'none'),
      h('dt', 'Include'), h('dd', inclusion || h('span', { class: 'field-error' }, 'not chosen yet')),
      h('dt', 'Locations'), h('dd', t.locations.join(', ') + ' (one ZIP per location, both attached to one email)'),
      h('dt', 'File names'), h('dd', t.kind === 'HR' ? 'RHC ID, converted to JPG (for example RHC1234.jpg)' : `${t.naming_template}${t.export_format === 'jpg' ? ' (converted to JPG)' : ' (original format)'}; duplicates get _<RHC ID>`),
      h('dt', 'Recipients'), h('dd', t.email.to && t.email.to.length ? `To: ${t.email.to.join(', ')}${t.email.cc && t.email.cc.length ? ` · CC: ${t.email.cc.join(', ')}` : ''}` : h('span', { class: 'field-error' }, 'none configured')),
      h('dt', 'Last completed'), h('dd', t.last_completed ? `${t.last_completed.period_label} (${t.last_completed.status})` : 'none'),
      t.auto_send_enabled && t.auto_send_blockers.length ? [h('dt', 'Automatic sending'), h('dd', { class: 'field-error' }, t.auto_send_blockers.join(' '))] : null),
    h('h3', 'Periods'), periodsBox);
}

// ------------------------------------------------------------------ config

function openConfig(t) {
  const s = t.schedule;
  const name = h('input', { type: 'text', value: t.name });
  const enabled = h('input', { type: 'checkbox', checked: !!t.enabled });
  const anchor = h('input', { type: 'date', value: s.anchor_date || '' });
  const interval = h('input', { type: 'number', min: 1, max: 365, value: s.interval_days || 14 });
  const day = h('input', { type: 'number', min: 1, max: 31, value: s.day || 1 });
  const time = h('input', { type: 'time', value: s.time || '10:00' });
  const incAll = h('input', { type: 'radio', name: 'inc', checked: t.inclusion === 'all' });
  const incChanged = h('input', { type: 'radio', name: 'inc', checked: t.inclusion === 'changed' });
  const naming = h('input', { type: 'text', value: t.naming_template || '{original_filename}' });
  const fmt = h('select', {}, h('option', { value: 'original' }, 'Keep original format'), h('option', { value: 'jpg' }, 'Convert to JPG'));
  fmt.value = t.export_format;
  const locBoxes = state.me.all_locations.map((l) => h('label', { class: 'field inline' }, h('input', { type: 'checkbox', value: l, checked: t.locations.includes(l) }), l));
  const to = h('input', { type: 'text', value: (t.email.to || []).join('; '), placeholder: 'name@company.com; other@company.com' });
  const cc = h('input', { type: 'text', value: (t.email.cc || []).join('; ') });
  const sender = h('input', { type: 'text', value: t.email.sender || '', placeholder: 'Leave blank to use the default Outlook account', list: 'sender-accounts' });
  const accounts = h('datalist', { id: 'sender-accounts' }, (outlook && outlook.accounts || []).map((a) => h('option', { value: a.smtp || a.name })));
  const subject = h('input', { type: 'text', value: t.email.subject_template || '' });
  const body = h('textarea', { value: t.email.body_template || '', style: { minHeight: '200px', fontFamily: 'var(--mono)', fontSize: '12.5px' } });
  const maxMb = h('input', { type: 'number', min: 1, max: 150, step: 'any', value: t.max_attachment_mb });
  const grace = h('input', { type: 'number', min: 1, max: 720, value: t.catchup_grace_hours });
  const skipEmpty = h('input', { type: 'checkbox', checked: !!t.skip_when_empty });
  const withOmissions = h('input', { type: 'checkbox', checked: !!t.auto_send_with_omissions });
  const err = h('div');
  const save = h('button', { class: 'primary' }, 'Save');
  const m = modal({ title: `Configure: ${t.name}`, size: 'wide', body: h('div', { class: 'stack' }, err,
    h('fieldset', h('legend', 'Schedule'), h('div', { class: 'grid2' },
      h('label', { class: 'field' }, 'Task name', name),
      t.kind === 'IT' ? h('label', { class: 'field' }, 'Anchor date (first due date)', anchor) : h('label', { class: 'field' }, 'Day of month (29-31 use the last day of shorter months)', day),
      t.kind === 'IT' ? h('label', { class: 'field' }, 'Repeat every (days)', interval) : null,
      h('label', { class: 'field' }, `Time (${state.tz})`, time)),
      h('label', { class: 'field inline' }, enabled, 'Schedule is on (periods are created automatically when due)')),
    h('fieldset', h('legend', 'Which employees to include (required)'),
      h('label', { class: 'field inline' }, incAll, 'All active employees'),
      h('label', { class: 'field inline' }, incChanged, 'Only new or changed headshots since the last completed submission (tracks photo version and location per employee)')),
    h('fieldset', h('legend', 'Files'),
      t.kind === 'IT' ? h('div', { class: 'grid2' },
        h('label', { class: 'field' }, 'File name template', naming, h('span', { class: 'small faint' }, `Placeholders: ${meta.naming_placeholders.map((p) => `{${p}}`).join(' ')}`)),
        h('label', { class: 'field' }, 'Image format', fmt)) : h('p', { class: 'muted' }, 'HR files are always named by RHC ID and converted to real JPG images (for example RHC1234.jpg).'),
      h('div', h('b', 'Locations '), h('div', { class: 'row' }, locBoxes))),
    h('fieldset', h('legend', 'Email'), accounts,
      h('div', { class: 'grid2' }, h('label', { class: 'field' }, 'To (required for sending)', to), h('label', { class: 'field' }, 'CC', cc),
        h('label', { class: 'field' }, 'Send from account', sender), h('label', { class: 'field' }, 'Attachment size limit (MB, both ZIPs together)', maxMb)),
      h('label', { class: 'field' }, 'Subject template', subject),
      h('label', { class: 'field' }, 'Body template', body),
      h('p', { class: 'small faint' }, `Placeholders: ${meta.template_fields.map((f) => `{${f}}`).join(' ')}. Keep {dispatch_ref} in the body or subject; it is how the application finds the message in Sent Items.`),
      h('p', { class: 'small muted' }, 'Changing recipients or the sender switches automatic sending to "needs re-confirmation".')),
    h('fieldset', h('legend', 'Automation safeguards'),
      h('label', { class: 'field inline' }, skipEmpty, 'If nothing is new or changed, close the period automatically without sending'),
      h('label', { class: 'field inline' }, withOmissions, 'Allow automatic sending even when some employees are excluded (not recommended)'),
      h('label', { class: 'field' }, 'Catch-up window (hours): after this, an overdue period is held for review instead of being sent automatically', grace))),
  actions: [h('button', { onclick: () => m.close() }, 'Cancel'), save] });
  save.addEventListener('click', () => busy(save, async () => {
    const payload = {
      version: t.version, name: name.value, enabled: enabled.checked,
      schedule: t.kind === 'IT' ? { anchor_date: anchor.value, interval_days: Number(interval.value), time: time.value } : { day: Number(day.value), time: time.value },
      locations: locBoxes.map((l) => l.querySelector('input')).filter((i) => i.checked).map((i) => i.value),
      email: { to: to.value, cc: cc.value, sender: sender.value, subject_template: subject.value, body_template: body.value },
      max_attachment_mb: Number(maxMb.value), catchup_grace_hours: Number(grace.value), skip_when_empty: skipEmpty.checked, auto_send_with_omissions: withOmissions.checked,
    };
    if (incAll.checked) payload.inclusion = 'all';
    else if (incChanged.checked) payload.inclusion = 'changed';
    if (t.kind === 'IT') { payload.naming_template = naming.value; payload.export_format = fmt.value; }
    try { await patch(`/api/tasks/${t.id}`, payload); toast('Task saved.', 'ok'); m.close(); draw(); } catch (e) { mount(err, notice('error', errorText(e))); }
  }));
}

function openAutoSend(t) {
  const confirmBox = h('input', { type: 'checkbox' });
  const on = !!t.auto_send_enabled;
  const btnOn = h('button', { class: 'accent' }, on ? 'Re-confirm automatic sending' : 'Enable automatic sending');
  const btnOff = on ? h('button', { class: 'danger' }, 'Turn off automatic sending') : null;
  const m = modal({ title: `Automatic sending: ${t.name}`, body: h('div', { class: 'stack' },
    h('p', 'When on, each due period is prepared and sent through classic Outlook without asking, as long as:'),
    h('ul', h('li', 'the application is running on this computer and you are signed in to Windows;'), h('li', 'the packages are complete (unless you allowed exclusions) and within the size limit;'),
      h('li', 'the period is not a backlog (several missed periods are always held for review);'), h('li', 'no earlier send for the period is uncertain (uncertain sends are never retried).')),
    h('dl', { class: 'kv' }, h('dt', 'To'), h('dd', (t.email.to || []).join(', ') || h('span', { class: 'field-error' }, 'no recipients')), h('dt', 'CC'), h('dd', (t.email.cc || []).join(', ') || 'none'),
      h('dt', 'From'), h('dd', t.email.sender || 'default Outlook account')),
    h('label', { class: 'field inline' }, confirmBox, 'I have checked the recipients and sender, and routine emails for this task may be sent automatically.')),
  actions: [h('button', { onclick: () => m.close() }, 'Cancel'), btnOff, btnOn].filter(Boolean) });
  btnOn.addEventListener('click', () => busy(btnOn, async () => {
    try { await post(`/api/tasks/${t.id}/auto-send`, { enable: true, confirm: confirmBox.checked }); toast('Automatic sending is on.', 'ok'); m.close(); draw(); } catch (e) { showError(e); }
  }));
  if (btnOff) btnOff.addEventListener('click', () => busy(btnOff, async () => {
    try { await post(`/api/tasks/${t.id}/auto-send`, { enable: false }); toast('Automatic sending is off.', 'ok'); m.close(); draw(); } catch (e) { showError(e); }
  }));
}

// ------------------------------------------------------------------ period

async function openPeriod(id) {
  const m = modal({ title: 'Task period', size: 'xwide', body: loading(), onClose: () => draw() });
  const reload = async () => {
    let d;
    try { d = await get(`/api/periods/${id}`); } catch (e) { mount(m.body, notice('error', errorText(e))); return; }
    m.box.querySelector('.modal-head h2').textContent = `${d.task.name} · ${d.period.period_label}`;
    mount(m.body, periodBody(d, reload));
  };
  reload();
}

function periodBody(d, reload) {
  const p = d.period;
  const current = d.packages.filter((x) => x.current);
  const older = d.packages.filter((x) => !x.current);
  const active = d.active_dispatch;
  const closed = ['Confirmed in Sent Items', 'Manually Completed'].includes(p.status);
  const incomplete = current.some((x) => x.omitted_count);
  const canOp = can('tasks.operate');
  const act = (label, cls, fn) => h('button', { class: cls, onclick: (e) => busy(e.currentTarget, async () => { try { await fn(); await reload(); } catch (err) { showError(err); } }) }, label);
  const actions = h('div', { class: 'row' });
  if (canOp && !closed && !active && !['Submitted to Outlook', 'Delivery Uncertain'].includes(p.status)) {
    actions.appendChild(act(current.length ? 'Regenerate packages' : 'Prepare packages', current.length ? '' : 'primary', async () => {
      if (current.length && !await confirmDialog('Regenerate packages', 'Create a new revision of every location package from the current data? The existing revision is kept in the history.')) return;
      const r = await post(`/api/periods/${p.id}/prepare`);
      toast(`Packages prepared: ${r.status}.`, 'ok');
    }));
  }
  if (current.length) actions.appendChild(h('button', { onclick: () => emailPreview(p.id) }, 'Preview email'));
  if (canOp && current.length && !closed && !active) {
    const allow = () => (incomplete ? confirmDialog('Incomplete package', 'Some employees were excluded (see the exclusions below). Continue with an incomplete package?', { confirmText: 'Continue anyway' }) : true);
    actions.appendChild(act('Create Outlook draft', '', async () => {
      if (!await allow()) return;
      const r = await post(`/api/periods/${p.id}/draft`, { allow_incomplete: incomplete });
      toast(r.status === 'draft_created' ? 'Draft created in Outlook with the ZIP files attached.' : `Draft result: ${r.status}. ${r.error || ''}`, r.status === 'draft_created' ? 'ok' : 'error');
    }));
    actions.appendChild(act('Send now', 'accent', async () => {
      if (!await confirmDialog('Send now', 'Send the email with the attached ZIP files through Outlook now?', { confirmText: 'Send now', extra: incomplete ? notice('warn', 'This package is incomplete: some employees were excluded.') : null })) return;
      const r = await post(`/api/periods/${p.id}/send`, { confirm: true, allow_incomplete: incomplete });
      toast({ submitted: 'Handed to Outlook. It will be confirmed once it appears in Sent Items.', failed: `Not sent: ${r.error}`, uncertain: `Result uncertain: ${r.error}` }[r.status] || r.status, r.status === 'submitted' ? 'ok' : 'error');
    }));
    actions.appendChild(act('Ready folder (manual send)', '', async () => {
      const r = await post(`/api/periods/${p.id}/ready-folder`);
      readyFolderDialog(p.id, r);
    }));
  }
  if (canOp && !closed && !active) {
    actions.appendChild(act('Mark manually completed', '', async () => {
      const note = await promptDialog('Mark manually completed', 'How was it completed?', { multiline: true, required: true, help: 'For example: "Sent from Outlook on the web by Priya at 10:15" or "Skipped: superseded by the next period". This is recorded in the audit log.' });
      if (!note) return;
      const sent = current.length ? await confirmDialog('Were the prepared packages sent?', 'Choose "Yes, they were sent" if the current ZIP files were delivered. This updates the "new or changed" baseline. Choose Cancel if nothing was sent (for example a skipped period).', { confirmText: 'Yes, they were sent' }) : false;
      await post(`/api/periods/${p.id}/manual-complete`, { note, packages_sent: !!sent });
      toast('Period marked as manually completed.', 'ok');
    }));
  }
  const dispatchRows = d.dispatches.map((x) => {
    const btns = h('div', { class: 'row' });
    if (canOp && ['submitted', 'draft_created', 'uncertain'].includes(x.status)) {
      btns.appendChild(act('Check Sent Items', 'small', async () => { const r = await post(`/api/dispatches/${x.id}/reconcile`); toast(`Outlook check: ${r.map((y) => y.result).join(', ') || 'no change'}.`); }));
      btns.appendChild(act('I confirmed it was sent', 'small', async () => {
        const note = await promptDialog('Confirm sent', 'How did you check?', { required: true, help: 'For example "Seen in Sent Items at 10:04 with both attachments".' });
        if (note) await post(`/api/dispatches/${x.id}/resolve`, { action: 'confirm_sent', note });
      }));
    }
    if (canOp && ['uncertain', 'failed'].includes(x.status)) {
      btns.appendChild(act('It was not sent', 'small danger', async () => {
        const note = await promptDialog('Mark as not sent', 'How did you check that it was not sent?', { required: true, help: 'Only do this after checking Outlook\'s Outbox and Sent Items. A new send will then be allowed.' });
        if (note) await post(`/api/dispatches/${x.id}/resolve`, { action: 'not_sent', note });
      }));
    }
    if (canOp && x.status === 'draft_created') {
      btns.appendChild(act('Discard draft', 'small danger', async () => {
        if (!await confirmDialog('Discard draft', 'Remove this draft record (and delete the draft from Outlook if it is still there) so a new email can be created?', { danger: true, confirmText: 'Discard draft' })) return;
        await post(`/api/dispatches/${x.id}/resolve`, { action: 'discard_draft', note: '' });
      }));
    }
    const label = { sending: 'Sending...', draft_created: 'Draft created', submitted: 'Submitted to Outlook', confirmed: 'Confirmed in Sent Items', failed: 'Failed', uncertain: 'Delivery uncertain', manual: 'Manually completed', abandoned: 'Abandoned / not sent' }[x.status] || x.status;
    return h('tr', h('td', h('span', { class: 'mono' }, x.dispatch_ref), x.automatic ? h('div', pill('automatic', 'purple')) : null), h('td', `${x.mode} via ${x.adapter}`),
      h('td', pill(label, { confirmed: 'green', submitted: 'purple', failed: 'red', uncertain: 'red', draft_created: 'blue' }[x.status] || '')),
      h('td', { class: 'small' }, (x.to_addrs || []).join(', ')), h('td', { class: 'small' }, (x.attachments || []).map((a) => `${a.name} (${fmtBytes(a.bytes)})`).join(', ')),
      h('td', { class: 'small' }, `${fmtDateTime(x.created_at)} by ${x.created_by || ''}`, x.confirmed_at ? h('div', `confirmed ${fmtDateTime(x.confirmed_at)}`) : null),
      h('td', { class: 'small' }, x.error ? h('div', { class: 'field-error' }, x.error) : null, x.notes || ''), h('td', btns));
  });
  return h('div', { class: 'stack' },
    h('div', { class: 'row' }, statusPill(p.status), h('span', { class: 'muted' }, `Due ${fmtDateTime(p.due_at)}`), p.completed_at ? h('span', { class: 'muted' }, `· completed ${fmtDateTime(p.completed_at)} by ${p.completed_by || ''}`) : null),
    p.status_note ? notice(['Failed', 'Delivery Uncertain'].includes(p.status) ? 'error' : p.status === 'Needs Attention' ? 'warn' : 'info', p.status_note) : null,
    active && active.status === 'uncertain' ? notice('error', h('b', 'Delivery uncertain. '), 'Check Outlook\'s Outbox and Sent Items for reference ', h('span', { class: 'mono' }, active.dispatch_ref), '. The application will not resend automatically. Use the buttons in the email history below once you know what happened.') : null,
    actions,
    h('h3', 'Packages'),
    current.length ? h('div', { class: 'pkg-grid' }, current.map((pk) => packageCard(pk))) : h('p', { class: 'muted' }, 'No packages prepared yet.'),
    older.length ? h('details', h('summary', `Earlier revisions (${older.length})`), h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Location'), h('th', 'Revision'), h('th', 'Photos'), h('th', 'Excluded'), h('th', 'Prepared'), h('th', ''))),
      h('tbody', older.map((pk) => h('tr', h('td', pk.location), h('td', `r${pk.revision}`), h('td', pk.included_count), h('td', pk.omitted_count), h('td', `${fmtDateTime(pk.created_at)} by ${pk.created_by}`),
        h('td', pk.allowed ? h('button', { class: 'small', onclick: () => download(`/api/packages/${pk.id}/manifest`, undefined, 'manifest.json').catch(showError) }, 'Manifest') : null))))))) : null,
    h('h3', 'Email history'),
    d.dispatches.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Reference'), h('th', 'Mode'), h('th', 'Status'), h('th', 'To'), h('th', 'Attachments'), h('th', 'When'), h('th', 'Notes'), h('th', ''))), h('tbody', dispatchRows))) : h('p', { class: 'muted' }, 'No email created yet.'));
}

function packageCard(pk) {
  const canDl = pk.allowed && (can('tasks.operate') || can('employees.export'));
  return h('div', { class: 'card flat' },
    h('div', { class: 'row' }, h('h3', { class: 'grow', style: { margin: 0 } }, pk.location), pill(`revision ${pk.revision}`), pk.is_complete ? pill('complete', 'green') : pill('incomplete', 'amber')),
    pk.problem ? notice('error', pk.problem) : null,
    h('dl', { class: 'kv' },
      h('dt', 'ZIP file'), h('dd', pk.included_count ? `${pk.zip_name} (${fmtBytes(pk.zip_size)})` : 'No ZIP (nothing to include)'),
      h('dt', 'Photos included'), h('dd', pk.included_count),
      h('dt', 'Excluded'), h('dd', pk.omitted_count),
      pk.inclusion === 'changed' ? [h('dt', 'Unchanged (not included)'), h('dd', pk.unchanged)] : null,
      h('dt', 'Prepared'), h('dd', `${fmtDateTime(pk.created_at)} by ${pk.created_by}`),
      h('dt', 'SHA-256'), h('dd', { class: 'mono small' }, pk.zip_sha256 ? pk.zip_sha256.slice(0, 24) + '…' : 'none')),
    pk.exclusions.length ? h('details', { open: pk.exclusions.length <= 5 }, h('summary', h('b', `Exclusions (${pk.exclusions.length})`)),
      h('ul', { class: 'change-list' }, pk.exclusions.map((x) => h('li', `${x.full_name} (${x.rhc_id || 'no RHC ID'}): ${x.reason}`)))) : null,
    pk.renamed.length ? h('details', h('summary', `Duplicate file names resolved (${pk.renamed.length})`), h('ul', { class: 'change-list' }, pk.renamed.map((x) => h('li', `${x.renamed_from} → ${x.exported_filename}`)))) : null,
    canDl ? h('div', { class: 'row' },
      pk.included_count ? h('button', { class: 'small', onclick: (e) => busy(e.currentTarget, () => download(`/api/packages/${pk.id}/zip`, undefined, pk.zip_name).catch(showError)) }, 'Download ZIP') : null,
      h('button', { class: 'small', onclick: () => download(`/api/packages/${pk.id}/manifest`, undefined, 'manifest.json').catch(showError) }, 'Manifest (JSON)'),
      h('button', { class: 'small', onclick: () => download(`/api/packages/${pk.id}/manifest?fmt=csv`, undefined, 'manifest.csv').catch(showError) }, 'Manifest (CSV)')) : null);
}

async function emailPreview(periodId) {
  let e;
  try { e = await get(`/api/periods/${periodId}/email-preview`); } catch (err) { showError(err); return; }
  modal({ title: 'Email preview', size: 'wide', body: h('div', { class: 'stack' },
    e.problems.length ? notice('warn', h('ul', e.problems.map((p) => h('li', p)))) : notice('ok', 'Ready to create a draft or send.'),
    h('dl', { class: 'kv' }, h('dt', 'From'), h('dd', e.sender || 'default Outlook account'), h('dt', 'To'), h('dd', e.to.join('; ') || 'none'), h('dt', 'CC'), h('dd', e.cc.join('; ') || 'none'),
      h('dt', 'Subject'), h('dd', e.subject), h('dt', 'Attachments'), h('dd', e.attachments.map((a) => `${a.name} (${fmtBytes(a.bytes)})`).join(', ') || 'none'),
      h('dt', 'Total size'), h('dd', `${fmtBytes(e.total_bytes)} (limit ${e.limit_mb} MB)`)),
    h('div', { class: 'copybox' }, e.body),
    h('p', { class: 'small faint' }, 'The reference number is filled in when the draft or email is created.')) });
}

function readyFolderDialog(periodId, r) {
  modal({ title: 'Ready folder for manual sending', size: 'wide', body: h('div', { class: 'stack' },
    notice('info', 'The ZIP files and email text were copied to the folder below. Attach the ZIP files in your email program (new Outlook, Outlook on the web or classic Outlook), send it, then use "Mark manually completed".'),
    h('div', { class: 'row' }, h('code', { class: 'grow' }, r.folder), copyButton(() => r.folder, 'Copy path'),
      h('button', { class: 'small', onclick: async () => { try { const x = await post(`/api/periods/${periodId}/open-folder`); if (!x.opened) toast('Open this folder in File Explorer on the application computer.'); } catch (e) { showError(e); } } }, 'Open folder')),
    h('dl', { class: 'kv' }, h('dt', 'To'), h('dd', r.to.join('; ') || 'none'), h('dt', 'CC'), h('dd', r.cc.join('; ') || 'none'), h('dt', 'Files'), h('dd', r.files.join(', '))),
    h('div', { class: 'row' }, h('b', { class: 'grow' }, 'Subject'), copyButton(() => r.subject, 'Copy subject')), h('div', { class: 'copybox' }, r.subject),
    h('div', { class: 'row' }, h('b', { class: 'grow' }, 'Body'), copyButton(() => r.body, 'Copy body')), h('div', { class: 'copybox' }, r.body)) });
}
