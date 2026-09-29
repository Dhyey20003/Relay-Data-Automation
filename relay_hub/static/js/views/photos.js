// Photos: upload batches (images, folders, ZIPs, PDFs, mapping CSV), review and matching, PDF cropping, missing photos.
import { download, get, post, upload } from '../api.js';
import {
  busy, can, confirmDialog, dropzone, empty, employeePicker, errorText, fmtDateTime, h, imagePreview, loading, modal,
  mount, notice, pill, showError, tabs, toast,
} from '../ui.js';

const FLAG_TEXT = {
  from_pdf: ['From PDF', 'purple'], manual_crop: ['Manual crop', 'purple'], duplicate_in_batch: ['Duplicate in batch', 'amber'],
  same_as_current: ['Same as current photo', 'amber'], used_by_other_employee: ['Used by another employee', 'red'],
  ambiguous_rhc: ['Several RHC IDs in name', 'amber'], full_page_image: ['Full-page scan', 'amber'], no_text_on_page: ['Scanned page (no text)', 'amber'],
};
const METHOD_TEXT = { rhc_filename: 'RHC ID in file name', mapping: 'Mapping file', manual: 'Chosen by operator', sequence: 'Confirmed sequence',
  name_suggestion: 'Name suggestion', pdf_text_rhc: 'RHC ID near photo in PDF' };

let view = null;

export function render(container) {
  const main = h('div');
  const defs = [];
  if (can('photos.manage')) defs.push({ key: 'upload', label: 'Upload and review' });
  defs.push({ key: 'missing', label: 'Missing photos' });
  if (can('photos.manage')) defs.push({ key: 'batches', label: 'Upload history' });
  mount(container,
    h('div', { class: 'page-head' }, h('div', h('h1', 'Photos'), h('div', { class: 'sub' }, 'Upload headshots, check every match, then save them to employee records. Facial recognition is never used.'))),
    tabs(defs, (k) => (k === 'upload' ? openCurrentBatch(main) : k === 'missing' ? renderMissing(main) : renderBatches(main))), main);
}

async function openCurrentBatch(main) {
  mount(main, loading());
  try {
    const batches = await get('/api/photo-batches');
    const open = batches.find((b) => b.status === 'open');
    if (open) return renderBatch(main, open.id);
    mount(main, empty('Start a photo upload', 'Upload single images, several images, a whole folder, ZIP files of images, or PDFs. You review every match before anything is saved.',
      h('button', { class: 'primary', onclick: async (e) => busy(e.currentTarget, async () => { try { const b = await post('/api/photo-batches'); renderBatch(main, b.id); } catch (err) { showError(err); } }) }, 'Start new upload')));
  } catch (e) { mount(main, notice('error', errorText(e))); }
}

async function renderBatch(main, batchId, filter = 'all') {
  mount(main, loading('Loading photo batch...'));
  let data;
  try { data = await get(`/api/photo-batches/${batchId}`); } catch (e) { mount(main, notice('error', errorText(e))); return; }
  view = { main, batchId, data, filter };
  const open = data.batch.status === 'open';
  const status = h('div');
  const zone = open ? dropzone({ accept: 'image/*,.zip,.pdf,.csv', folder: true, label: 'Drag images, a folder, ZIP files, PDFs or a mapping CSV here', onFiles: async (files) => {
    const fd = new FormData();
    for (const f of files) { fd.append('files', f.file, f.file.name); fd.append('relpaths', f.path); }
    const bar = h('div', { class: 'progress' }, h('div'));
    mount(status, h('p', `Uploading ${files.length} file(s)... Large ZIPs and PDFs take a moment to process after upload.`), bar);
    try {
      const rep = await upload(`/api/photo-batches/${batchId}/upload`, fd, (p) => { bar.firstChild.style.width = `${Math.round(p * 100)}%`; });
      const msgs = [`${rep.added} image(s) added`];
      if (rep.invalid) msgs.push(`${rep.invalid} file(s) rejected (see "Rejected" below)`);
      if (rep.pdf_pages) msgs.push(`${rep.pdf_pages} PDF page(s) rendered`);
      if (rep.mappings) msgs.push(`${rep.mappings} mapping(s) applied`);
      toast(msgs.join(', ') + '.', 'ok');
      await renderBatch(main, batchId, view.filter);
      if (rep.messages && rep.messages.length) modal({ title: 'Upload notes', body: h('ul', rep.messages.map((m) => h('li', m))) });
    } catch (e) { mount(status, notice('error', errorText(e))); }
  } }) : null;

  const items = data.items;
  const counts = { all: items.length, pending: 0, ready: 0, skipped: 0, invalid: 0, committed: 0 };
  for (const it of items) counts[it.status] = (counts[it.status] || 0) + 1;
  const filterBar = h('div', { class: 'row' }, [['all', 'All'], ['pending', 'Needs decision'], ['ready', 'Ready to save'], ['skipped', 'Skipped'], ['invalid', 'Rejected'], ['committed', 'Saved']]
    .map(([k, l]) => h('button', { class: `small ${filter === k ? 'primary' : ''}`, onclick: () => { view.filter = k; drawItems(); } }, `${l} (${counts[k] || 0})`)));
  view.itemsEl = h('div');
  view.filterBar = filterBar;
  const commitBtn = h('button', { class: 'primary', disabled: !counts.ready || !open }, `Save ${counts.ready} ready photo(s)`);
  commitBtn.addEventListener('click', () => busy(commitBtn, async () => {
    if (!await confirmDialog('Save photos', `Save ${counts.ready} photo(s) to the matched employees? Earlier photos are kept as previous versions.`, { confirmText: 'Save photos' })) return;
    try {
      const r = await post(`/api/photo-batches/${batchId}/commit`);
      toast(`${r.saved} photo(s) saved${r.unchanged ? `, ${r.unchanged} identical to the current photo were not saved again` : ''}.`, 'ok');
      renderBatch(main, batchId, 'pending');
    } catch (e) {
      if (e.details && e.details.clashes) modal({ title: 'Two photos for one employee', body: h('div', h('p', e.message), h('ul', e.details.clashes.map((c) => h('li', `${c.employee} (${c.rhc_id}): items ${c.items.join(', ')}`)))) });
      else showError(e);
    }
  }));
  const closeBtn = open ? h('button', { onclick: async () => {
    const msg = counts.pending || counts.ready ? `${counts.pending} photo(s) still need a decision and ${counts.ready} are ready but not saved. Closing discards them.` : 'Close this upload batch? Its temporary files are removed; saved photos are kept.';
    if (!await confirmDialog('Close upload batch', msg, { danger: !!(counts.pending || counts.ready), confirmText: 'Close batch' })) return;
    try { await post(`/api/photo-batches/${batchId}/close`); toast('Batch closed.', 'ok'); openCurrentBatch(main); } catch (e) { showError(e); }
  } }, 'Close batch') : null;
  const pages = data.pdf_pages;
  mount(main,
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('h2', { class: 'grow' }, `Upload batch #${batchId}`, ' ', pill(data.batch.status, open ? 'blue' : '')), h('span', { class: 'muted small' }, `Started ${fmtDateTime(data.batch.created_at)} by ${data.batch.created_by}`)),
      zone, status,
      open ? h('p', { class: 'small muted' }, 'Matching: an RHC ID in the file name (or a mapping CSV with columns filename, RHC ID) marks a photo ready. Name look-alikes are only suggestions you must confirm. File, ZIP and PDF order is never assumed to match your employee list; use "Match by sequence" to pair photos with an ordered RHC ID list explicitly.') : null),
    pages.length ? h('div', { class: 'card' }, h('h2', 'PDF pages'),
      h('p', { class: 'muted small' }, 'Open a page to see where photos were found and to crop a photo by hand when extraction was not suitable (scanned pages, photos embedded in larger images).'),
      h('div', { class: 'table-wrap', style: { maxHeight: '260px' } }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'File'), h('th', 'Page'), h('th', 'Photos found'), h('th', 'Notes'), h('th', ''))),
        h('tbody', pages.map((p) => h('tr', h('td', p.source_name), h('td', p.page_no), h('td', p.image_count),
          h('td', p.flags.map((f) => pill({ no_text: 'No text (scanned?)', no_images: 'No photos extracted', scanned: 'Scanned page', multiple_images: 'Several photos' }[f] || f, f === 'multiple_images' ? '' : 'amber'))),
          h('td', open ? h('button', { class: 'small', onclick: () => openPage(p.id) }, 'Open page / crop') : null))))))) : null,
    h('div', { class: 'card' },
      h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Review'), open ? h('button', { onclick: openSequence }, 'Match by sequence...') : null, closeBtn, commitBtn),
      filterBar, h('div', { class: 'spacer' }), view.itemsEl));
  drawItems();
}

function drawItems() {
  const { data, filter } = view;
  for (const b of view.filterBar.children) b.classList.toggle('primary', b.textContent.startsWith({ all: 'All', pending: 'Needs', ready: 'Ready', skipped: 'Skipped', invalid: 'Rejected', committed: 'Saved' }[filter]));
  const items = data.items.filter((it) => filter === 'all' || it.status === filter);
  if (!items.length) { mount(view.itemsEl, h('p', { class: 'muted' }, data.items.length ? 'No photos in this category.' : 'No files uploaded yet.')); return; }
  mount(view.itemsEl, h('div', { class: 'photo-grid' }, items.map(card)));
}

function card(it) {
  const open = view.data.batch.status === 'open';
  const editable = open && ['pending', 'ready', 'skipped'].includes(it.status);
  const statusPill = { pending: pill('Needs decision', 'amber'), ready: pill('Ready to save', 'green'), skipped: pill('Skipped'), invalid: pill('Rejected', 'red'), committed: pill('Saved', 'blue') }[it.status];
  const img = it.status === 'invalid' ? h('div', { class: 'img' }, h('span', { class: 'faint small' }, 'No preview')) :
    h('div', { class: 'img', onclick: () => imagePreview(`/api/photo-batches/items/${it.id}/image`, it.original_filename) }, h('img', { src: `/api/photo-batches/items/${it.id}/thumb`, alt: it.original_filename, loading: 'lazy' }));
  const act = async (action, employee_id) => {
    try { await post(`/api/photo-batches/items/${it.id}`, { action, employee_id }); await renderBatch(view.main, view.batchId, view.filter); } catch (e) { showError(e); }
  };
  const match = [];
  if (it.assigned_employee_id) match.push(h('div', h('b', `${it.assigned_name} (${it.assigned_rhc || 'no RHC ID'})`), h('div', { class: 'faint small' }, `${METHOD_TEXT[it.match_method] || it.match_method || ''} · ${it.assigned_location || 'no location'}`)));
  else if (it.suggested_employee_id) match.push(h('div', 'Suggested: ', h('b', `${it.suggested_name} (${it.suggested_rhc || ''})`)));
  if (it.match_note) match.push(h('div', { class: 'faint small' }, it.match_note));
  if (it.error) match.push(h('div', { class: 'field-error small' }, it.error));
  const pickerHolder = h('div');
  const buttons = editable ? h('div', { class: 'row' },
    it.suggested_employee_id && it.status === 'pending' ? h('button', { class: 'small primary', onclick: () => act('confirm') }, 'Confirm') : null,
    h('button', { class: 'small', onclick: () => mount(pickerHolder, employeePicker((emp) => act('assign', emp.id), 'Type a name or RHC ID...', true)) }, it.assigned_employee_id ? 'Change' : 'Choose employee'),
    it.status !== 'skipped' ? h('button', { class: 'small', onclick: () => act('skip') }, 'Skip') : null,
    it.status !== 'pending' ? h('button', { class: 'small', onclick: () => act('reset') }, 'Reset') : null) : null;
  return h('div', { class: `photo-card ${it.status}` }, img,
    h('div', { class: 'row' }, statusPill, (it.flags || []).map((f) => (FLAG_TEXT[f] ? pill(FLAG_TEXT[f][0], FLAG_TEXT[f][1]) : null))),
    h('div', { class: 'fname' }, it.source_name && it.source_name !== it.original_filename ? `${it.source_name}` : it.original_filename, it.width ? ` · ${it.width}×${it.height}` : ''),
    it.context_text ? h('div', { class: 'small muted', title: 'Text next to the photo in the PDF' }, `Page text: “${it.context_text.slice(0, 120)}”`) : null,
    h('div', { class: 'match' }, match), buttons, pickerHolder);
}

// ---------------------------------------------------------------- sequence

function openSequence() {
  const candidates = view.data.items.filter((it) => ['pending', 'ready'].includes(it.status));
  if (!candidates.length) { toast('There are no photos waiting for a decision.', 'error'); return; }
  const order = candidates.map((it) => it.id);
  const chosen = new Set(candidates.filter((it) => it.status === 'pending').map((it) => it.id));
  const byId = Object.fromEntries(candidates.map((it) => [it.id, it]));
  const list = h('div', { class: 'seq-list' });
  const draw = () => mount(list, order.map((id, i) => {
    const it = byId[id];
    return h('div', { class: 'seq-item' },
      h('input', { type: 'checkbox', checked: chosen.has(id), onchange: (e) => { if (e.target.checked) chosen.add(id); else chosen.delete(id); } }),
      h('img', { src: `/api/photo-batches/items/${id}/thumb`, alt: '' }),
      h('div', { class: 'grow small' }, it.source_name || it.original_filename, it.page_no ? ` (page ${it.page_no})` : ''),
      h('button', { class: 'small', disabled: i === 0, onclick: () => { [order[i - 1], order[i]] = [order[i], order[i - 1]]; draw(); } }, '↑'),
      h('button', { class: 'small', disabled: i === order.length - 1, onclick: () => { [order[i + 1], order[i]] = [order[i], order[i + 1]]; draw(); } }, '↓'));
  }));
  draw();
  const ids = h('textarea', { placeholder: 'One RHC ID per line, in the same order as the ticked photos', style: { minHeight: '280px', fontFamily: 'var(--mono)' } });
  const result = h('div');
  const confirmBox = h('input', { type: 'checkbox' });
  const applyBtn = h('button', { class: 'primary', disabled: true }, 'Apply confirmed pairs');
  const selectedOrder = () => order.filter((id) => chosen.has(id));
  const rhcList = () => ids.value.split(/[\n,;\t]+/).map((s) => s.trim()).filter(Boolean);
  const previewBtn = h('button', { class: 'accent' }, 'Preview pairs');
  previewBtn.addEventListener('click', () => busy(previewBtn, async () => {
    try {
      const r = await post(`/api/photo-batches/${view.batchId}/sequence/preview`, { item_ids: selectedOrder(), rhc_ids: rhcList() });
      confirmBox.checked = false;
      applyBtn.disabled = !!r.errors.length;
      mount(result, r.errors.length ? notice('error', h('ul', r.errors.map((e) => h('li', e)))) : notice('info', 'Check every pair below. Only apply if each photo shows the named person.'),
        h('div', { class: 'table-wrap', style: { maxHeight: '320px' } }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', '#'), h('th', 'Photo'), h('th', 'File'), h('th', 'RHC ID'), h('th', 'Employee'))),
          h('tbody', r.pairs.map((p) => h('tr', h('td', p.position), h('td', h('img', { src: `/api/photo-batches/items/${p.item_id}/thumb`, style: { width: '56px', height: '56px', objectFit: 'cover' }, alt: '' })),
            h('td', { class: 'small' }, p.source_name || p.original_filename || ''), h('td', p.rhc_id), h('td', p.name || h('span', { class: 'field-error' }, p.problem))))))),
        r.errors.length ? null : h('label', { class: 'field inline' }, confirmBox, 'I have checked every pair and they are correct'));
    } catch (e) { showError(e); }
  }));
  applyBtn.addEventListener('click', () => busy(applyBtn, async () => {
    if (!confirmBox.checked) { toast('Tick the confirmation after checking the pairs.', 'error'); return; }
    try {
      const r = await post(`/api/photo-batches/${view.batchId}/sequence/apply`, { item_ids: selectedOrder(), rhc_ids: rhcList(), confirmed: true });
      toast(`${r.assigned} photo(s) assigned by sequence.`, 'ok');
      m.close();
      renderBatch(view.main, view.batchId, 'ready');
    } catch (e) { showError(e); }
  }));
  const m = modal({ title: 'Match by sequence', size: 'xwide', body: h('div', { class: 'stack' },
    notice('warn', 'Use this only when you have an ordered list of RHC IDs that you know matches the photos. Arrange the photos in the same order (use the arrows), paste the RHC IDs, preview, and confirm each pair.'),
    h('div', { class: 'row top' }, h('div', { class: 'grow' }, h('h3', 'Photos in order'), list), h('div', { class: 'grow' }, h('h3', 'RHC IDs in the same order'), ids)),
    h('div', previewBtn), result), actions: [h('button', { onclick: () => m.close() }, 'Cancel'), applyBtn] });
}

// ---------------------------------------------------------------- PDF crop

async function openPage(pageId) {
  let page;
  try { page = await get(`/api/photo-batches/pages/${pageId}`); } catch (e) { showError(e); return; }
  const img = h('img', { src: `/api/photo-batches/pages/${pageId}/image`, alt: `Page ${page.page_no}`, draggable: 'false' });
  const wrap = h('div', { class: 'page-canvas-wrap' }, img);
  const sel = h('div', { class: 'sel-rect hidden' });
  wrap.appendChild(sel);
  let rect = null;
  const scale = () => img.naturalWidth / img.clientWidth;
  img.addEventListener('load', () => {
    for (const it of page.items) {
      if (!it.bbox) continue;
      const s = 1 / scale();
      wrap.appendChild(h('div', { class: 'img-rect', title: `Item ${it.id} (${it.status})`, style: { left: `${it.bbox[0] * s}px`, top: `${it.bbox[1] * s}px`, width: `${(it.bbox[2] - it.bbox[0]) * s}px`, height: `${(it.bbox[3] - it.bbox[1]) * s}px` } }));
    }
  });
  let start = null;
  wrap.addEventListener('mousedown', (e) => {
    const b = img.getBoundingClientRect();
    start = { x: e.clientX - b.left, y: e.clientY - b.top };
    rect = null;
    sel.classList.remove('hidden');
    Object.assign(sel.style, { left: `${start.x}px`, top: `${start.y}px`, width: '0px', height: '0px' });
    e.preventDefault();
  });
  const move = (e) => {
    if (!start) return;
    const b = img.getBoundingClientRect();
    const x = Math.min(Math.max(0, e.clientX - b.left), b.width);
    const y = Math.min(Math.max(0, e.clientY - b.top), b.height);
    rect = { x: Math.min(start.x, x), y: Math.min(start.y, y), w: Math.abs(x - start.x), h: Math.abs(y - start.y) };
    Object.assign(sel.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.w}px`, height: `${rect.h}px` });
  };
  const up = () => { start = null; };
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
  const cropBtn = h('button', { class: 'primary' }, 'Add cropped photo');
  cropBtn.addEventListener('click', () => busy(cropBtn, async () => {
    if (!rect || rect.w < 10 || rect.h < 10) { toast('Drag a rectangle around the photo first.', 'error'); return; }
    const s = scale();
    try {
      await post(`/api/photo-batches/pages/${pageId}/crop`, { rect: [rect.x * s, rect.y * s, rect.w * s, rect.h * s] });
      toast('Cropped photo added for review.', 'ok');
      sel.classList.add('hidden');
      rect = null;
      renderBatch(view.main, view.batchId, view.filter);
    } catch (e) { showError(e); }
  }));
  modal({ title: `${page.source_name} · page ${page.page_no}`, size: 'xwide',
    body: h('div', { class: 'stack' },
      h('p', { class: 'muted' }, 'Dashed boxes show photos already extracted. Drag a rectangle around a photo and click "Add cropped photo". The crop is taken from a high-resolution render of the page and added to the review list.'),
      page.flags.length ? notice('warn', page.flags.map((f) => ({ no_text: 'This page has no text layer; it may be a scan.', no_images: 'No separate photos could be extracted from this page.', scanned: 'The page is one large image (scanned); crop each photo by hand.', multiple_images: 'Several photos were found on this page; check each assignment carefully.' }[f] || f)).join(' ')) : null,
      page.text ? h('details', h('summary', 'Page text'), h('div', { class: 'copybox' }, page.text)) : null,
      wrap),
    actions: [cropBtn], onClose: () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); } });
}

// ------------------------------------------------------------ other tabs

async function renderMissing(main) {
  mount(main, loading());
  let items;
  try { items = await get('/api/photos/missing'); } catch (e) { mount(main, notice('error', errorText(e))); return; }
  mount(main, h('div', { class: 'card' },
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, `Active employees without a photo (${items.length})`),
      can('employees.export') && items.length ? h('button', { onclick: (e) => busy(e.currentTarget, () => download('/api/photos/missing.xlsx', undefined, 'missing_photos.xlsx').catch(showError)) }, 'Export to Excel') : null),
    items.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'RHC ID'), h('th', 'Name'), h('th', 'Location'), h('th', 'Designation'), h('th', 'Department'))),
      h('tbody', items.map((i) => h('tr', { class: 'clickable', onclick: () => { location.hash = `#/employees/${i.id}`; } }, h('td', i.rhc_id || ''), h('td', i.full_name), h('td', i.location || ''), h('td', i.designation || ''), h('td', i.department || '')))))) :
      h('p', { class: 'muted' }, 'Every active employee has a photo.')));
}

async function renderBatches(main) {
  mount(main, loading());
  let items;
  try { items = await get('/api/photo-batches'); } catch (e) { mount(main, notice('error', errorText(e))); return; }
  if (!items.length) { mount(main, empty('No uploads yet', 'Photo upload batches appear here.')); return; }
  mount(main, h('div', { class: 'table-wrap' }, h('table', { class: 'simple' }, h('thead', h('tr', h('th', 'Batch'), h('th', 'Started'), h('th', 'By'), h('th', 'Files'), h('th', 'Waiting'), h('th', 'Result'), h('th', 'Status'), h('th', ''))),
    h('tbody', items.map((b) => h('tr', h('td', `#${b.id}`), h('td', fmtDateTime(b.created_at)), h('td', b.created_by), h('td', b.item_count), h('td', b.pending),
      h('td', { class: 'small' }, b.summary && b.summary.saved !== undefined ? `${b.summary.saved} saved` : ''), h('td', pill(b.status, b.status === 'open' ? 'blue' : '')),
      h('td', h('button', { class: 'small', onclick: () => renderBatch(main, b.id) }, 'Open'))))))));
}
