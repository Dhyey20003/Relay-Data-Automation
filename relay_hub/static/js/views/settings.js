// Settings: general, users, roles, custom fields, email, tokens, backups, network, audit log, about.
import { get, patch, post, put, qs, upload } from '../api.js';
import {
  busy, can, confirmDialog, copyButton, dropzone, empty, errorText, fmtBytes, fmtDateTime, h, loading, modal, mount,
  notice, pill, promptDialog, showError, state, tabs, toast,
} from '../ui.js';

let S = null; // settings payload (for managers)

export function render(container, params) {
  const main = h('div');
  const defs = [];
  if (can('settings.manage')) defs.push({ key: 'general', label: 'General' });
  if (can('users.manage')) defs.push({ key: 'users', label: 'Users' }, { key: 'roles', label: 'Roles and permissions' });
  if (can('settings.manage')) defs.push({ key: 'fields', label: 'Custom fields' }, { key: 'email', label: 'Email and Outlook' });
  if (can('users.manage')) defs.push({ key: 'tokens', label: 'Integration tokens' });
  if (can('backups.manage')) defs.push({ key: 'backups', label: 'Backups and transfer' });
  if (state.me.user.role === 'admin') defs.push({ key: 'network', label: 'Network' });
  if (can('audit.view')) defs.push({ key: 'audit', label: 'Audit log' });
  defs.push({ key: 'about', label: 'About' });
  const views = { general, users, roles, fields: customFields, email, tokens, backups, network, audit, about };
  const first = params && params[0] && defs.some((d) => d.key === params[0]) ? params[0] : defs[0].key;
  mount(container, h('div', { class: 'page-head' }, h('div', h('h1', 'Settings'))), tabs(defs, async (k) => {
    mount(main, loading());
    try {
      if ((can('settings.manage') || can('users.manage')) && ['general', 'fields', 'email', 'roles', 'network', 'users', 'tokens'].includes(k)) S = await get('/api/settings');
      await views[k](main);
    } catch (e) { mount(main, notice('error', errorText(e))); }
  }, first), main);
}

function saveBtn(label, fn) {
  const b = h('button', { class: 'primary' }, label);
  b.addEventListener('click', () => busy(b, async () => { try { await fn(); } catch (e) { showError(e); } }));
  return b;
}

// ------------------------------------------------------------------ general

async function general(main) {
  const s = S.settings;
  const locs = h('textarea', { value: s.locations.join('\n'), style: { minHeight: '80px' } });
  const tz = h('input', { type: 'text', value: s.timezone });
  const dayFirst = h('input', { type: 'checkbox', checked: s.date_day_first });
  const enrich = S.fields.filter((f) => f.importable).map((f) => h('label', { class: 'field inline small' }, h('input', { type: 'checkbox', value: f.key, checked: s.historical_enrich_fields.includes(f.key) }), f.label));
  const limitInputs = (obj) => Object.entries(obj).map(([k, v]) => h('label', { class: 'field' }, k.replace(/_/g, ' '), h('input', { type: 'number', min: 1, step: 'any', value: v, dataset: { k } })));
  const photoLimits = h('div', { class: 'grid3' }, limitInputs(s.photo_limits));
  const importLimits = h('div', { class: 'grid3' }, limitInputs(s.import_limits));
  const read = (box) => Object.fromEntries(Array.from(box.querySelectorAll('input')).map((i) => [i.dataset.k, Number(i.value)]));
  mount(main,
    h('div', { class: 'card' }, h('h2', 'Locations and dates'),
      h('div', { class: 'grid2' },
        h('label', { class: 'field' }, 'Locations (one per line). A location in use cannot be removed.', locs),
        h('div', { class: 'stack' }, h('label', { class: 'field' }, 'Timezone (IANA name)', tz),
          h('label', { class: 'field inline' }, dayFirst, 'Read numeric dates day-first (03/04/2026 = 3 April 2026)'))),
      h('h3', 'Approved historical fields'),
      h('p', { class: 'small muted' }, 'A historical import may fill these fields on matched current employees (only where blank, unless the import says otherwise).'),
      h('div', { class: 'row' }, enrich),
      h('h3', 'Photo limits'), photoLimits, h('h3', 'Import limits'), importLimits,
      h('div', { class: 'spacer' }),
      saveBtn('Save', async () => {
        await patch('/api/settings', {
          locations: locs.value.split('\n').map((x) => x.trim()).filter(Boolean), timezone: tz.value.trim(), date_day_first: dayFirst.checked,
          historical_enrich_fields: enrich.map((l) => l.querySelector('input')).filter((i) => i.checked).map((i) => i.value),
          photo_limits: read(photoLimits), import_limits: read(importLimits),
        });
        toast('Settings saved. Reload the page to see new locations everywhere.', 'ok');
      })),
    h('div', { class: 'card' }, h('h2', 'Data folder'),
      h('p', h('code', S.data_dir)),
      h('p', { class: 'muted' }, 'The database, photos, packages and staging files live here. Keep it outside OneDrive, SharePoint or other synchronised folders. To move to another computer: stop the application (Settings > Network > Stop application, or close it), copy the whole folder, start the application there, then revalidate email settings. Never run two copies against the same data.')));
}

// -------------------------------------------------------------------- users

async function users(main) {
  const list = await get('/api/users');
  const roleLabel = S ? S.roles : { admin: 'Administrator', editor: 'Editor', viewer: 'Viewer', export_operator: 'Export Operator' };
  mount(main, h('div', { class: 'card' },
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Users'), h('button', { class: 'primary', onclick: () => userForm(null, () => users(main)) }, '+ Add user')),
    h('p', { class: 'small muted' }, 'Users sign in with their email address. New users and reset passwords are temporary: the user must choose a new password at the next sign-in.'),
    h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Email'), h('th', 'Name'), h('th', 'Role'), h('th', 'Locations'), h('th', 'Status'), h('th', 'Last sign-in'), h('th', ''))),
      h('tbody', list.map((u) => h('tr', h('td', u.email), h('td', u.display_name), h('td', roleLabel[u.role] || u.role), h('td', u.locations.length ? u.locations.join(', ') : 'All'),
        h('td', u.is_active ? (u.locked_until && new Date(u.locked_until) > new Date() ? pill('locked', 'red') : pill('active', 'green')) : pill('disabled'), u.must_change_password ? h('span', ' ', pill('must change password', 'amber')) : null),
        h('td', fmtDateTime(u.last_login_at)),
        h('td', h('div', { class: 'row' },
          h('button', { class: 'small', onclick: () => userForm(u, () => users(main)) }, 'Edit'),
          h('button', { class: 'small', onclick: async () => {
            const pw = await promptDialog(`Reset password for ${u.email}`, 'Temporary password (at least 10 characters)', { type: 'text', required: true, help: 'Give this to the user privately. They must change it when they next sign in. All their sessions are signed out.' });
            if (!pw) return;
            try { await post(`/api/users/${u.id}/reset-password`, { temporary_password: pw }); toast('Password reset.', 'ok'); users(main); } catch (e) { showError(e); }
          } }, 'Reset password'),
          u.locked_until ? h('button', { class: 'small', onclick: async () => { try { await post(`/api/users/${u.id}/unlock`); toast('Unlocked.', 'ok'); users(main); } catch (e) { showError(e); } } }, 'Unlock') : null)))))))));
}

function userForm(u, done) {
  const perms = S.permissions;
  const email = h('input', { type: 'email', value: u ? u.email : '', disabled: !!u });
  const name = h('input', { type: 'text', value: u ? u.display_name : '' });
  const role = h('select', {}, Object.entries(S.roles).map(([k, v]) => h('option', { value: k }, v)));
  role.value = u ? u.role : 'viewer';
  const pw = u ? null : h('input', { type: 'text', autocomplete: 'off' });
  const active = h('input', { type: 'checkbox', checked: u ? !!u.is_active : true });
  const locBoxes = state.me.all_locations.map((l) => h('label', { class: 'field inline' }, h('input', { type: 'checkbox', value: l, checked: u ? u.locations.includes(l) : false }), l));
  const dob = h('select', {}, h('option', { value: '' }, 'Role default'), h('option', { value: 'full' }, 'Full date of birth'), h('option', { value: 'day_month' }, 'Day and month only'), h('option', { value: 'hidden' }, 'Hidden'));
  dob.value = u && u.dob_access ? u.dob_access : '';
  const permRows = Object.entries(perms).map(([k, label]) => {
    const sel = h('select', { dataset: { perm: k } }, h('option', { value: '' }, 'Role default'), h('option', { value: 'grant' }, 'Allow'), h('option', { value: 'revoke' }, 'Deny'));
    sel.value = u && u.perm_grants.includes(k) ? 'grant' : u && u.perm_revokes.includes(k) ? 'revoke' : '';
    return h('tr', h('td', label, h('div', { class: 'faint small mono' }, k)), h('td', sel));
  });
  const hideable = S.fields.filter((f) => !['id', 'rhc_id', 'full_name'].includes(f.key));
  const hidden = hideable.map((f) => h('label', { class: 'field inline small' }, h('input', { type: 'checkbox', value: f.key, checked: u ? u.hidden_fields.includes(f.key) : false }), f.label));
  const err = h('div');
  const m = modal({ title: u ? `Edit ${u.email}` : 'Add user', size: 'wide', body: h('div', { class: 'stack' }, err,
    h('div', { class: 'grid2' }, h('label', { class: 'field' }, 'Email (username)', email), h('label', { class: 'field' }, 'Display name', name),
      h('label', { class: 'field' }, 'Role', role), pw ? h('label', { class: 'field' }, 'Temporary password (at least 10 characters)', pw) : null),
    h('label', { class: 'field inline' }, active, 'Account active'),
    h('fieldset', h('legend', 'Location scope'), h('p', { class: 'small muted' }, 'Leave all unticked for access to every location. Administrators always see everything.'), h('div', { class: 'row' }, locBoxes)),
    h('fieldset', h('legend', 'Field restrictions'), h('label', { class: 'field' }, 'Date of birth access', dob), h('p', { class: 'small muted' }, 'Hidden fields are removed from tables, profiles, exports and imports for this user.'), h('div', { class: 'row' }, hidden)),
    h('details', h('summary', 'Permission overrides (advanced)'), h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('tbody', permRows))))),
  actions: [h('button', { onclick: () => m.close() }, 'Cancel'), saveBtn('Save', async () => {
    const grants = []; const revokes = [];
    for (const sel of m.box.querySelectorAll('select[data-perm]')) { if (sel.value === 'grant') grants.push(sel.dataset.perm); if (sel.value === 'revoke') revokes.push(sel.dataset.perm); }
    const body = { email: email.value, display_name: name.value, role: role.value, is_active: active.checked, dob_access: dob.value || null,
      locations: locBoxes.map((l) => l.querySelector('input')).filter((i) => i.checked).map((i) => i.value),
      hidden_fields: hidden.map((l) => l.querySelector('input')).filter((i) => i.checked).map((i) => i.value), perm_grants: grants, perm_revokes: revokes };
    try {
      if (u) await put(`/api/users/${u.id}`, body); else await post('/api/users', { ...body, password: pw.value });
      toast('User saved.', 'ok'); m.close(); done();
    } catch (e) { mount(err, notice('error', errorText(e))); }
  })] });
}

// -------------------------------------------------------------------- roles

async function roles(main) {
  const rp = S.settings.role_permissions;
  const dob = S.settings.role_dob_access;
  const roleKeys = Object.keys(S.roles);
  const rows = Object.entries(S.permissions).map(([p, label]) => h('tr', h('td', label, h('div', { class: 'faint small mono' }, p)),
    roleKeys.map((r) => h('td', { class: 'right' }, h('input', { type: 'checkbox', dataset: { role: r, perm: p }, checked: (rp[r] || []).includes(p), disabled: r === 'admin' })))));
  const dobSel = roleKeys.map((r) => {
    const sel = h('select', { dataset: { role: r }, disabled: r === 'admin' }, h('option', { value: 'full' }, 'Full'), h('option', { value: 'day_month' }, 'Day and month'), h('option', { value: 'hidden' }, 'Hidden'));
    sel.value = r === 'admin' ? 'full' : (dob[r] || 'hidden');
    return h('td', { class: 'right' }, sel);
  });
  const table = h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Permission'), roleKeys.map((r) => h('th', { class: 'right' }, S.roles[r])))), h('tbody', rows, h('tr', h('td', h('b', 'Date of birth access')), dobSel)));
  mount(main, h('div', { class: 'card' }, h('h2', 'Role permissions'),
    h('p', { class: 'muted' }, 'Defaults for each role. Individual users can have extra permissions allowed or denied in Users. Viewers are read-only; tick "Export employee data" for a viewer role only if they should export. All checks are enforced by the server, including downloads and photos. Fetch-only integration access uses tokens (see Integration tokens) and has no interactive access.'),
    h('div', { class: 'table-wrap' }, table), h('div', { class: 'spacer' }),
    saveBtn('Save role permissions', async () => {
      const out = {};
      for (const r of roleKeys) if (r !== 'admin') out[r] = [];
      for (const cb of table.querySelectorAll('input[type=checkbox]')) if (cb.checked && cb.dataset.role !== 'admin') out[cb.dataset.role].push(cb.dataset.perm);
      const dobOut = {};
      for (const sel of table.querySelectorAll('select[data-role]')) dobOut[sel.dataset.role] = sel.value;
      await patch('/api/settings', { role_permissions: out, role_dob_access: dobOut });
      toast('Role permissions saved.', 'ok');
    })));
}

// ----------------------------------------------------------- custom fields

async function customFields(main) {
  const list = await get('/api/custom-fields');
  mount(main, h('div', { class: 'card' },
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Custom fields'), h('button', { class: 'primary', onclick: () => fieldForm(null, () => customFields(main)) }, '+ Add field')),
    h('p', { class: 'muted' }, 'Custom fields appear in the add/edit forms, the directory columns, import mapping and Excel exports. Restricted fields are never given to integration tokens and can be hidden per user.'),
    list.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Label'), h('th', 'Key'), h('th', 'Type'), h('th', 'Options'), h('th', 'Status'), h('th', ''))),
      h('tbody', list.map((f) => h('tr', h('td', f.label), h('td', { class: 'mono' }, f.key), h('td', f.field_type), h('td', { class: 'small' }, f.options.join(', ')),
        h('td', f.is_active ? pill('active', 'green') : pill('inactive'), f.is_restricted ? h('span', ' ', pill('restricted', 'amber')) : null),
        h('td', h('button', { class: 'small', onclick: () => fieldForm(f, () => customFields(main)) }, 'Edit'))))))) : empty('No custom fields yet', 'Add text, number, date, dropdown or yes/no fields.')));
}

function fieldForm(f, done) {
  const label = h('input', { type: 'text', value: f ? f.label : '' });
  const type = h('select', {}, [['text', 'Text'], ['number', 'Number'], ['date', 'Date'], ['dropdown', 'Dropdown'], ['yesno', 'Yes / No']].map(([k, v]) => h('option', { value: k }, v)));
  type.value = f ? f.field_type : 'text';
  const options = h('textarea', { value: f ? f.options.join('\n') : '', placeholder: 'One option per line' });
  const restricted = h('input', { type: 'checkbox', checked: f ? !!f.is_restricted : false });
  const active = h('input', { type: 'checkbox', checked: f ? !!f.is_active : true });
  const order = h('input', { type: 'number', value: f ? f.sort_order : 0 });
  const err = h('div');
  const m = modal({ title: f ? `Edit field: ${f.label}` : 'Add custom field', body: h('div', { class: 'stack' }, err,
    h('label', { class: 'field' }, 'Label', label), h('label', { class: 'field' }, 'Type', type),
    h('label', { class: 'field' }, 'Dropdown options', options), h('label', { class: 'field' }, 'Sort order', order),
    h('label', { class: 'field inline' }, restricted, 'Restricted (sensitive; never shared with integrations)'), h('label', { class: 'field inline' }, active, 'Active')),
  actions: [h('button', { onclick: () => m.close() }, 'Cancel'), saveBtn('Save', async () => {
    const body = { label: label.value, field_type: type.value, options: options.value.split('\n').map((x) => x.trim()).filter(Boolean), is_restricted: restricted.checked, is_active: active.checked, sort_order: Number(order.value) || 0 };
    try {
      if (f) await put(`/api/custom-fields/${f.id}`, body); else await post('/api/custom-fields', body);
      toast('Field saved. Reload the page to see it in the directory.', 'ok'); m.close(); done();
    } catch (e) { mount(err, notice('error', errorText(e))); }
  })] });
}

// -------------------------------------------------------------------- email

async function email(main) {
  const s = S.settings;
  const mode = h('select', {}, [['auto', 'Automatic: classic Outlook if available, otherwise manual'], ['outlook', 'Classic Outlook (COM)'], ['manual', 'Manual (ready folder + manual completion)'], ['dry_run', 'Dry run (development/testing: nothing is sent)']].map(([k, v]) => h('option', { value: k }, v)));
  mode.value = s.mail_adapter;
  const acct = h('input', { type: 'text', value: s.outlook_default_account, placeholder: 'e.g. hr@relayhumancloud.com (blank = Outlook default)' });
  const sentTimeout = h('input', { type: 'number', min: 10, value: s.sent_items_timeout_minutes });
  const sendTimeout = h('input', { type: 'number', min: 10, value: s.outlook_send_timeout_seconds });
  const status = h('div', loading('Checking Outlook...'));
  get('/api/outlook/status').then((o) => mount(status, notice(o.available ? 'ok' : 'warn', h('b', `${o.adapter_label}: `), o.message,
    o.accounts && o.accounts.length ? h('div', 'Accounts: ', o.accounts.map((a) => a.smtp || a.name).join(', ')) : null))).catch((e) => mount(status, notice('error', errorText(e))));
  const reval = h('div');
  if (state.me.revalidation_required && state.me.user.role === 'admin') {
    const t = await get('/api/tasks');
    const boxes = t.tasks.filter((x) => x.auto_send_enabled).map((x) => h('label', { class: 'field inline' }, h('input', { type: 'checkbox', value: x.id, checked: true }),
      `${x.name}: To ${(x.email.to || []).join(', ') || '(none)'}; CC ${(x.email.cc || []).join(', ') || '(none)'}; from ${x.email.sender || 'default account'}`));
    const ok = h('input', { type: 'checkbox' });
    mount(reval, h('div', { class: 'card' }, h('h2', 'Revalidate after restore or transfer'),
      notice('warn', state.me.revalidation_required.reason),
      h('p', 'Check that the sender account exists in this computer\'s Outlook profile, that the recipients are still correct, and that you own the scheduled tasks on this computer. Automatic sending resumes only for the tasks you tick.'),
      boxes.length ? h('div', { class: 'stack' }, boxes) : h('p', { class: 'muted' }, 'No task has automatic sending switched on.'),
      h('label', { class: 'field inline' }, ok, 'I have checked the sender, recipients and task ownership'),
      saveBtn('Revalidate', async () => {
        await post('/api/settings/revalidate', { confirm: ok.checked, task_ids: boxes.map((b) => b.querySelector('input')).filter((i) => i.checked).map((i) => Number(i.value)) });
        toast('Revalidated. Reloading...', 'ok');
        setTimeout(() => location.reload(), 800);
      })));
  }
  mount(main, reval, h('div', { class: 'card' }, h('h2', 'Email sending'), status,
    h('div', { class: 'grid2' }, h('label', { class: 'field' }, 'Email method', mode), h('label', { class: 'field' }, 'Default sender account', acct),
      h('label', { class: 'field' }, 'Mark as "Delivery uncertain" if not in Sent Items after (minutes)', sentTimeout), h('label', { class: 'field' }, 'Wait for Outlook to accept a send for (seconds)', sendTimeout)),
    h('p', { class: 'small muted' }, 'Classic Outlook automation uses the Outlook profile of the Windows user running this application. It never bypasses Outlook security prompts; if a prompt appears, answer it in Outlook. The new Outlook for Windows and Outlook on the web cannot be automated: use the manual method. No SMTP passwords, Microsoft Graph or cloud services are used.'),
    saveBtn('Save', async () => {
      await patch('/api/settings', { mail_adapter: mode.value, outlook_default_account: acct.value, sent_items_timeout_minutes: Number(sentTimeout.value), outlook_send_timeout_seconds: Number(sendTimeout.value) });
      toast('Email settings saved. Reloading...', 'ok');
      setTimeout(() => location.reload(), 800);
    })));
}

// ------------------------------------------------------------------- tokens

async function tokens(main) {
  const list = await get('/api/tokens');
  const settings = S || await get('/api/settings').catch(() => null);
  mount(main, h('div', { class: 'card' },
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Read-only integration tokens (fetch-only)'), h('button', { class: 'primary', onclick: () => tokenForm(settings, () => tokens(main)) }, '+ Create token')),
    h('p', { class: 'muted' }, 'Local automations can read employee data with a token: GET /api/fetch/v1/employees with the header "Authorization: Bearer <token>". Tokens cannot sign in to the interface or change anything, are stored only as hashes, can be revoked, are limited to chosen locations and fields, and never receive restricted fields such as date of birth.'),
    list.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Name'), h('th', 'Prefix'), h('th', 'Locations'), h('th', 'Fields'), h('th', 'Photos'), h('th', 'Created'), h('th', 'Last used'), h('th', 'Status'), h('th', ''))),
      h('tbody', list.map((t) => h('tr', h('td', t.name), h('td', { class: 'mono' }, t.token_prefix + '\u2026'), h('td', t.locations.join(', ') || 'All'), h('td', { class: 'small' }, t.fields.join(', ')),
        h('td', t.include_photos ? 'Yes' : 'No'), h('td', `${fmtDateTime(t.created_at)} by ${t.created_by}`), h('td', fmtDateTime(t.last_used_at)),
        h('td', t.revoked_at ? pill('revoked', 'red') : t.expires_at && new Date(t.expires_at) < new Date() ? pill('expired') : pill('active', 'green')),
        h('td', t.revoked_at ? null : h('button', { class: 'small danger', onclick: async () => {
          if (!await confirmDialog('Revoke token', `Revoke "${t.name}"? Integrations using it stop working immediately.`, { danger: true, confirmText: 'Revoke' })) return;
          try { await post(`/api/tokens/${t.id}/revoke`); toast('Token revoked.', 'ok'); tokens(main); } catch (e) { showError(e); }
        } }, 'Revoke'))))))) : empty('No tokens', 'Create a token only for a specific local integration that needs to read employee data.')));
}

function tokenForm(settings, done) {
  const name = h('input', { type: 'text', placeholder: 'e.g. Reception display' });
  const fields = (settings ? settings.fields : state.me.fields).filter((f) => !f.restricted && !['photo', 'id', 'rhc_id', 'full_name'].includes(f.key));
  const fieldBoxes = fields.map((f) => h('label', { class: 'field inline small' }, h('input', { type: 'checkbox', value: f.key, checked: ['designation', 'department', 'location'].includes(f.key) }), f.label));
  const locBoxes = state.me.all_locations.map((l) => h('label', { class: 'field inline' }, h('input', { type: 'checkbox', value: l }), l));
  const photos = h('input', { type: 'checkbox' });
  const left = h('input', { type: 'checkbox' });
  const expires = h('input', { type: 'number', min: 1, placeholder: 'never' });
  const m = modal({ title: 'Create integration token', size: 'wide', body: h('div', { class: 'stack' },
    h('label', { class: 'field' }, 'Name', name),
    h('fieldset', h('legend', 'Fields (RHC ID, name and internal ID are always included)'), h('div', { class: 'row' }, fieldBoxes)),
    h('fieldset', h('legend', 'Locations (none ticked = all)'), h('div', { class: 'row' }, locBoxes)),
    h('label', { class: 'field inline' }, photos, 'Allow reading current photos'), h('label', { class: 'field inline' }, left, 'Include employees who have left'),
    h('label', { class: 'field' }, 'Expires after (days, optional)', expires)),
  actions: [h('button', { onclick: () => m.close() }, 'Cancel'), saveBtn('Create token', async () => {
    const r = await post('/api/tokens', { name: name.value, fields: fieldBoxes.map((l) => l.querySelector('input')).filter((i) => i.checked).map((i) => i.value),
      locations: locBoxes.map((l) => l.querySelector('input')).filter((i) => i.checked).map((i) => i.value), include_photos: photos.checked, include_left: left.checked,
      expires_days: expires.value ? Number(expires.value) : null });
    m.close();
    modal({ title: 'Token created', body: h('div', { class: 'stack' }, notice('warn', r.note), h('div', { class: 'row' }, h('code', { class: 'grow', style: { wordBreak: 'break-all' } }, r.token), copyButton(() => r.token)),
      h('p', { class: 'small muted' }, `Fields: ${r.fields.join(', ')}`)), onClose: done });
  })] });
}

// ------------------------------------------------------------------ backups

async function backups(main) {
  const d = await get('/api/backups');
  const s = d.settings;
  const dest = h('input', { type: 'text', value: s.destination, placeholder: `Default: ${d.destination}` });
  const daily = h('input', { type: 'checkbox', checked: s.daily_enabled });
  const time = h('input', { type: 'time', value: s.daily_time });
  const keep = h('input', { type: 'number', min: 1, value: s.retention_daily });
  const keepManual = h('input', { type: 'number', min: 0, value: s.retention_manual });
  const zips = h('input', { type: 'checkbox', checked: s.include_package_zips });
  const restoreUpload = h('div');
  const rows = d.backups.map((b) => h('tr', h('td', b.filename, h('div', { class: 'faint small' }, b.path)), h('td', b.kind),
    h('td', pill(b.status, b.status === 'verified' ? 'green' : b.status === 'failed' ? 'red' : ''), b.exists ? null : h('div', { class: 'small field-error' }, 'file not found')),
    h('td', fmtBytes(b.size)), h('td', `${fmtDateTime(b.created_at)}${b.created_by ? ' by ' + b.created_by : ''}`), h('td', { class: 'small' }, b.error || (b.verified_at ? `verified ${fmtDateTime(b.verified_at)}` : '')),
    h('td', b.exists && b.status === 'verified' ? h('div', { class: 'row' },
      h('button', { class: 'small', onclick: (e) => busy(e.currentTarget, async () => { try { const r = await post(`/api/backups/${b.id}/verify`); toast(`Backup verified: ${r.file_count} files, ${r.counts.employees} employees, ${r.counts.photos} photos.`, 'ok'); backups(main); } catch (err) { showError(err); } }) }, 'Verify'),
      state.me.user.role === 'admin' ? h('button', { class: 'small danger', onclick: () => restore(b) }, 'Restore') : null) : null)));
  if (state.me.user.role === 'admin') {
    mount(restoreUpload, dropzone({ accept: '.zip', multiple: false, label: 'Restore from a backup file (for example from an external drive or another computer)', onFiles: async (files) => {
      const typed = await confirmDialog('Restore from file', restoreWarning(), { danger: true, confirmText: 'Restore', requireText: 'RESTORE' });
      if (!typed) return;
      const fd = new FormData();
      fd.append('file', files[0].file);
      toast('Uploading and verifying the backup...');
      try { const r = await upload(`/api/backups/restore-upload?confirm_text=${encodeURIComponent(typed)}`, fd); restoreDone(r); } catch (e) { showError(e); }
    } }));
  }
  mount(main,
    h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Backups'),
      h('button', { class: 'primary', onclick: (e) => busy(e.currentTarget, async () => { try { const b = await post('/api/backups'); toast(`Backup created and verified: ${b.filename}`, 'ok'); backups(main); } catch (err) { showError(err); } }) }, 'Back up now')),
      h('p', { class: 'muted' }, 'A backup is one ZIP file with a consistent copy of the database (taken safely while running), every photo version, thumbnails, package manifests and settings. Each backup is verified after it is written. The backup folder is never included in backups.'),
      d.backups.length ? h('div', { class: 'table-wrap', style: { maxHeight: '380px' } }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'File'), h('th', 'Type'), h('th', 'Status'), h('th', 'Size'), h('th', 'Created'), h('th', 'Details'), h('th', ''))), h('tbody', rows))) : empty('No backups yet', 'Click "Back up now" to create the first one.')),
    h('div', { class: 'card' }, h('h2', 'Backup settings'),
      h('div', { class: 'grid2' }, h('label', { class: 'field' }, 'Backup folder (for example an external drive such as E:\\RelayBackups)', dest),
        h('label', { class: 'field' }, 'Daily backup time', time), h('label', { class: 'field' }, 'Keep this many daily backups', keep), h('label', { class: 'field' }, 'Keep this many manual backups (0 = keep all)', keepManual)),
      h('label', { class: 'field inline' }, daily, 'Create a full backup automatically once a day while the application is running'),
      h('label', { class: 'field inline' }, zips, 'Include prepared package ZIP files (larger backups; manifests are always included)'),
      saveBtn('Save backup settings', async () => {
        await patch('/api/settings', { backup: { destination: dest.value.trim(), daily_enabled: daily.checked, daily_time: time.value, retention_daily: Number(keep.value), retention_manual: Number(keepManual.value), include_package_zips: zips.checked } });
        toast('Backup settings saved.', 'ok');
        backups(main);
      })),
    state.me.user.role === 'admin' ? h('div', { class: 'card' }, h('h2', 'Restore'), notice('warn', restoreWarning()), restoreUpload) : null,
    h('div', { class: 'card' }, h('h2', 'Moving to another computer'),
      h('ol', h('li', 'Create a backup (or stop the application and copy the whole data folder).'), h('li', 'Install Relay Employee Hub on the new computer.'),
        h('li', 'Restore the backup there (or point the application at the copied folder with --set-data-dir).'), h('li', 'Sign in as an administrator and revalidate email settings. Automatic sending stays off until you do.'),
        h('li', 'Stop using the old computer. Never run two active copies: they would not share changes and could send duplicate emails.'))));
}

function restoreWarning() {
  return 'Restoring replaces all current employees, photos, templates, users, task history and settings with the backup. A safety backup of the current data is taken first. Everyone is signed out, and automatic sending is switched off until an administrator revalidates the sender, recipients and task ownership.';
}

async function restore(b) {
  const typed = await confirmDialog(`Restore ${b.filename}`, restoreWarning(), { danger: true, confirmText: 'Restore', requireText: 'RESTORE' });
  if (!typed) return;
  try { const r = await post('/api/backups/restore', { backup_id: b.id, confirm_text: typed }); restoreDone(r); } catch (e) { showError(e); }
}

function restoreDone(r) {
  modal({ title: 'Restore complete', dismissable: false, body: h('div', { class: 'stack' }, notice('ok', r.message), h('p', `Safety backup of the previous data: ${r.pre_restore_backup}`)),
    actions: [h('button', { class: 'primary', onclick: () => location.reload() }, 'Sign in again')] });
}

// ------------------------------------------------------------------ network

async function network(main) {
  const c = S.server;
  const lan = h('input', { type: 'checkbox', checked: c.lan_enabled });
  const host = h('input', { type: 'text', value: c.lan_enabled ? c.host : '0.0.0.0' });
  const port = h('input', { type: 'number', value: c.port });
  const hosts = h('input', { type: 'text', value: c.allowed_hosts.join(', '), placeholder: 'e.g. HR-PC01, 192.168.1.25' });
  const cert = h('input', { type: 'text', value: c.tls_certfile || '' });
  const key = h('input', { type: 'text', value: c.tls_keyfile || '' });
  const idle = h('input', { type: 'number', value: c.session_idle_minutes });
  const max = h('input', { type: 'number', value: c.session_max_hours });
  const ack = h('input', { type: 'checkbox' });
  mount(main,
    h('div', { class: 'card' }, h('h2', 'Network access'),
      notice('info', 'By default the application only accepts connections from this computer (localhost). Office-network (LAN) access is optional and must be switched on here. Never expose this application to the internet.'),
      h('label', { class: 'field inline' }, lan, 'Allow other computers on the office network to connect'),
      h('div', { class: 'grid2' }, h('label', { class: 'field' }, 'Listen address when LAN access is on (0.0.0.0 = all network adapters)', host), h('label', { class: 'field' }, 'Port', port),
        h('label', { class: 'field' }, 'Allowed host names / IP addresses users will type (comma separated)', hosts),
        h('label', { class: 'field' }, 'TLS certificate file (optional, for HTTPS)', cert), h('label', { class: 'field' }, 'TLS key file (optional)', key),
        h('label', { class: 'field' }, 'Sign out after inactivity (minutes)', idle), h('label', { class: 'field' }, 'Maximum session length (hours)', max)),
      h('label', { class: 'field inline' }, ack, 'I understand that without HTTPS, sign-in and data travel unencrypted on the office network, and that Windows Firewall must allow the port only for the office network.'),
      saveBtn('Save network settings', async () => {
        const r = await put('/api/server-config', { lan_enabled: lan.checked, host: host.value.trim() || '0.0.0.0', port: Number(port.value), allowed_hosts: hosts.value.split(',').map((x) => x.trim()).filter(Boolean),
          tls_certfile: cert.value.trim() || null, tls_keyfile: key.value.trim() || null, session_idle_minutes: Number(idle.value), session_max_hours: Number(max.value), confirm_lan: ack.checked });
        toast(r.restart_required ? 'Saved. Restart the application for network changes to take effect.' : 'Saved.', 'ok');
      })),
    h('div', { class: 'card' }, h('h2', 'Stop the application'),
      h('p', { class: 'muted' }, 'Stops the server on this computer (for example before copying the data folder). Scheduled tasks do not run while it is stopped. Start it again from the desktop shortcut.'),
      h('button', { class: 'danger', onclick: async () => {
        if (!await confirmDialog('Stop application', 'Stop Relay Employee Hub now? Everyone connected will lose access until it is started again.', { danger: true, confirmText: 'Stop' })) return;
        try { await post('/api/server/shutdown'); mount(document.getElementById('app'), h('div', { class: 'auth-wrap' }, h('div', { class: 'auth-card' }, h('h1', 'Stopped'), h('p', 'Relay Employee Hub has stopped. Close this tab. Start it again from the desktop shortcut.')))); } catch (e) { showError(e); }
      } }, 'Stop application')));
}

// -------------------------------------------------------------------- audit

async function audit(main, offset = 0, filters = {}) {
  const d = await get('/api/audit' + qs({ ...filters, limit: 100, offset }));
  const action = h('input', { type: 'text', placeholder: 'Action starts with (e.g. import, email, user)', value: filters.action || '' });
  const actor = h('input', { type: 'text', placeholder: 'User', value: filters.actor || '' });
  const search = h('input', { type: 'search', placeholder: 'Search text', value: filters.search || '' });
  const apply = () => audit(main, 0, { action: action.value, actor: actor.value, search: search.value });
  mount(main, h('div', { class: 'card' }, h('h2', 'Audit log'),
    h('div', { class: 'toolbar' }, action, actor, search, h('button', { onclick: apply }, 'Filter')),
    h('div', { class: 'table-wrap', style: { maxHeight: '560px' } }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'When'), h('th', 'User'), h('th', 'Action'), h('th', 'Summary'), h('th', 'Details'))),
      h('tbody', d.items.map((a) => h('tr', h('td', { class: 'nowrap' }, fmtDateTime(a.ts)), h('td', a.actor || ''), h('td', { class: 'mono small' }, a.action), h('td', a.summary || ''),
        h('td', a.details ? h('details', h('summary', 'show'), h('pre', { class: 'copybox' }, JSON.stringify(a.details, null, 1))) : '')))))),
    h('div', { class: 'pager' }, h('button', { class: 'small', disabled: offset === 0, onclick: () => audit(main, Math.max(0, offset - 100), filters) }, '\u2039 Newer'),
      h('span', { class: 'muted' }, `${offset + 1}-${Math.min(offset + 100, d.total)} of ${d.total}`),
      h('button', { class: 'small', disabled: offset + 100 >= d.total, onclick: () => audit(main, offset + 100, filters) }, 'Older \u203a'))));
}

// -------------------------------------------------------------------- about

async function about(main) {
  let notices = '';
  try { const r = await fetch('/api/licenses'); notices = await r.text(); } catch (e) { notices = 'Could not load notices.'; }
  mount(main, h('div', { class: 'card' }, h('h2', `Relay Employee Hub ${state.me.version}`),
    h('p', 'Local employee records, photos and headshot packages for Relay Human Cloud. All data stays on this computer; the only network use is sending email through your own classic Outlook.'),
    h('p', { class: 'muted' }, 'Application permissions protect data inside this application. A Windows administrator of the computer that holds the data folder can read the files directly; protect that computer and its backups accordingly.'),
    state.me.data_dir ? h('p', 'Data folder: ', h('code', state.me.data_dir)) : null),
  h('div', { class: 'card' }, h('h2', 'Third-party software notices'), h('pre', { class: 'copybox', style: { maxHeight: '420px' } }, notices)));
}
