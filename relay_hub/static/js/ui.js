// Small DOM toolkit: element builder, modals, confirmations, toasts, formatting, inputs.
import { get } from './api.js';

export const state = { me: null, tz: 'Asia/Kolkata' };

// h('div', {class: 'x', onclick: fn}, child, 'text', [children])
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs !== undefined && (attrs === null || typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) { el.replaceChildren(); return el; }
export function mount(el, ...children) {
  const frag = document.createDocumentFragment();
  append(frag, children);
  el.replaceChildren(frag);
  return el;
}

export function loading(text = 'Loading...') { return h('div', { class: 'loading' }, h('span', { class: 'spinner' }), text); }
export function empty(title, text, ...actions) {
  return h('div', { class: 'empty' }, h('h3', title), text ? h('p', text) : null, actions.length ? h('div', { class: 'row', style: { justifyContent: 'center' } }, actions) : null);
}
export function notice(kind, ...content) { return h('div', { class: `notice ${kind}` }, content); }
export function pill(text, color = '') { return h('span', { class: `pill ${color}` }, text); }
export function statusPill(status) { return h('span', { class: `pill status-${String(status).replace(/\s+/g, '')}` }, status); }

export function toast(message, kind = '') {
  const t = h('div', { class: `toast ${kind}` }, message);
  document.getElementById('toasts').appendChild(t);
  setTimeout(() => t.remove(), kind === 'error' ? 9000 : 4500);
}

export function errorText(err) {
  if (!err) return 'Unknown error';
  let msg = err.message || String(err);
  const d = err.details;
  if (d && d.fields) msg += ' ' + Object.values(d.fields).join(' ');
  if (d && d.mapping_problems) msg += ' ' + d.mapping_problems.join(' ');
  if (d && d.problems && !msg.includes(d.problems[0])) msg += ' ' + d.problems.join(' ');
  return msg;
}
export function showError(err) { toast(errorText(err), 'error'); }

// Run an async action with the button disabled and a spinner.
export async function busy(button, fn) {
  const original = button ? Array.from(button.childNodes) : null;
  if (button) { button.disabled = true; mount(button, h('span', { class: 'spinner' }), ' Working...'); }
  try { return await fn(); } finally {
    if (button) { button.disabled = false; mount(button, original); }
  }
}

// ------------------------------------------------------------------ modals

export function modal({ title, body, actions = [], size = '', onClose, dismissable = true }) {
  const root = document.getElementById('modals');
  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); if (onClose) onClose(); };
  const onKey = (e) => { if (e.key === 'Escape' && dismissable && root.lastChild === backdrop) close(); };
  const foot = h('div', { class: 'modal-foot' });
  const box = h('div', { class: `modal ${size}`, role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-head' }, h('h2', title), dismissable ? h('button', { class: 'close-x', title: 'Close', onclick: close }, '×') : null),
    h('div', { class: 'modal-body' }, body),
    foot);
  const backdrop = h('div', { class: 'modal-backdrop', onmousedown: (e) => { if (e.target === backdrop && dismissable) close(); } }, box);
  for (const a of actions) foot.appendChild(a);
  if (!actions.length) foot.remove();
  root.appendChild(backdrop);
  document.addEventListener('keydown', onKey);
  const first = box.querySelector('input, select, textarea');
  if (first) setTimeout(() => first.focus(), 30);
  return { close, box, body: box.querySelector('.modal-body'), foot };
}

// Confirmation dialog. Returns a promise resolving to true/false.
// Options: danger, confirmText, requireText (user must type it), extra (node).
export function confirmDialog(title, message, opts = {}) {
  return new Promise((resolve) => {
    let done = false;
    const typed = opts.requireText ? h('input', { type: 'text', placeholder: `Type ${opts.requireText}`, autocomplete: 'off' }) : null;
    const ok = h('button', { class: opts.danger ? 'danger solid' : 'primary', disabled: !!opts.requireText }, opts.confirmText || 'Confirm');
    if (typed) typed.addEventListener('input', () => { ok.disabled = typed.value.trim().toUpperCase() !== opts.requireText.toUpperCase(); });
    const m = modal({
      title, size: opts.size || '',
      body: h('div', { class: 'stack' }, typeof message === 'string' ? h('p', message) : message, opts.extra || null,
        typed ? h('label', { class: 'field' }, `To confirm, type ${opts.requireText}`, typed) : null),
      actions: [h('button', { onclick: () => { done = true; m.close(); resolve(false); } }, 'Cancel'), ok],
      onClose: () => { if (!done) resolve(false); },
    });
    ok.addEventListener('click', () => { done = true; m.close(); resolve(typed ? typed.value : true); });
  });
}

export function promptDialog(title, label, opts = {}) {
  return new Promise((resolve) => {
    let done = false;
    const input = opts.multiline ? h('textarea', { value: opts.value || '' }) : h('input', { type: opts.type || 'text', value: opts.value || '' });
    const ok = h('button', { class: 'primary' }, opts.confirmText || 'OK');
    const m = modal({
      title, body: h('div', { class: 'stack' }, opts.help ? h('p', { class: 'muted' }, opts.help) : null, h('label', { class: 'field' }, label, input)),
      actions: [h('button', { onclick: () => { done = true; m.close(); resolve(null); } }, 'Cancel'), ok],
      onClose: () => { if (!done) resolve(null); },
    });
    ok.addEventListener('click', () => {
      if (opts.required && !input.value.trim()) { input.classList.add('invalid'); return; }
      done = true; m.close(); resolve(input.value);
    });
  });
}

export function imagePreview(src, title = 'Preview') {
  modal({ title, size: 'wide', body: h('img', { class: 'full-image', src, alt: title }) });
}

// --------------------------------------------------------------- formatting

export function fmtDate(v) {
  if (!v) return '';
  if (typeof v === 'string' && v.startsWith('--')) {
    const [m, d] = v.slice(2).split('-');
    const month = new Date(2000, Number(m) - 1, 1).toLocaleString('en-GB', { month: 'short' });
    return `${Number(d)} ${month}`;
  }
  const [y, m, d] = String(v).slice(0, 10).split('-').map(Number);
  if (!y) return String(v);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' });
}
export function fmtDateTime(v) {
  if (!v) return '';
  const d = new Date(v);
  if (isNaN(d)) return v;
  return d.toLocaleString('en-GB', { timeZone: state.tz, day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}
export function fmtBytes(n) {
  if (n === null || n === undefined) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
export function fmtValue(field, v) {
  if (v === null || v === undefined || v === '') return '';
  if (field && field.type === 'date') return fmtDate(v);
  if (field && field.type === 'timestamp') return fmtDateTime(v);
  if (field && field.key === 'record_type') return v === 'historical' ? 'Historical only' : 'Directory';
  return String(v);
}

// ------------------------------------------------------------------- inputs

// Build an input element for a field definition. Returns the element; read with readInput().
export function fieldInput(field, value) {
  let el;
  const opts = field.options || [];
  if (field.type === 'enum' || field.type === 'dropdown') {
    el = h('select', {}, h('option', { value: '' }, '(blank)'), opts.map((o) => h('option', { value: o }, o)));
    el.value = value ?? '';
  } else if (field.type === 'yesno') {
    el = h('select', {}, h('option', { value: '' }, '(blank)'), h('option', { value: 'Yes' }, 'Yes'), h('option', { value: 'No' }, 'No'));
    el.value = value ?? '';
  } else if (field.type === 'date') {
    el = h('input', { type: 'date', value: value && !String(value).startsWith('--') ? value : '' });
  } else if (field.type === 'number') {
    el = h('input', { type: 'number', step: 'any', value: value ?? '' });
  } else if (field.key === 'notes') {
    el = h('textarea', { value: value ?? '' });
  } else {
    el = h('input', { type: field.type === 'email' ? 'email' : 'text', value: value ?? '', autocomplete: 'off' });
  }
  el.dataset.field = field.key;
  return el;
}
export function readInput(el) { const v = el.value; return v === '' ? null : v; }

// Employee picker with search-as-you-type. onPick({id, full_name, rhc_id, location}).
export function employeePicker(onPick, placeholder = 'Search name or RHC ID...', includeLeft = false) {
  const input = h('input', { type: 'search', placeholder, autocomplete: 'off' });
  const results = h('div', { class: 'results hidden' });
  let timer = null;
  let items = [];
  let hl = -1;
  const render = () => {
    mount(results, items.length ? items.map((it, i) => h('div', {
      class: i === hl ? 'hl' : '', onmousedown: (e) => { e.preventDefault(); choose(it); },
    }, `${it.full_name} `, h('span', { class: 'faint' }, `${it.rhc_id || 'no RHC ID'} · ${it.location || 'no location'}${it.record_type === 'historical' ? ' · historical' : ''}`)))
      : h('div', { class: 'faint' }, 'No matching employees'));
    results.classList.remove('hidden');
  };
  const choose = (it) => { input.value = `${it.full_name} (${it.rhc_id || ''})`; results.classList.add('hidden'); onPick(it); };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 1) { results.classList.add('hidden'); return; }
    timer = setTimeout(async () => {
      try { items = await get(`/api/employees/lookup?q=${encodeURIComponent(q)}${includeLeft ? '&include_left=true' : ''}`); hl = -1; render(); } catch (e) { showError(e); }
    }, 180);
  });
  input.addEventListener('keydown', (e) => {
    if (results.classList.contains('hidden')) return;
    if (e.key === 'ArrowDown') { hl = Math.min(items.length - 1, hl + 1); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { hl = Math.max(0, hl - 1); render(); e.preventDefault(); }
    else if (e.key === 'Enter' && hl >= 0) { choose(items[hl]); e.preventDefault(); }
    else if (e.key === 'Escape') results.classList.add('hidden');
  });
  input.addEventListener('blur', () => setTimeout(() => results.classList.add('hidden'), 150));
  return h('div', { class: 'picker' }, input, results);
}

export function can(perm) { return state.me && state.me.permissions.includes(perm); }

// Drag-and-drop zone. onFiles(FileList-like array of {file, path}).
export function dropzone({ accept, multiple = true, folder = false, onFiles, label }) {
  const input = h('input', { type: 'file', class: 'hidden', accept, multiple });
  const folderInput = folder ? h('input', { type: 'file', class: 'hidden', webkitdirectory: true, multiple: true }) : null;
  const zone = h('div', { class: 'dropzone' },
    h('p', h('b', label || 'Drag and drop files here')),
    h('div', { class: 'row', style: { justifyContent: 'center' } },
      h('button', { type: 'button', class: 'primary', onclick: () => input.click() }, multiple ? 'Choose files' : 'Choose file'),
      folderInput ? h('button', { type: 'button', onclick: () => folderInput.click() }, 'Choose folder') : null),
    input, folderInput);
  const emit = (files) => { if (files.length) onFiles(files); };
  input.addEventListener('change', () => { emit(Array.from(input.files).map((f) => ({ file: f, path: f.name }))); input.value = ''; });
  if (folderInput) folderInput.addEventListener('change', () => { emit(Array.from(folderInput.files).map((f) => ({ file: f, path: f.webkitRelativePath || f.name }))); folderInput.value = ''; });
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', async (e) => {
    e.preventDefault();
    zone.classList.remove('over');
    const out = [];
    const items = e.dataTransfer.items ? Array.from(e.dataTransfer.items) : [];
    const entries = items.map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (entries.length && entries.some((en) => en.isDirectory)) {
      if (!folder) { toast('Folders are not accepted here.', 'error'); return; }
      const walk = (entry, prefix) => new Promise((resolve) => {
        if (entry.isFile) entry.file((f) => { out.push({ file: f, path: prefix + f.name }); resolve(); }, () => resolve());
        else {
          const reader = entry.createReader();
          const all = [];
          const readBatch = () => reader.readEntries(async (batch) => {
            if (!batch.length) { for (const en of all) await walk(en, prefix + entry.name + '/'); resolve(); }
            else { all.push(...batch); readBatch(); }
          }, () => resolve());
          readBatch();
        }
      });
      for (const en of entries) await walk(en, '');
    } else {
      for (const f of Array.from(e.dataTransfer.files)) out.push({ file: f, path: f.name });
    }
    if (!multiple && out.length > 1) { toast('Drop one file at a time here.', 'error'); return; }
    emit(out);
  });
  return zone;
}

export function copyButton(getText, label = 'Copy') {
  return h('button', { class: 'small', type: 'button', onclick: async () => {
    const text = getText();
    try { await navigator.clipboard.writeText(text); toast('Copied to clipboard.', 'ok'); }
    catch (e) {
      const ta = h('textarea', { value: text });
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); toast('Copied to clipboard.', 'ok'); } catch (e2) { toast('Copy failed; select the text and press Ctrl+C.', 'error'); }
      ta.remove();
    }
  } }, label);
}

export function tabs(defs, onChange, active) {
  const bar = h('div', { class: 'tabs', role: 'tablist' });
  const set = (key) => {
    for (const b of bar.children) b.classList.toggle('active', b.dataset.key === key);
    onChange(key);
  };
  for (const d of defs) {
    bar.appendChild(h('button', { type: 'button', dataset: { key: d.key }, onclick: () => set(d.key) }, d.label,
      d.count !== undefined ? h('span', { class: 'count' }, ` ${d.count}`) : null));
  }
  setTimeout(() => set(active || defs[0].key), 0);
  return bar;
}
