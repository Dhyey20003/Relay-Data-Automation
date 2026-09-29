// Application shell: first-run setup, sign in, navigation and routing.
import { get, post, setCsrf } from './api.js';
import { busy, can, errorText, h, modal, mount, notice, showError, state, toast } from './ui.js';
import * as employees from './views/employees.js';
import * as importsView from './views/imports.js';
import * as photosView from './views/photos.js';
import * as tasksView from './views/tasks.js';
import * as settingsView from './views/settings.js';

const app = document.getElementById('app');

const SECTIONS = [
  { key: 'employees', label: 'Employees', perm: 'employees.view', view: employees },
  { key: 'import', label: 'Import Data', perm: 'imports.run', view: importsView },
  { key: 'photos', label: 'Photos', perm: 'photos.view', view: photosView },
  { key: 'tasks', label: 'Tasks and Exports', perm: 'tasks.view', view: tasksView },
  { key: 'settings', label: 'Settings', perm: null, view: settingsView },
];

function brand() {
  return h('div', { class: 'brand' }, h('div', { class: 'logo' }, 'RH'), h('div', 'Relay Employee Hub'));
}

async function boot() {
  let health;
  try { health = await get('/api/health'); } catch (e) {
    mount(app, h('div', { class: 'auth-wrap' }, h('div', { class: 'auth-card' }, brand(), notice('error', errorText(e)))));
    return;
  }
  if (health.setup_required) return renderSetup();
  try {
    state.me = await get('/api/me');
  } catch (e) {
    return renderLogin(e.status === 403 ? e.message : null);
  }
  setCsrf(state.me.csrf);
  state.tz = state.me.timezone;
  if (state.me.user.must_change_password) return renderPasswordChange(true);
  renderShell();
}

function renderSetup() {
  const email = h('input', { type: 'email', autocomplete: 'username' });
  const name = h('input', { type: 'text' });
  const pw = h('input', { type: 'password', autocomplete: 'new-password' });
  const pw2 = h('input', { type: 'password', autocomplete: 'new-password' });
  const msg = h('div');
  const btn = h('button', { class: 'primary', type: 'submit' }, 'Create administrator');
  const form = h('form', { class: 'stack', onsubmit: async (e) => {
    e.preventDefault();
    if (pw.value !== pw2.value) { mount(msg, notice('error', 'The passwords do not match.')); return; }
    await busy(btn, async () => {
      try {
        await post('/api/setup', { email: email.value, display_name: name.value, password: pw.value });
        location.hash = '#/employees';
        await boot();
      } catch (err) { mount(msg, notice('error', errorText(err))); }
    });
  } },
  h('h1', 'Welcome'),
  h('p', { class: 'muted' }, 'Create the first administrator account. There are no default passwords; this account manages users, settings and backups.'),
  msg,
  h('label', { class: 'field' }, 'Your work email (used as the username)', email),
  h('label', { class: 'field' }, 'Your name', name),
  h('label', { class: 'field' }, 'Password (at least 10 characters)', pw),
  h('label', { class: 'field' }, 'Repeat password', pw2),
  btn);
  mount(app, h('div', { class: 'auth-wrap' }, h('div', { class: 'auth-card' }, brand(), form)));
}

function renderLogin(message) {
  const email = h('input', { type: 'email', autocomplete: 'username', required: true });
  const pw = h('input', { type: 'password', autocomplete: 'current-password', required: true });
  const msg = h('div', message ? notice('warn', message) : null);
  const btn = h('button', { class: 'primary', type: 'submit' }, 'Sign in');
  const form = h('form', { class: 'stack', onsubmit: async (e) => {
    e.preventDefault();
    await busy(btn, async () => {
      try {
        await post('/api/auth/login', { email: email.value, password: pw.value });
        await boot();
      } catch (err) { mount(msg, notice('error', errorText(err))); pw.value = ''; pw.focus(); }
    });
  } },
  h('h1', 'Sign in'),
  msg,
  h('label', { class: 'field' }, 'Email', email),
  h('label', { class: 'field' }, 'Password', pw),
  btn,
  h('p', { class: 'small muted' }, 'Forgotten your password? Ask an administrator to reset it.'));
  mount(app, h('div', { class: 'auth-wrap' }, h('div', { class: 'auth-card' }, brand(), form)));
  setTimeout(() => email.focus(), 50);
}

export function renderPasswordChange(forced) {
  const cur = h('input', { type: 'password', autocomplete: 'current-password' });
  const pw = h('input', { type: 'password', autocomplete: 'new-password' });
  const pw2 = h('input', { type: 'password', autocomplete: 'new-password' });
  const msg = h('div');
  const btn = h('button', { class: 'primary', type: 'submit' }, 'Change password');
  const form = h('form', { class: 'stack', onsubmit: async (e) => {
    e.preventDefault();
    if (pw.value !== pw2.value) { mount(msg, notice('error', 'The new passwords do not match.')); return; }
    await busy(btn, async () => {
      try {
        await post('/api/me/password', { current_password: cur.value, new_password: pw.value });
        toast('Password changed.', 'ok');
        await boot();
      } catch (err) { mount(msg, notice('error', errorText(err))); }
    });
  } },
  h('h1', forced ? 'Choose a new password' : 'Change password'),
  forced ? h('p', { class: 'muted' }, 'An administrator set a temporary password for you. Choose your own password to continue.') : null,
  msg,
  h('label', { class: 'field' }, forced ? 'Temporary password' : 'Current password', cur),
  h('label', { class: 'field' }, 'New password (at least 10 characters)', pw),
  h('label', { class: 'field' }, 'Repeat new password', pw2),
  btn,
  h('button', { type: 'button', class: 'link', onclick: logout }, 'Sign out'));
  mount(app, h('div', { class: 'auth-wrap' }, h('div', { class: 'auth-card' }, brand(), form)));
}

async function logout() {
  try { await post('/api/auth/logout'); } catch (e) { /* session may already be gone */ }
  state.me = null;
  setCsrf(null);
  renderLogin();
}

let content;
let navEl;

function renderShell() {
  const me = state.me;
  navEl = h('nav', { class: 'nav' }, SECTIONS.filter((s) => !s.perm || can(s.perm)).map((s) => h('a', { href: `#/${s.key}`, dataset: { key: s.key }, onclick: () => { if (location.hash === `#/${s.key}`) setTimeout(route, 0); } }, s.label)));
  const adapterBadge = me.mail_adapter.name === 'dry_run' ? h('span', { class: 'dry-badge', title: 'Emails are recorded to disk and never sent' }, 'Email: DRY RUN') : null;
  const header = h('header', { class: 'topbar' }, brand(), navEl,
    h('div', { class: 'userbox' }, adapterBadge,
      h('div', { class: 'who' }, me.user.name, h('small', `${me.user.role_label}${me.locations.length < me.all_locations.length ? ' · ' + me.locations.join(', ') : ''}`)),
      h('button', { class: 'small', onclick: () => renderPasswordChangeModal() }, 'Password'),
      h('button', { class: 'small', onclick: logout }, 'Sign out')));
  const banners = h('div');
  if (me.revalidation_required) {
    banners.appendChild(notice('warn', h('b', 'Automatic sending is paused. '), me.revalidation_required.reason + ' ',
      me.user.role === 'admin' ? h('a', { href: '#/settings/email' }, 'Review sender, recipients and task ownership') : 'An administrator must review the email settings.'));
  }
  content = h('main');
  mount(app, header, h('div', { style: { maxWidth: '1680px', margin: '0 auto', padding: '12px 22px 0' } }, banners), content);
  route();
}

function renderPasswordChangeModal() {
  {
    const cur = h('input', { type: 'password' });
    const pw = h('input', { type: 'password' });
    const pw2 = h('input', { type: 'password' });
    const btn = h('button', { class: 'primary' }, 'Change password');
    const m = modal({ title: 'Change password', body: h('div', { class: 'stack' },
      h('label', { class: 'field' }, 'Current password', cur),
      h('label', { class: 'field' }, 'New password (at least 10 characters)', pw),
      h('label', { class: 'field' }, 'Repeat new password', pw2)), actions: [btn] });
    btn.addEventListener('click', () => busy(btn, async () => {
      if (pw.value !== pw2.value) { toast('The new passwords do not match.', 'error'); return; }
      try { await post('/api/me/password', { current_password: cur.value, new_password: pw.value }); toast('Password changed. Other sessions were signed out.', 'ok'); m.close(); }
      catch (e) { showError(e); }
    }));
  }
}

function route() {
  if (!state.me || !content) return;
  // Dialogs belong to the page they were opened from.
  for (const b of Array.from(document.querySelectorAll('#modals .modal-backdrop'))) {
    const x = b.querySelector('.close-x');
    if (x) x.click(); else b.remove();
  }
  const parts = (location.hash || '#/employees').replace(/^#\//, '').split('/');
  let section = SECTIONS.find((s) => s.key === parts[0] && (!s.perm || can(s.perm)));
  if (!section) section = SECTIONS.find((s) => !s.perm || can(s.perm));
  for (const a of navEl.querySelectorAll('a')) a.classList.toggle('active', a.dataset.key === section.key);
  document.title = `${section.label} - Relay Employee Hub`;
  section.view.render(content, parts.slice(1));
}

window.addEventListener('hashchange', route);
window.addEventListener('relay:unauthorized', (e) => {
  if (state.me) { state.me = null; renderLogin(e.detail || 'Your session has expired. Please sign in again.'); }
});
boot();
