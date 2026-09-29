// Import Data: upload -> mapping -> preview -> resolve -> commit -> results; history and undo.
import { del, download, get, post, qs, upload } from '../api.js';
import {
  busy, can, confirmDialog, dropzone, empty, errorText, fmtDateTime, h, loading, modal, mount, notice, pill,
  promptDialog, showError, state, tabs, toast,
} from '../ui.js';

let w = null; // wizard state

export function render(container) {
  const main = h('div');
  mount(container,
    h('div', { class: 'page-head' }, h('div', h('h1', 'Import Data'), h('div', { class: 'sub' }, 'Bring in HR spreadsheets (.xlsx or .csv). Nothing changes until you review the preview and confirm.'))),
    tabs([{ key: 'new', label: 'New import' }, { key: 'history', label: 'Import history' }, { key: 'samples', label: 'Templates and samples' }],
      (k) => (k === 'new' ? startWizard(main) : k === 'history' ? renderHistory(main) : renderSamples(main))),
    main);
}

// ------------------------------------------------------------------ wizard

async function startWizard(main) {
  mount(main, loading());
  let meta, templates;
  try { [meta, templates] = await Promise.all([get('/api/imports/meta'), get('/api/import-templates')]); } catch (e) { mount(main, notice('error', errorText(e))); return; }
  w = { main, meta, templates, step: 1, batch: null, inspect: null, config: null, preview: null, decisions: {}, deactivate: new Set() };
  drawStep1();
}

function steps() {
  const names = ['Upload', 'Map columns', 'Preview', 'Resolve and confirm', 'Results'];
  return h('div', { class: 'steps' }, names.map((n, i) => h('div', { class: i + 1 < w.step ? 'done' : i + 1 === w.step ? 'current' : '' }, `${i + 1}. ${n}`)));
}

function purposeSelect(value) {
  const sel = h('select', {}, h('option', { value: '' }, 'Choose what this file is...'),
    Object.entries(w.meta.purposes).filter(([k]) => k !== 'roster' || w.meta.can_roster).map(([k, v]) => h('option', { value: k }, v)));
  sel.value = value || '';
  return sel;
}

const PURPOSE_HELP = {
  historical: 'Past employee data. Rows that match nobody become historical-only records outside the active directory. Rows that match current employees only fill approved blank fields.',
  roster: 'The complete list of current employees for the chosen locations. Matched employees become Active, new rows are added, and people missing from the file are listed so you can decide whether to mark them Left.',
  partial: 'Updates some employees. Nobody is deactivated. Unmatched rows are reported (or created, if you choose).',
  designation: 'Updates only the Designation column of matched employees.',
  selected: 'Updates only the fields you choose below.',
};

function drawStep1() {
  w.step = 1;
  const purpose = purposeSelect(w.purpose);
  const help = h('p', { class: 'muted small' });
  purpose.addEventListener('change', () => { help.textContent = PURPOSE_HELP[purpose.value] || ''; w.purpose = purpose.value; });
  help.textContent = PURPOSE_HELP[purpose.value] || '';
  const status = h('div');
  const zone = dropzone({ accept: '.xlsx,.csv', multiple: false, label: 'Drag an .xlsx or .csv file here', onFiles: async (files) => {
    if (!purpose.value) { toast('Choose what the file is for first.', 'error'); return; }
    w.purpose = purpose.value;
    const fd = new FormData();
    fd.append('file', files[0].file);
    const bar = h('div', { class: 'progress' }, h('div'));
    mount(status, h('p', `Uploading ${files[0].file.name}...`), bar);
    try {
      w.batch = await upload('/api/imports/upload', fd, (p) => { bar.firstChild.style.width = `${Math.round(p * 100)}%`; });
      w.sheet = w.batch.sheets[0];
      w.headerRow = 1;
      await drawStep2();
    } catch (e) { mount(status, notice('error', errorText(e))); }
  } });
  mount(w.main, steps(), h('div', { class: 'card' },
    h('h2', 'What are you importing?'), purpose, help, h('div', { class: 'spacer' }), zone, status,
    h('p', { class: 'small muted' }, 'Formulas and macros in the workbook are never run; for formula cells the value Excel last saved is used and marked in the preview. Format ID columns as Text in Excel to keep leading zeros.')));
}

async function loadInspect() {
  w.inspect = await get(`/api/imports/${w.batch.batch_id}/inspect` + qs({ sheet: w.sheet, header_row: w.headerRow }));
}

async function drawStep2(templateToApply) {
  w.step = 2;
  mount(w.main, steps(), loading('Reading the spreadsheet...'));
  try { await loadInspect(); } catch (e) { mount(w.main, steps(), notice('error', errorText(e)), h('button', { onclick: drawStep1 }, 'Back')); return; }
  const ins = w.inspect;
  const cfg = w.config && w.config.batch_id === w.batch.batch_id ? w.config : {
    batch_id: w.batch.batch_id, purpose: w.purpose, mapping: { ...ins.suggested_mapping },
    options: { match_by_email: false, clear_blank_fields: [], selected_fields: [], create_missing: false, historical_overwrite: false },
    scope: [...w.meta.locations].filter((l) => state.me.locations.includes(l)), template_id: null,
  };
  if (templateToApply) {
    cfg.purpose = templateToApply.purpose;
    w.purpose = cfg.purpose;
    cfg.template_id = templateToApply.id;
    cfg.options = { ...cfg.options, ...templateToApply.options };
    if (templateToApply.options.scope && templateToApply.options.scope.length) cfg.scope = templateToApply.options.scope;
    cfg.mapping = {};
    for (const hname of ins.headers) cfg.mapping[hname] = templateToApply.mapping[hname] || '';
  } else {
    for (const hname of ins.headers) if (!(hname in cfg.mapping)) cfg.mapping[hname] = ins.suggested_mapping[hname] || '';
  }
  w.config = cfg;

  const sheetSel = h('select', { onchange: (e) => { if (e.target.value === w.sheet) return; w.sheet = e.target.value; setTimeout(() => drawStep2(), 0); } }, ins.sheets.map((s) => h('option', { value: s }, s)));
  sheetSel.value = ins.sheet;
  const headerInput = h('input', { type: 'number', min: 1, max: 25, value: ins.header_row, style: { width: '80px' }, onchange: (e) => { const v = Number(e.target.value) || 1; if (v === w.headerRow) return; w.headerRow = v; setTimeout(() => drawStep2(), 0); } });
  const tplSel = h('select', { onchange: (e) => { const t = w.templates.find((x) => String(x.id) === e.target.value); if (t) drawStep2(t); } },
    h('option', { value: '' }, w.templates.length ? 'Apply a saved template...' : 'No saved templates yet'), w.templates.map((t) => h('option', { value: t.id }, `${t.name} (${w.meta.purposes[t.purpose]})`)));
  if (cfg.template_id) tplSel.value = String(cfg.template_id);

  const purpose = purposeSelect(cfg.purpose);
  purpose.addEventListener('change', () => { if (cfg.purpose === purpose.value) return; cfg.purpose = purpose.value; w.purpose = purpose.value; setTimeout(() => drawStep2(), 0); });

  // Preview of the raw sheet with the header row highlighted.
  const rawTable = h('div', { class: 'table-wrap', style: { maxHeight: '220px' } }, h('table', { class: 'simple sheet-preview' },
    h('tbody', ins.rows.slice(0, 12).map((r, i) => h('tr', { class: i + 1 === ins.header_row ? 'header-row' : '' }, h('td', { class: 'faint' }, i + 1), r.map((c) => h('td', { title: c }, c)))))));

  const targets = w.meta.targets;
  const mapRows = ins.headers.map((hname) => {
    const sel = h('select', { onchange: (e) => { cfg.mapping[hname] = e.target.value; } },
      h('option', { value: '' }, 'Ignore this column'), targets.map((t) => h('option', { value: t.key }, t.label + (t.custom ? ' (custom)' : ''))));
    sel.value = cfg.mapping[hname] || '';
    const col = ins.headers.indexOf(hname);
    const sample = ins.rows.slice(ins.header_row, ins.header_row + 3).map((r) => r[col]).filter((x) => x).join(' | ');
    return h('tr', h('td', h('b', hname)), h('td', sel), h('td', { class: 'faint small' }, sample));
  });

  const opts = cfg.options;
  const scopeBoxes = w.meta.locations.map((l) => h('label', { class: 'field inline' }, h('input', { type: 'checkbox', checked: cfg.scope.includes(l), disabled: !state.me.locations.includes(l), onchange: (e) => {
    cfg.scope = e.target.checked ? [...new Set([...cfg.scope, l])] : cfg.scope.filter((x) => x !== l);
  } }), l));
  const fieldChecks = (key, list) => h('div', { class: 'row' }, targets.filter((t) => !t.key.startsWith('_') && t.key !== 'rhc_id').map((t) => h('label', { class: 'field inline small' },
    h('input', { type: 'checkbox', checked: (opts[key] || []).includes(t.key), onchange: (e) => {
      const set = new Set(opts[key] || []);
      if (e.target.checked) set.add(t.key); else set.delete(t.key);
      opts[key] = [...set];
    } }), t.label)));
  const options = h('div', { class: 'stack' },
    h('div', h('b', 'Location scope '), h('span', { class: 'muted small' }, cfg.purpose === 'roster' ? 'The roster defines who is active in these locations only.' : 'Rows for other locations are skipped.'), h('div', { class: 'row' }, scopeBoxes)),
    h('label', { class: 'field inline' }, h('input', { type: 'checkbox', checked: opts.match_by_email, onchange: (e) => { opts.match_by_email = e.target.checked; } }),
      'Also match by official email when a row has no RHC ID match (exact, unique emails only)'),
    cfg.purpose === 'partial' ? h('label', { class: 'field inline' }, h('input', { type: 'checkbox', checked: opts.create_missing, onchange: (e) => { opts.create_missing = e.target.checked; } }),
      'Create new employees for rows that match nobody') : null,
    cfg.purpose === 'historical' ? h('label', { class: 'field inline' }, h('input', { type: 'checkbox', checked: opts.historical_overwrite, onchange: (e) => { opts.historical_overwrite = e.target.checked; } }),
      'For current employees, allow approved historical fields to overwrite existing (non-blank) values') : null,
    cfg.purpose === 'selected' ? h('div', h('b', 'Fields to update'), fieldChecks('selected_fields')) : null,
    ['partial', 'selected', 'roster'].includes(cfg.purpose) ? h('details', h('summary', 'Blank cells (advanced)'),
      h('p', { class: 'small muted' }, 'By default a blank cell never erases an existing value. Tick a field only if blank cells in that column should clear it.'), fieldChecks('clear_blank_fields')) : null);

  const next = h('button', { class: 'primary' }, 'Preview changes');
  next.addEventListener('click', () => busy(next, () => runPreview()));
  const saveTpl = h('button', { onclick: saveTemplate }, 'Save as template');
  mount(w.main, steps(),
    h('div', { class: 'card' },
      h('h2', `File: ${w.batch.filename}`),
      w.batch.repeats && w.batch.repeats.length ? notice('warn', h('b', 'This exact file was imported before: '), w.batch.repeats.map((r) => `#${r.id} on ${fmtDateTime(r.committed_at || r.created_at)} by ${r.operator} (${r.status})`).join('; '), '. You will be asked to confirm before importing it again.') : null,
      h('div', { class: 'row' }, h('label', { class: 'field' }, 'Purpose', purpose), h('label', { class: 'field' }, 'Worksheet', sheetSel), h('label', { class: 'field' }, 'Header row', headerInput), h('label', { class: 'field grow' }, 'Mapping template', tplSel)),
      h('p', { class: 'small muted' }, PURPOSE_HELP[cfg.purpose] || ''),
      h('h3', 'Sheet preview (header row highlighted)'), rawTable,
      h('h3', 'Column mapping'),
      h('div', { class: 'table-wrap' }, h('table', { class: 'simple map-table' }, h('thead', h('tr', h('th', 'Spreadsheet column'), h('th', 'Import into'), h('th', 'Sample values'))), h('tbody', mapRows))),
      h('h3', 'Options'), options,
      h('div', { class: 'spacer' }),
      h('div', { class: 'row' }, h('button', { onclick: () => cancelBatch() }, 'Cancel import'), saveTpl, h('div', { class: 'grow' }), next)));
}

async function saveTemplate() {
  const cfg = w.config;
  const existing = w.templates.find((t) => t.id === cfg.template_id);
  const name = await promptDialog('Save mapping template', 'Template name', { value: existing ? existing.name : '', required: true, help: 'Saves the purpose, worksheet, header row, column mapping, options and scope for reuse with future files.' });
  if (!name) return;
  const body = { name, purpose: cfg.purpose, sheet: w.sheet, header_row: w.headerRow, mapping: cfg.mapping, options: cfg.options, scope: cfg.scope };
  try {
    let t;
    try { t = await post('/api/import-templates', body); } catch (e) {
      if (e.status !== 409) throw e;
      if (!await confirmDialog('Overwrite template?', e.message)) return;
      t = await post('/api/import-templates', { ...body, overwrite: true });
    }
    w.templates = await get('/api/import-templates');
    cfg.template_id = t.id;
    toast(`Template "${name}" saved.`, 'ok');
  } catch (e) { showError(e); }
}

async function cancelBatch() {
  if (w.batch) { try { await post(`/api/imports/${w.batch.batch_id}/cancel`); } catch (e) { /* ignore */ } }
  startWizard(w.main);
}

function configBody() {
  const c = w.config;
  return { purpose: c.purpose, sheet: w.sheet, header_row: w.headerRow, mapping: c.mapping, options: c.options, scope: c.scope, template_id: c.template_id };
}

async function runPreview() {
  try {
    w.preview = await post(`/api/imports/${w.batch.batch_id}/preview`, { config: configBody(), decisions: w.decisions });
    w.lastDecisions = { ...w.decisions };
    w.deactivate = new Set([...w.deactivate].filter((id) => w.preview.deactivations.some((d) => d.employee_id === id)));
    drawPreview();
  } catch (e) {
    if (e.details && e.details.mapping_problems) {
      modal({ title: 'The mapping needs attention', body: h('ul', e.details.mapping_problems.map((p) => h('li', p))) });
    } else showError(e);
  }
}

function drawPreview() {
  w.step = w.preview.summary.blocking ? 3 : 4;
  const p = w.preview;
  const s = p.summary;
  const body = h('div');
  const cats = [
    { key: 'new', label: 'New records', count: s.new },
    { key: 'updates', label: 'Changes', count: s.updates },
    { key: 'unchanged', label: 'Unchanged', count: s.unchanged },
    { key: 'errors', label: 'Errors', count: s.errors },
    { key: 'duplicates', label: 'Duplicates', count: s.duplicates },
    { key: 'suggestions', label: 'Name matches to decide', count: s.suggestions },
    { key: 'unmatched', label: 'Unmatched', count: s.unmatched },
    { key: 'out_of_scope', label: 'Outside scope', count: s.out_of_scope },
    { key: 'deactivations', label: 'Missing from roster', count: s.deactivations },
    { key: 'skipped', label: 'Skipped', count: s.skipped },
    { key: 'warnings', label: 'Warnings', count: s.warnings },
  ].filter((c) => c.count || ['new', 'updates', 'errors'].includes(c.key));
  const first = s.blocking ? (s.errors ? 'errors' : s.duplicates ? 'duplicates' : 'suggestions') : (s.deactivations ? 'deactivations' : 'updates');
  const changed = JSON.stringify(w.decisions) !== JSON.stringify(w.lastDecisions || {});
  const commitBtn = h('button', { class: 'primary', disabled: !!s.blocking || changed }, 'Confirm and import');
  commitBtn.addEventListener('click', () => busy(commitBtn, commit));
  const repreview = h('button', { class: changed ? 'accent' : '', onclick: (e) => busy(e.currentTarget, runPreview) }, 'Update preview with my decisions');
  mount(w.main, steps(),
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('h2', { class: 'grow' }, `Preview: ${w.batch.filename}`), h('span', { class: 'muted' }, `${s.rows_read} data rows read · ${w.meta.purposes[p.purpose]} · scope: ${p.scope.join(', ')}`)),
      s.blocking ? notice('error', h('b', `${s.blocking} blocking issue(s). `), 'Resolve each one (skip the row, or choose "Create as new" for name matches), then update the preview. You can also fix the spreadsheet and upload it again.')
        : notice('ok', h('b', 'No blocking issues. '), 'Review the changes, then confirm. The import is applied in a single step and can be undone later if nothing has changed since.'),
      p.ignored_columns && p.ignored_columns.length ? notice('info', `Not used by this import purpose and ignored: ${p.ignored_columns.join(', ')}.`) : null,
      changed ? notice('info', h('b', 'You changed some decisions. '), 'Click "Update preview with my decisions" to check the result before importing.') : null,
      p.repeats && p.repeats.length ? notice('warn', 'This exact file (same content) was imported before. You will need to confirm to import it again.') : null,
      h('div', { class: 'row' }, h('button', { onclick: () => drawStep2() }, '‹ Back to mapping'), repreview,
        h('button', { onclick: () => download(`/api/imports/${w.batch.batch_id}/report`, undefined, 'import_issues.xlsx').catch(showError) }, 'Download issues report'), h('div', { class: 'grow' }), commitBtn),
      h('div', { class: 'spacer' }),
      tabs(cats, (k) => drawCategory(body, k), first), body));
}

function valuesSummary(v) {
  return Object.entries(v || {}).filter(([, x]) => x !== null && x !== '').map(([k, x]) => `${k}: ${x}`).join(' · ');
}

function decisionButtons(row, options) {
  const cur = w.decisions[row];
  return h('div', { class: 'row' }, options.map(([val, label]) => h('button', { class: `small ${cur === val ? 'primary' : ''}`, onclick: () => {
    if (w.decisions[row] === val) delete w.decisions[row]; else w.decisions[row] = val;
    drawPreview();
    toast('Decision noted. Click "Update preview with my decisions" to apply it.');
  } }, label)));
}

function drawCategory(body, key) {
  const p = w.preview;
  const items = p[key] || [];
  if (!items.length) { mount(body, h('p', { class: 'muted' }, 'Nothing in this category.')); return; }
  const table = (head, rows) => h('div', { class: 'table-wrap', style: { maxHeight: '460px' } }, h('table', { class: 'simple' }, h('thead', h('tr', head.map((x) => h('th', x)))), h('tbody', rows)));
  if (key === 'new') {
    mount(body, table(['Row', 'Record', 'Values'], items.map((it) => h('tr', h('td', it.row), h('td', it.record_type === 'historical' ? pill('historical only') : pill('directory', 'blue')), h('td', { class: 'small' }, valuesSummary(it.values), it.note ? h('div', { class: 'faint' }, it.note) : null)))));
  } else if (key === 'updates') {
    mount(body, table(['Row', 'Employee', 'Matched by', 'Changes'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.name} (${it.rhc_id || ''})`),
      h('td', it.match_method, it.kind === 'promote' ? h('div', pill('moves into directory', 'purple')) : it.kind === 'enrich' ? h('div', pill('enrich blanks', 'purple')) : null),
      h('td', h('ul', { class: 'change-list' }, it.changes.map((c) => h('li', `${c.label}: `, h('span', { class: 'old' }, c.old ?? '(blank)'), ' → ', h('span', { class: 'new' }, c.new ?? '(blank)')))))))));
  } else if (key === 'unchanged') {
    mount(body, table(['Row', 'Employee', 'Note'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.name} (${it.rhc_id || ''})`), h('td', { class: 'small muted' }, it.note || 'No differences')))));
  } else if (key === 'errors') {
    mount(body, table(['Row', 'RHC ID / name', 'Problem', 'Resolve'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.rhc_id || ''} ${it.full_name || ''}`),
      h('td', h('ul', { class: 'change-list' }, it.messages.map((m) => h('li', { class: 'field-error' }, m)))), h('td', decisionButtons(it.row, [['skip', 'Skip row']]))))));
  } else if (key === 'duplicates') {
    mount(body, table(['Row', 'RHC ID / name', 'Problem', 'Resolve'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.rhc_id || ''} ${it.full_name || ''}`), h('td', { class: 'field-error' }, it.message), h('td', decisionButtons(it.row, [['skip', 'Skip this row']]))))));
  } else if (key === 'suggestions') {
    mount(body, h('div', { class: 'stack' }, notice('info', 'These rows would create a new record, but an existing employee has the same name. Records are never merged by name. Check the RHC IDs, then choose.'),
      table(['Row', 'Row values', 'Existing employees with this name', 'Decision'], items.map((it) => h('tr', h('td', it.row), h('td', { class: 'small' }, valuesSummary(it.values)),
        h('td', h('ul', { class: 'change-list' }, it.possible_matches.map((m) => h('li', `${m.name} · ${m.rhc_id || 'no RHC ID'} · ${m.location || ''} · ${m.status}${m.record_type === 'historical' ? ' (historical)' : ''}`)))),
        h('td', decisionButtons(it.row, [['create', 'Create as new'], ['skip', 'Skip row']])))))));
  } else if (key === 'unmatched') {
    mount(body, table(['Row', 'RHC ID / name', 'Note'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.rhc_id || ''} ${it.full_name || ''}`), h('td', { class: 'small muted' }, it.message)))));
  } else if (key === 'out_of_scope') {
    mount(body, table(['Row', 'RHC ID / name', 'Location'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.rhc_id || ''} ${it.full_name || ''}`), h('td', it.location)))));
  } else if (key === 'skipped') {
    mount(body, table(['Row', 'RHC ID / name', 'Reason', ''], items.map((it) => h('tr', h('td', it.row), h('td', `${it.rhc_id || ''} ${it.full_name || ''}`), h('td', it.reason), h('td', decisionButtons(it.row, [['skip', 'Undo skip']]))))));
  } else if (key === 'warnings') {
    mount(body, table(['Row', 'RHC ID / name', 'Warning'], items.map((it) => h('tr', h('td', it.row), h('td', `${it.rhc_id || ''} ${it.full_name || ''}`), h('td', { class: 'small' }, it.message)))));
  } else if (key === 'deactivations') {
    const all = h('input', { type: 'checkbox', checked: w.deactivate.size === items.length && items.length > 0, onchange: (e) => {
      if (e.target.checked) items.forEach((d) => w.deactivate.add(d.employee_id)); else w.deactivate.clear();
      drawCategory(body, key);
    } });
    mount(body, h('div', { class: 'stack' },
      notice('warn', h('b', 'These active employees in the roster scope are not in the file. '), 'Nobody is marked Left unless you tick them here and confirm when importing. Unticked employees stay as they are.'),
      table([all, 'Employee', 'RHC ID', 'Location', 'Status', 'Note'], items.map((d) => h('tr',
        h('td', h('input', { type: 'checkbox', checked: w.deactivate.has(d.employee_id), onchange: (e) => { if (e.target.checked) w.deactivate.add(d.employee_id); else w.deactivate.delete(d.employee_id); } })),
        h('td', d.name), h('td', d.rhc_id || ''), h('td', d.location || ''), h('td', d.status),
        h('td', d.row_problem ? h('span', { class: 'field-error small' }, 'Their row in the file has a problem or was skipped; check before marking Left.') : ''))))));
  }
}

async function commit() {
  const p = w.preview;
  const lwd = h('input', { type: 'date' });
  const n = w.deactivate.size;
  const extra = h('div', { class: 'stack' },
    h('ul', h('li', `${p.summary.new} new record(s)`), h('li', `${p.summary.updates} record(s) changed`), n ? h('li', h('b', `${n} employee(s) will be marked Left`)) : null),
    n ? h('label', { class: 'field' }, 'Last working date for those marked Left (optional, kept if already set)', lwd) : null,
    p.repeats && p.repeats.length ? notice('warn', 'This file was imported before and will be imported again.') : null);
  const ok = await confirmDialog('Confirm import', 'Apply these changes now? They are saved in one step.', { confirmText: 'Import now', extra, danger: n > 0 });
  if (!ok) return;
  try {
    const res = await post(`/api/imports/${w.batch.batch_id}/commit`, {
      config: configBody(), decisions: w.decisions, deactivate_ids: [...w.deactivate], confirm_deactivation: n > 0,
      acknowledge_repeat: !!(p.repeats && p.repeats.length), last_working_date: lwd.value || null,
    });
    drawResults(res);
  } catch (e) {
    if (e.status === 409) {
      await confirmDialog('Records changed', `${e.message}`, { confirmText: 'Refresh preview' });
      runPreview();
    } else showError(e);
  }
}

function drawResults(res) {
  w.step = 5;
  const s = res.summary;
  const line = (label, n) => (n ? h('li', `${label}: ${n}`) : null);
  mount(w.main, steps(), h('div', { class: 'card' },
    h('h2', 'Import complete'),
    notice('ok', `Import #${res.batch_id} was saved.`),
    h('ul', line('New directory employees', s.created), line('New historical-only records', s.created_historical), line('Updated', s.updated),
      line('Moved from historical into the directory', s.promoted), line('Enriched from historical data', s.enriched), line('Marked Left', s.marked_left),
      line('Unchanged', s.unchanged), line('Skipped', s.skipped), line('Unmatched (not imported)', s.unmatched), line('Outside scope (not imported)', s.out_of_scope)),
    h('div', { class: 'row' },
      h('button', { onclick: () => download(`/api/imports/${res.batch_id}/report`, undefined, 'import_issues.xlsx').catch(showError) }, 'Download issues report'),
      h('a', { class: 'btn', href: '#/employees' }, 'Go to employees'),
      h('button', { class: 'primary', onclick: () => startWizard(w.main) }, 'Import another file'))));
}

// ------------------------------------------------------------------ history

async function renderHistory(main) {
  mount(main, loading());
  let items;
  try { items = await get('/api/imports'); } catch (e) { mount(main, notice('error', errorText(e))); return; }
  if (!items.length) { mount(main, empty('No imports yet', 'Completed and cancelled imports appear here with who ran them, when, and what changed.')); return; }
  const statusColor = { committed: 'green', undone: 'amber', cancelled: '', previewed: 'blue', uploaded: 'blue', failed: 'red' };
  mount(main, h('div', { class: 'table-wrap' }, h('table', { class: 'simple' },
    h('thead', h('tr', h('th', '#'), h('th', 'File'), h('th', 'Purpose'), h('th', 'Scope'), h('th', 'Status'), h('th', 'Operator'), h('th', 'When'), h('th', 'Result'), h('th', ''))),
    h('tbody', items.map((b) => h('tr',
      h('td', b.id), h('td', b.filename, h('div', { class: 'faint small mono', title: 'SHA-256 of the file' }, b.file_hash.slice(0, 16) + '…')), h('td', b.purpose_label),
      h('td', (b.scope || []).join(', ')), h('td', pill(b.status, statusColor[b.status])), h('td', b.committed_by || b.operator),
      h('td', fmtDateTime(b.committed_at || b.created_at), b.undone_at ? h('div', { class: 'small faint' }, `undone ${fmtDateTime(b.undone_at)} by ${b.undone_by}`) : null),
      h('td', { class: 'small' }, summaryText(b.summary)),
      h('td', h('div', { class: 'row' },
        b.status === 'committed' || b.status === 'undone' ? h('button', { class: 'small', onclick: () => download(`/api/imports/${b.id}/report`, undefined, 'import_issues.xlsx').catch(showError) }, 'Issues') : null,
        b.status === 'committed' && can('imports.undo') ? h('button', { class: 'small danger', onclick: () => undoBatch(b, main) }, 'Undo') : null))))))));
}

function summaryText(s) {
  if (!s) return '';
  const parts = [];
  for (const [k, l] of [['created', 'new'], ['created_historical', 'historical'], ['updated', 'updated'], ['promoted', 'promoted'], ['enriched', 'enriched'], ['marked_left', 'marked Left']]) if (s[k]) parts.push(`${s[k]} ${l}`);
  return parts.join(', ') || (s.error ? s.error : '');
}

async function undoBatch(b, main) {
  let check;
  try { check = await get(`/api/imports/${b.id}/undo-check`); } catch (e) { showError(e); return; }
  if (check.conflicts.length) {
    modal({ title: 'Undo is not possible', size: 'wide', body: h('div', { class: 'stack' },
      notice('error', 'Some records changed after this import. Undoing it would overwrite those later changes, so nothing was undone.'),
      h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Employee'), h('th', 'Field'), h('th', 'Import set'), h('th', 'Now'), h('th', 'Changed by'), h('th', 'Why'))),
        h('tbody', check.conflicts.map((c) => h('tr', h('td', c.name), h('td', c.field || ''), h('td', c.import_value ?? ''), h('td', c.current_value ?? ''), h('td', c.changed_by ? `${c.changed_by} ${fmtDateTime(c.changed_at)}` : ''), h('td', { class: 'small' }, c.message))))))) });
    return;
  }
  if (!await confirmDialog('Undo import', `Undo import #${b.id} (${b.filename})? ${check.changes} recorded change(s) will be reverted and records it created will be removed.`, { danger: true, confirmText: 'Undo import' })) return;
  try { const r = await post(`/api/imports/${b.id}/undo`); toast(`Import undone: ${r.reverted} record(s) reverted, ${r.removed} removed.`, 'ok'); renderHistory(main); }
  catch (e) { showError(e); }
}

// ------------------------------------------------------------------ samples

async function renderSamples(main) {
  mount(main, loading());
  let samples, templates;
  try { [samples, templates] = await Promise.all([get('/api/samples'), get('/api/import-templates')]); } catch (e) { mount(main, notice('error', errorText(e))); return; }
  mount(main,
    h('div', { class: 'card' }, h('h2', 'Sample files'), h('p', { class: 'muted' }, 'Synthetic example files showing the expected layout. They contain made-up people only.'),
      h('ul', samples.map((s) => h('li', h('a', { href: '#', onclick: (e) => { e.preventDefault(); download(`/api/samples/${s.name}`, undefined, s.name).catch(showError); } }, s.name), `: ${s.description}`)))),
    h('div', { class: 'card' }, h('h2', 'Saved mapping templates'),
      templates.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Name'), h('th', 'Purpose'), h('th', 'Mapped columns'), h('th', 'Updated'), h('th', ''))),
        h('tbody', templates.map((t) => h('tr', h('td', t.name), h('td', t.purpose), h('td', { class: 'small' }, Object.entries(t.mapping).filter(([, v]) => v).map(([k, v]) => `${k} → ${v}`).join(', ')),
          h('td', fmtDateTime(t.updated_at || t.created_at)), h('td', h('button', { class: 'small danger', onclick: async () => {
            if (!await confirmDialog('Delete template', `Delete the template "${t.name}"?`, { danger: true, confirmText: 'Delete' })) return;
            try { await del(`/api/import-templates/${t.id}`); toast('Template deleted.', 'ok'); renderSamples(main); } catch (e) { showError(e); }
          } }, 'Delete'))))))) : h('p', { class: 'muted' }, 'No templates yet. Use "Save as template" on the mapping step.')));
}
