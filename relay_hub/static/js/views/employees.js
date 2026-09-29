// Employees: Excel-like directory with inline editing, paste, filters, columns, profile and export.
import { download, get, patch, post, qs, upload } from '../api.js';
import {
  busy, can, confirmDialog, empty, errorText, fieldInput, fmtDate, fmtDateTime, fmtValue, h, imagePreview, loading,
  modal, mount, notice, pill, readInput, showError, state, tabs, toast,
} from '../ui.js';

const DEFAULT_COLUMNS = ['photo', 'rhc_id', 'full_name', 'designation', 'department', 'official_email', 'location',
  'employment_status', 'assignment_status', 'client_name', 'bench_days', 'date_of_joining'];
const VIEWS = [['active', 'Active employees'], ['current', 'Active + Pending Review'], ['pending', 'Pending Review'],
  ['left', 'Left (archived)'], ['directory', 'All directory records'], ['historical', 'Historical-only records'],
  ['everything', 'Everything']];

let g = null; // grid state for the current render

function prefsKey() { return `rhc.grid.v1.${state.me.user.id}`; }
function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(prefsKey())) || {}; } catch (e) { return {}; }
}
function savePrefs() {
  try { localStorage.setItem(prefsKey(), JSON.stringify({ columns: g.columns, widths: g.widths, pageSize: g.query.page_size })); } catch (e) { /* storage unavailable */ }
}

export function render(container, params) {
  const prefs = loadPrefs();
  const fields = state.me.fields;
  const fmap = Object.fromEntries(fields.map((f) => [f.key, f]));
  const cols = (prefs.columns || DEFAULT_COLUMNS).filter((k) => fmap[k]);
  g = {
    container, fields, fmap,
    columns: cols.length ? cols : DEFAULT_COLUMNS.filter((k) => fmap[k]),
    widths: prefs.widths || {},
    query: { view: 'active', search: '', location: '', assignment_status: '', employment_status: '', has_photo: '', filters: [], sort: 'full_name', direction: 'asc', page: 1, page_size: prefs.pageSize || 50 },
    data: null, sel: null, editing: null,
  };
  const search = h('input', { type: 'search', placeholder: 'Search name, RHC ID or email', value: '' });
  let t = null;
  search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { g.query.search = search.value; g.query.page = 1; load(); }, 250); });
  const viewSel = h('select', { title: 'Which records to show', onchange: (e) => { g.query.view = e.target.value; g.query.page = 1; load(); } },
    VIEWS.map(([k, l]) => h('option', { value: k }, l)));
  const locSel = h('select', { onchange: (e) => { g.query.location = e.target.value; g.query.page = 1; load(); } },
    h('option', { value: '' }, 'All locations'), state.me.locations.map((l) => h('option', { value: l }, l)));
  const asgSel = h('select', { onchange: (e) => { g.query.assignment_status = e.target.value; g.query.page = 1; load(); } },
    h('option', { value: '' }, 'Any assignment'), ['Client', 'Bench', 'Internal', 'Unknown'].map((l) => h('option', { value: l }, l)));
  const photoSel = can('photos.view') ? h('select', { onchange: (e) => { g.query.has_photo = e.target.value; g.query.page = 1; load(); } },
    h('option', { value: '' }, 'Photo: any'), h('option', { value: 'yes' }, 'Has photo'), h('option', { value: 'no' }, 'Missing photo')) : null;
  g.filterBtn = h('button', { onclick: openFilters }, 'More filters');
  g.gridWrap = h('div', { class: 'grid-wrap', tabindex: '0' });
  g.pager = h('div', { class: 'pager' });
  g.summary = h('div', { class: 'muted small' });
  g.dash = h('div');
  mount(container,
    h('div', { class: 'page-head' }, h('div', h('h1', 'Employees'), h('div', { class: 'sub' }, 'Double-click a cell to edit. Select a cell and press Ctrl+V to paste several rows from Excel.')),
      h('div', { class: 'grow' }),
      can('employees.edit') ? h('button', { class: 'primary', onclick: () => openForm(null) }, '+ Add employee') : null),
    g.dash,
    h('div', { class: 'toolbar' }, search, viewSel, locSel, asgSel, photoSel, g.filterBtn,
      h('button', { onclick: openColumns }, 'Columns'),
      h('div', { class: 'grow' }),
      can('employees.export') ? h('button', { onclick: (e) => exportView(e.currentTarget) }, 'Export view to Excel') : null,
      h('button', { onclick: load, title: 'Reload' }, 'Refresh')),
    g.summary, g.gridWrap, g.pager);
  g.gridWrap.addEventListener('keydown', onKey);
  g.gridWrap.addEventListener('paste', onPaste);
  loadDashboard();
  load();
  if (params && params[0]) openProfile(Number(params[0]));
}

async function loadDashboard() {
  try {
    const d = await get('/api/dashboard');
    const items = [h('div', { class: 'stat' }, h('b', d.active), h('span', 'Active employees'))];
    if (d.pending_review) items.push(h('div', { class: 'stat' }, h('b', d.pending_review), h('span', 'Pending review')));
    if (can('photos.view')) items.push(h('div', { class: 'stat' }, h('b', d.missing_photos), h('span', 'Active without a photo')));
    const att = (d.attention || []).filter((a) => a.overdue || ['Needs Attention', 'Failed', 'Delivery Uncertain', 'Ready'].includes(a.status));
    if (att.length) items.push(h('div', { class: 'stat' }, h('b', att.length), h('span', h('a', { href: '#/tasks' }, 'Task periods need action'))));
    mount(g.dash, h('div', { class: 'grid3', style: { marginBottom: '14px' } }, items));
  } catch (e) { /* dashboard is optional */ }
}

async function load() {
  mount(g.gridWrap, loading('Loading employees...'));
  try {
    const q = { ...g.query, filters: g.query.filters.length ? g.query.filters : '' };
    g.data = await get('/api/employees' + qs(q));
    g.sel = null;
    drawGrid();
  } catch (e) {
    mount(g.gridWrap, notice('error', errorText(e)));
  }
  g.filterBtn.textContent = g.query.filters.length ? `More filters (${g.query.filters.length})` : 'More filters';
}

function drawGrid() {
  const { data } = g;
  const cols = g.columns.map((k) => g.fmap[k]).filter(Boolean);
  mount(g.summary, `${data.total} record${data.total === 1 ? '' : 's'}`);
  if (!data.items.length) {
    const actions = [];
    if (g.query.search || g.query.filters.length || g.query.location || g.query.assignment_status || g.query.has_photo) {
      actions.push(h('button', { onclick: () => { g.query.search = ''; g.query.filters = []; g.query.location = ''; g.query.assignment_status = ''; g.query.has_photo = ''; render(g.container, []); } }, 'Clear search and filters'));
    }
    if (can('employees.edit')) actions.push(h('button', { class: 'primary', onclick: () => openForm(null) }, 'Add an employee'));
    if (can('imports.run')) actions.push(h('a', { class: 'btn', href: '#/import' }, 'Import a spreadsheet'));
    mount(g.gridWrap, empty('No employees to show', 'Nothing matches the current view. Change the filters, add an employee, or import your HR spreadsheet.', ...actions));
    drawPager();
    return;
  }
  const colgroup = h('colgroup', cols.map((c) => h('col', { style: { width: `${g.widths[c.key] || c.width || 140}px` } })));
  const headRow = h('tr', cols.map((c, i) => {
    const th = h('th', { title: `Sort by ${c.label}`, onclick: (e) => { if (e.target.classList.contains('resizer')) return; sortBy(c.key); } },
      c.label, g.query.sort === c.key ? h('span', { class: 'sort' }, g.query.direction === 'asc' ? '▲' : '▼') : null,
      h('span', { class: 'resizer', onmousedown: (e) => startResize(e, c, colgroup.children[i]) }));
    return th;
  }));
  const body = h('tbody', data.items.map((row, r) => h('tr', cols.map((c, ci) => cell(row, c, r, ci)))));
  const total = cols.reduce((s, c) => s + (g.widths[c.key] || c.width || 140), 0);
  const table = h('table', { class: 'grid', style: { width: `${total}px` } }, colgroup, h('thead', headRow), body);
  mount(g.gridWrap, table);
  g.table = table;
  drawPager();
}

function cell(row, f, r, ci) {
  const editable = f.editable && can('employees.edit');
  const td = h('td', { class: editable ? '' : 'readonly', dataset: { r, c: ci } });
  fillCell(td, row, f);
  td.addEventListener('mousedown', () => select(r, ci));
  td.addEventListener('dblclick', () => startEdit(r, ci));
  return td;
}

function fillCell(td, row, f) {
  const v = row[f.key];
  if (f.key === 'photo') {
    mount(td, v ? h('img', { class: 'thumb', src: v.thumb, alt: '', loading: 'lazy' }) : h('span', { class: 'avatar-empty', title: 'No photo' }));
  } else if (f.key === 'full_name') {
    td.classList.add('name-cell');
    mount(td, h('a', { href: '#', onclick: (e) => { e.preventDefault(); openProfile(row.id); } }, v || '(no name)'));
  } else if (f.key === 'employment_status' && v) {
    mount(td, pill(v, v === 'Active' ? 'green' : v === 'Left' ? '' : 'amber'));
  } else {
    mount(td, fmtValue(f, v));
  }
  td.title = typeof v === 'string' ? v : '';
}

function drawPager() {
  const { total, page, page_size: size } = g.data;
  const pages = Math.max(1, Math.ceil(total / size));
  const sizeSel = h('select', { onchange: (e) => { g.query.page_size = Number(e.target.value); g.query.page = 1; savePrefs(); load(); } },
    [25, 50, 100, 200, 500].map((n) => h('option', { value: n }, `${n} per page`)));
  sizeSel.value = String(size);
  mount(g.pager,
    h('button', { class: 'small', disabled: page <= 1, onclick: () => { g.query.page = 1; load(); } }, '« First'),
    h('button', { class: 'small', disabled: page <= 1, onclick: () => { g.query.page -= 1; load(); } }, '‹ Previous'),
    h('span', { class: 'muted' }, `Page ${page} of ${pages}`),
    h('button', { class: 'small', disabled: page >= pages, onclick: () => { g.query.page += 1; load(); } }, 'Next ›'),
    h('button', { class: 'small', disabled: page >= pages, onclick: () => { g.query.page = pages; load(); } }, 'Last »'),
    sizeSel);
}

function sortBy(key) {
  if (key === 'photo') return;
  if (g.query.sort === key) g.query.direction = g.query.direction === 'asc' ? 'desc' : 'asc';
  else { g.query.sort = key; g.query.direction = 'asc'; }
  load();
}

function startResize(e, f, colEl) {
  e.preventDefault();
  e.stopPropagation();
  const startX = e.clientX;
  const startW = g.widths[f.key] || f.width || 140;
  const handle = e.target;
  handle.classList.add('dragging');
  const move = (ev) => {
    const w = Math.max(50, startW + ev.clientX - startX);
    g.widths[f.key] = w;
    colEl.style.width = `${w}px`;
    const cols = g.columns.map((k) => g.fmap[k]).filter(Boolean);
    g.table.style.width = `${cols.reduce((s, c) => s + (g.widths[c.key] || c.width || 140), 0)}px`;
  };
  const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); handle.classList.remove('dragging'); savePrefs(); };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

// ---------------------------------------------------------------- selection

function tdAt(r, c) { return g.table && g.table.querySelector(`td[data-r="${r}"][data-c="${c}"]`); }

function select(r, c) {
  if (g.editing) return;
  if (g.sel) { const old = tdAt(g.sel.r, g.sel.c); if (old) old.classList.remove('sel'); }
  g.sel = { r, c };
  const td = tdAt(r, c);
  if (td) { td.classList.add('sel'); td.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }
  g.gridWrap.focus({ preventScroll: true });
}

function onKey(e) {
  if (g.editing || !g.sel || !g.data) return;
  const rows = g.data.items.length;
  const colsN = g.columns.length;
  const { r, c } = g.sel;
  const moves = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], Tab: [0, e.shiftKey ? -1 : 1] };
  if (moves[e.key]) {
    e.preventDefault();
    const [dr, dc] = moves[e.key];
    select(Math.min(rows - 1, Math.max(0, r + dr)), Math.min(colsN - 1, Math.max(0, c + dc)));
  } else if (e.key === 'Enter' || e.key === 'F2') {
    e.preventDefault();
    startEdit(r, c);
  } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    startEdit(r, c, e.key);
    e.preventDefault();
  }
}

// ------------------------------------------------------------------ editing

function startEdit(r, c, initial) {
  const f = g.fmap[g.columns[c]];
  const row = g.data.items[r];
  if (!f || !f.editable || !can('employees.edit')) {
    if (f && f.key === 'date_of_birth' && f.dob_access !== 'full') toast('Your account cannot edit the full date of birth.');
    return;
  }
  select(r, c);
  const td = tdAt(r, c);
  const input = fieldInput(f, row[f.key]);
  if (f.key === 'notes') { openForm(row); return; }
  if (initial !== undefined && input.tagName === 'INPUT' && input.type !== 'date') input.value = initial;
  g.editing = { r, c, td };
  td.classList.add('editing');
  mount(td, input);
  input.focus();
  if (input.select && initial === undefined && input.type !== 'date') input.select();
  let finished = false;
  const finish = async (save, move) => {
    if (finished) return;
    finished = true;
    input.removeEventListener('blur', onBlur);
    const value = readInput(input);
    td.classList.remove('editing');
    g.editing = null;
    const current = row[f.key] ?? null;
    if (!save || String(value ?? '') === String(current ?? '')) {
      fillCell(td, row, f);
      if (move) moveAfter(move);
      else select(r, c);
      return;
    }
    fillCell(td, { ...row, [f.key]: value }, f);
    td.classList.add('saving');
    await saveCell(row, f, value, td);
    td.classList.remove('saving');
    if (move) moveAfter(move); else select(r, c);
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true, 'down'); }
    else if (e.key === 'Tab') { e.preventDefault(); finish(true, e.shiftKey ? 'left' : 'right'); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    e.stopPropagation();
  });
  const onBlur = () => finish(true);
  input.addEventListener('blur', onBlur);
  if (input.tagName === 'SELECT') input.addEventListener('change', () => finish(true));
}

function moveAfter(dir) {
  const { r, c } = g.sel || {};
  if (r === undefined) return;
  const d = { down: [1, 0], right: [0, 1], left: [0, -1] }[dir];
  select(Math.min(g.data.items.length - 1, Math.max(0, r + d[0])), Math.min(g.columns.length - 1, Math.max(0, c + d[1])));
}

async function saveCell(row, f, value, td) {
  try {
    const updated = await patch(`/api/employees/${row.id}`, { version: row.version, changes: { [f.key]: value } });
    Object.assign(row, updated);
    refreshRow(row);
    toast(`Saved ${f.label} for ${row.full_name}.`, 'ok');
  } catch (e) {
    if (e.status === 409 && e.details && e.details.current) {
      const cur = e.details.current;
      const choice = await conflictDialog(row, f, value, cur);
      Object.assign(row, cur);
      if (choice === 'mine') {
        await saveCell(row, f, value, td);
        return;
      }
      refreshRow(row);
    } else {
      td.classList.add('err');
      fillCell(td, row, f);
      setTimeout(() => td.classList.remove('err'), 2500);
      showError(e);
    }
  }
}

function refreshRow(row) {
  const r = g.data.items.indexOf(row);
  g.columns.forEach((k, c) => { const td = tdAt(r, c); if (td && !td.classList.contains('editing')) fillCell(td, row, g.fmap[k]); });
}

function conflictDialog(row, f, mine, current) {
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title: 'Someone else changed this record',
      body: h('div', { class: 'stack' },
        h('p', `${row.full_name} was updated by ${current.updated_by || 'another user'} at ${fmtDateTime(current.updated_at)} after you loaded the page. Nothing was overwritten.`),
        h('dl', { class: 'kv' }, h('dt', `${f.label} now`), h('dd', fmtValue(f, current[f.key]) || '(blank)'), h('dt', 'Your value'), h('dd', fmtValue(f, mine) || '(blank)'))),
      actions: [
        h('button', { onclick: () => { done = true; m.close(); resolve('theirs'); } }, 'Keep their value'),
        h('button', { class: 'primary', onclick: () => { done = true; m.close(); resolve('mine'); } }, 'Apply my value'),
      ],
      onClose: () => { if (!done) resolve('theirs'); },
    });
  });
}

// -------------------------------------------------------------------- paste

async function onPaste(e) {
  if (g.editing || !g.sel || !can('employees.edit')) return;
  const text = (e.clipboardData || window.clipboardData).getData('text');
  if (!text) return;
  e.preventDefault();
  const lines = text.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n').map((l) => l.split('\t'));
  const rows = [];
  const skipped = [];
  lines.forEach((cells, i) => {
    const row = g.data.items[g.sel.r + i];
    if (!row) { skipped.push(i + 1); return; }
    const changes = {};
    cells.forEach((v, j) => {
      const f = g.fmap[g.columns[g.sel.c + j]];
      if (!f) return;
      if (!f.editable) return;
      changes[f.key] = v.trim() === '' ? null : v.trim();
    });
    rows.push({ id: row.id, version: row.version, row: g.sel.r + i + 1, changes });
  });
  const readonly = [];
  lines[0].forEach((_, j) => { const f = g.fmap[g.columns[g.sel.c + j]]; if (f && !f.editable) readonly.push(f.label); });
  let result;
  try { result = await post('/api/employees/paste/validate', { rows }); } catch (err) { showError(err); return; }
  const body = h('div', { class: 'stack' },
    h('p', `${lines.length} pasted row(s) starting at "${g.fmap[g.columns[g.sel.c]].label}" on row ${g.sel.r + 1}. Blank pasted cells clear the value.`),
    skipped.length ? notice('warn', `${skipped.length} pasted row(s) go beyond the rows on this page and were ignored. Use Import Data to add new employees.`) : null,
    readonly.length ? notice('warn', `Read-only columns were ignored: ${readonly.join(', ')}.`) : null,
    result.blocking ? notice('error', `${result.blocking} row(s) have errors. Fix them in Excel and paste again; nothing will be saved until all rows are valid.`) : null,
    h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Row'), h('th', 'Employee'), h('th', 'Changes'), h('th', 'Problems'))),
      h('tbody', result.rows.map((r) => h('tr',
        h('td', r.row), h('td', r.name || ''),
        h('td', r.changes.length ? h('ul', { class: 'change-list' }, r.changes.map((c) => h('li', `${c.label}: `, h('span', { class: 'old' }, fmtValue(g.fmap[c.field], c.old) || '(blank)'), ' → ', h('span', { class: 'new' }, fmtValue(g.fmap[c.field], c.new) || '(blank)')))) : h('span', { class: 'faint' }, 'No change')),
        h('td', Object.keys(r.errors).length ? h('ul', { class: 'change-list' }, Object.entries(r.errors).map(([k, v]) => h('li', { class: 'field-error' }, `${g.fmap[k] ? g.fmap[k].label + ': ' : ''}${v}`))) : '')))))));
  const apply = h('button', { class: 'primary', disabled: !!result.blocking || !result.change_count }, `Apply ${result.change_count} change(s)`);
  const m = modal({ title: 'Review pasted changes', size: 'wide', body, actions: [h('button', { onclick: () => m.close() }, 'Cancel'), apply] });
  apply.addEventListener('click', () => busy(apply, async () => {
    try {
      const res = await post('/api/employees/paste/apply', { rows });
      toast(`Updated ${res.updated} record(s).`, 'ok');
      m.close();
      load();
    } catch (err) { showError(err); }
  }));
}

// ------------------------------------------------------------ filters/columns

function openFilters() {
  const fields = g.fields.filter((f) => !['photo'].includes(f.key));
  const list = h('div');
  const filters = g.query.filters.map((x) => ({ ...x }));
  const ops = [['contains', 'contains'], ['equals', 'equals'], ['not_equals', 'is not'], ['empty', 'is blank'], ['not_empty', 'is not blank'], ['before', 'before / less than'], ['after', 'after / greater than']];
  const draw = () => mount(list, filters.length ? filters.map((flt, i) => {
    const fs = h('select', { onchange: (e) => { flt.field = e.target.value; } }, fields.map((f) => h('option', { value: f.key }, f.label)));
    fs.value = flt.field;
    const os = h('select', { onchange: (e) => { flt.op = e.target.value; draw(); } }, ops.map(([k, l]) => h('option', { value: k }, l)));
    os.value = flt.op;
    const val = h('input', { type: 'text', value: flt.value || '', placeholder: 'value (dates as YYYY-MM-DD)', oninput: (e) => { flt.value = e.target.value; } });
    return h('div', { class: 'filter-row' }, fs, os, ['empty', 'not_empty'].includes(flt.op) ? null : val,
      h('button', { class: 'small danger', onclick: () => { filters.splice(i, 1); draw(); } }, 'Remove'));
  }) : h('p', { class: 'muted' }, 'No extra filters. Add one below.'));
  draw();
  const m = modal({
    title: 'Filters', size: 'wide',
    body: h('div', { class: 'stack' }, h('p', { class: 'muted' }, 'All filters must match. These apply on top of the search box and the dropdowns.'), list,
      h('div', h('button', { onclick: () => { filters.push({ field: 'department', op: 'contains', value: '' }); draw(); } }, '+ Add filter'))),
    actions: [
      h('button', { onclick: () => { g.query.filters = []; g.query.page = 1; m.close(); load(); } }, 'Clear all'),
      h('button', { class: 'primary', onclick: () => { g.query.filters = filters.filter((f) => f.field); g.query.page = 1; m.close(); load(); } }, 'Apply filters'),
    ],
  });
}

function openColumns() {
  const order = [...g.columns, ...g.fields.map((f) => f.key).filter((k) => !g.columns.includes(k))];
  const chosen = new Set(g.columns);
  const list = h('div', { class: 'stack' });
  const draw = () => mount(list, order.map((k, i) => {
    const f = g.fmap[k];
    if (!f) return null;
    const cb = h('input', { type: 'checkbox', checked: chosen.has(k), onchange: (e) => { if (e.target.checked) chosen.add(k); else chosen.delete(k); } });
    return h('div', { class: 'row' }, h('label', { class: 'field inline grow' }, cb, f.label, f.custom ? pill('custom', 'purple') : null),
      h('button', { class: 'small', disabled: i === 0, title: 'Move up', onclick: () => { [order[i - 1], order[i]] = [order[i], order[i - 1]]; draw(); } }, '↑'),
      h('button', { class: 'small', disabled: i === order.length - 1, title: 'Move down', onclick: () => { [order[i + 1], order[i]] = [order[i], order[i + 1]]; draw(); } }, '↓'));
  }));
  draw();
  const m = modal({
    title: 'Choose columns', body: h('div', { class: 'stack' }, h('p', { class: 'muted' }, 'Tick the columns to show and set their order. Drag the edge of a column heading to resize it. Exports use the same columns.'), list),
    actions: [
      h('button', { onclick: () => { g.columns = DEFAULT_COLUMNS.filter((k) => g.fmap[k]); g.widths = {}; savePrefs(); m.close(); drawGrid(); } }, 'Reset to default'),
      h('button', { class: 'primary', onclick: () => { g.columns = order.filter((k) => chosen.has(k)); if (!g.columns.length) { toast('Choose at least one column.', 'error'); return; } savePrefs(); m.close(); drawGrid(); } }, 'Apply'),
    ],
  });
}

async function exportView(btn) {
  await busy(btn, async () => {
    try {
      await download('/api/employees/export', { query: g.query, columns: g.columns.filter((k) => k !== 'photo') }, 'employees.xlsx');
      toast('Excel file downloaded.', 'ok');
    } catch (e) { showError(e); }
  });
}

// ------------------------------------------------------------ add/edit form

const GROUPS = [
  ['Identity', ['rhc_id', 'full_name', 'official_email', 'designation', 'department', 'location']],
  ['Employment', ['employment_status', 'date_of_joining', 'date_of_birth', 'last_working_date']],
  ['Assignment', ['assignment_status', 'client_name', 'assignment_start', 'assignment_end', 'bench_start', 'bench_end']],
];

export function openForm(row, onSaved) {
  const editable = g ? g.fields : state.me.fields;
  const fmap = Object.fromEntries(editable.map((f) => [f.key, f]));
  const inputs = {};
  const errs = {};
  const block = (keys) => h('div', { class: 'grid2' }, keys.map((k) => {
    const f = fmap[k];
    if (!f) return null;
    if (!row && !f.editable) return null;
    const input = fieldInput(f, row ? row[k] : (k === 'employment_status' ? 'Active' : k === 'assignment_status' ? 'Unknown' : null));
    if (!f.editable) input.disabled = true;
    if (k === 'location' && !row && state.me.locations.length === 1) input.value = state.me.locations[0];
    inputs[k] = input;
    errs[k] = h('span', { class: 'field-error' });
    return h('label', { class: 'field' }, h('span', { class: f.required ? 'req' : '' }, f.label), input, errs[k]);
  }));
  const customKeys = editable.filter((f) => f.custom).map((f) => f.key);
  const general = h('div', { class: 'field-error' });
  const body = h('div', { class: 'stack' }, general,
    GROUPS.map(([title, keys]) => h('fieldset', h('legend', title), block(keys))),
    customKeys.length ? h('fieldset', h('legend', 'Additional fields'), block(customKeys)) : null,
    fmap.notes ? h('fieldset', h('legend', 'Notes'), block(['notes'])) : null,
    h('p', { class: 'small muted' }, 'Current bench days are calculated automatically: calendar days since the bench start date (0 on the start date) while the assignment status is Bench.'));
  const save = h('button', { class: 'primary' }, row ? 'Save changes' : 'Add employee');
  const m = modal({ title: row ? `Edit ${row.full_name}` : 'Add employee', size: 'wide', body, actions: [h('button', { onclick: () => m.close() }, 'Cancel'), save] });
  save.addEventListener('click', () => busy(save, async () => {
    for (const e of Object.values(errs)) e.textContent = '';
    for (const i of Object.values(inputs)) i.classList.remove('invalid');
    general.textContent = '';
    const values = {};
    for (const [k, input] of Object.entries(inputs)) {
      if (input.disabled) continue;
      const v = readInput(input);
      if (row) { if (String(v ?? '') !== String(row[k] ?? '')) values[k] = v; } else if (v !== null) values[k] = v;
    }
    try {
      let saved;
      if (row) {
        if (!Object.keys(values).length) { m.close(); return; }
        saved = await patch(`/api/employees/${row.id}`, { version: row.version, changes: values });
      } else {
        saved = await post('/api/employees', { values });
      }
      toast(row ? 'Changes saved.' : `${saved.full_name} added.`, 'ok');
      m.close();
      if (onSaved) onSaved(saved);
      if (g && g.container.isConnected) load();
    } catch (e) {
      if (e.details && e.details.fields) {
        for (const [k, v] of Object.entries(e.details.fields)) {
          if (errs[k]) { errs[k].textContent = v; inputs[k].classList.add('invalid'); } else general.textContent += ` ${v}`;
        }
        general.textContent = (e.message + general.textContent).trim();
      } else if (e.status === 409) {
        general.textContent = 'Someone else changed this record while you were editing. Close this form, reopen the profile and apply your changes again.';
      } else general.textContent = errorText(e);
    }
  }));
}

// ------------------------------------------------------------------ profile

export async function openProfile(id) {
  const m = modal({ title: 'Employee profile', size: 'wide', body: loading() });
  let data;
  try { data = await get(`/api/employees/${id}`); } catch (e) { mount(m.body, notice('error', errorText(e))); return; }
  const emp = data.employee;
  const fields = state.me.fields;
  const reload = () => { m.close(); openProfile(id); if (g && g.container.isConnected) load(); };
  m.box.querySelector('.modal-head h2').textContent = `${emp.full_name} · ${emp.rhc_id || 'no RHC ID'}`;
  const photoBox = h('div', { class: 'photo-box stack' });
  if (emp.photo) photoBox.appendChild(h('img', { src: emp.photo.file, alt: `Photo of ${emp.full_name}`, onclick: () => imagePreview(emp.photo.file, emp.full_name) }));
  else if (can('photos.view')) photoBox.appendChild(h('div', { class: 'placeholder' }, 'No photo'));
  if (can('photos.manage')) {
    const file = h('input', { type: 'file', accept: 'image/*', class: 'hidden' });
    const btn = h('button', { onclick: () => file.click() }, emp.photo ? 'Replace photo' : 'Upload photo');
    file.addEventListener('change', async () => {
      if (!file.files.length) return;
      const fd = new FormData();
      fd.append('file', file.files[0]);
      await busy(btn, async () => {
        try { await upload(`/api/employees/${id}/photo`, fd); toast('Photo saved. The previous version is kept in the history.', 'ok'); reload(); } catch (e) { showError(e); }
      });
    });
    photoBox.append(btn, file);
  }
  if (emp.photo) photoBox.appendChild(h('a', { href: `${emp.photo.file}?download=true`, class: 'small' }, 'Download original'));
  const kv = h('dl', { class: 'kv' });
  for (const f of fields) {
    if (f.key === 'photo') continue;
    const v = emp[f.key];
    kv.append(h('dt', f.label), h('dd', f.key === 'bench_days' && v !== null && v !== undefined ? `${v} day(s)` : fmtValue(f, v) || h('span', { class: 'faint' }, 'none')));
  }
  const actions = h('div', { class: 'row' });
  if (can('employees.edit')) actions.appendChild(h('button', { class: 'primary', onclick: () => { m.close(); openForm(emp, () => openProfile(id)); } }, 'Edit'));
  if (can('employees.archive') && emp.record_type === 'directory') {
    if (emp.employment_status !== 'Left') {
      actions.appendChild(h('button', { class: 'danger', onclick: async () => {
        const lwd = h('input', { type: 'date', value: emp.last_working_date || state.me.today });
        const ok = await confirmDialog('Archive employee', `Mark ${emp.full_name} as Left? The record and its history are kept and can be reactivated later.`,
          { danger: true, confirmText: 'Mark as Left', extra: h('label', { class: 'field' }, 'Last working date', lwd) });
        if (!ok) return;
        try { await post(`/api/employees/${id}/archive`, { version: emp.version, last_working_date: lwd.value || null }); toast('Employee archived.', 'ok'); reload(); } catch (e) { showError(e); }
      } }, 'Archive (mark as Left)'));
    } else {
      actions.appendChild(h('button', { onclick: async () => {
        if (!await confirmDialog('Reactivate', `Set ${emp.full_name} back to Active?`)) return;
        try { await post(`/api/employees/${id}/reactivate`, { version: emp.version }); toast('Employee reactivated.', 'ok'); reload(); } catch (e) { showError(e); }
      } }, 'Reactivate'));
    }
  }
  const tabBody = h('div');
  const tabDefs = [{ key: 'assign', label: 'Assignment and status history', count: data.history.length }, { key: 'changes', label: 'Change log', count: data.changes.length }];
  if (data.photos) tabDefs.push({ key: 'photos', label: 'Photo versions', count: data.photos.length });
  const fmap = Object.fromEntries(fields.map((f) => [f.key, f]));
  const showTab = (key) => {
    if (key === 'assign') {
      mount(tabBody, data.history.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' },
        h('thead', h('tr', h('th', 'Recorded'), h('th', 'Type'), h('th', 'Status'), h('th', 'Client'), h('th', 'From'), h('th', 'To'), h('th', 'By'))),
        h('tbody', data.history.map((x) => h('tr', h('td', fmtDateTime(x.recorded_at)), h('td', x.kind), h('td', x.kind === 'employment' ? `${x.details.from} → ${x.details.to}` : x.status || ''),
          h('td', x.client_name || ''), h('td', fmtDate(x.start_date)), h('td', fmtDate(x.end_date)), h('td', x.recorded_by || '')))))) : h('p', { class: 'muted' }, 'No earlier assignments or status changes recorded yet. Previous values are saved here whenever assignment, bench or employment status changes.'));
    } else if (key === 'changes') {
      mount(tabBody, data.changes.length ? h('div', { class: 'table-wrap', style: { maxHeight: '320px' } }, h('table', { class: 'simple' },
        h('thead', h('tr', h('th', 'When'), h('th', 'Field'), h('th', 'Old'), h('th', 'New'), h('th', 'By'), h('th', 'Source'))),
        h('tbody', data.changes.map((c) => h('tr', h('td', fmtDateTime(c.changed_at)), h('td', fmap[c.field] ? fmap[c.field].label : c.field.replace(/^_/, '')),
          h('td', fmtValue(fmap[c.field], c.old_value)), h('td', fmtValue(fmap[c.field], c.new_value)), h('td', c.changed_by || ''), h('td', c.source || '')))))) : h('p', { class: 'muted' }, 'No changes recorded.'));
    } else {
      mount(tabBody, h('div', { class: 'table-wrap' }, h('table', { class: 'simple' },
        h('thead', h('tr', h('th', ''), h('th', 'Version'), h('th', 'Original file name'), h('th', 'Size'), h('th', 'Uploaded'), h('th', 'Source'), h('th', ''))),
        h('tbody', data.photos.map((p) => h('tr',
          h('td', h('img', { src: `/api/photos/${p.id}/thumb`, class: 'thumb', style: { width: '40px', height: '40px', objectFit: 'cover', borderRadius: '4px', cursor: 'zoom-in' }, onclick: () => imagePreview(`/api/photos/${p.id}/file`, p.original_filename) })),
          h('td', `#${p.id}`, p.is_current ? h('span', ' ', pill('current', 'green')) : null), h('td', p.original_filename), h('td', `${p.width}×${p.height}`),
          h('td', `${fmtDateTime(p.uploaded_at)} by ${p.uploaded_by || ''}`), h('td', p.source || ''),
          h('td', !p.is_current && can('photos.manage') ? h('button', { class: 'small', onclick: async () => {
            if (!await confirmDialog('Use this photo version', 'Make this earlier photo the current one? Packages will treat this as a changed headshot.')) return;
            try { await post(`/api/photos/${p.id}/make-current`); toast('Photo version restored.', 'ok'); reload(); } catch (e) { showError(e); }
          } }, 'Make current') : '')))))));
    }
  };
  mount(m.body, h('div', { class: 'profile' }, photoBox, h('div', { class: 'stack' }, actions, kv)),
    h('div', { class: 'spacer' }), tabs(tabDefs, showTab), tabBody);
}
