// Fetch wrapper: JSON, errors, CSRF header, downloads and uploads with progress.
let csrf = null;
export function setCsrf(token) { csrf = token; }

export class ApiError extends Error {
  constructor(status, message, details) { super(message); this.status = status; this.details = details; }
}

function headers(extra = {}, method = 'GET') {
  const h = { ...extra };
  if (method !== 'GET' && method !== 'HEAD') {
    h['X-Requested-With'] = 'RelayHub';
    if (csrf) h['X-CSRF-Token'] = csrf;
  }
  return h;
}

async function handle(res) {
  const type = res.headers.get('content-type') || '';
  let data = null;
  if (type.includes('application/json')) data = await res.json();
  else if (!res.ok) data = { error: await res.text() };
  if (!res.ok) {
    if (res.status === 401 && !location.hash.startsWith('#/login')) {
      window.dispatchEvent(new CustomEvent('relay:unauthorized', { detail: data && data.error }));
    }
    throw new ApiError(res.status, (data && data.error) || `Request failed (${res.status})`, data && data.details);
  }
  return data;
}

export async function api(method, url, body) {
  const opts = { method, headers: headers(body !== undefined ? { 'Content-Type': 'application/json' } : {}, method),
    credentials: 'same-origin' };
  if (body !== undefined) opts.body = JSON.stringify(body);
  let res;
  try { res = await fetch(url, opts); } catch (e) {
    throw new ApiError(0, 'The application is not responding. Check that Relay Employee Hub is still running.');
  }
  return handle(res);
}
export const get = (url) => api('GET', url);
export const post = (url, body = {}) => api('POST', url, body);
export const patch = (url, body) => api('PATCH', url, body);
export const put = (url, body) => api('PUT', url, body);
export const del = (url) => api('DELETE', url);

export function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    p.set(k, typeof v === 'object' ? JSON.stringify(v) : v);
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}

function filenameFrom(res, fallback) {
  const cd = res.headers.get('content-disposition') || '';
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="?([^";]+)"?/i.exec(cd);
  return m ? decodeURIComponent(m[1]) : fallback;
}

// Download a file from GET or POST and save it via a temporary link.
export async function download(url, body, fallbackName = 'download') {
  const method = body === undefined ? 'GET' : 'POST';
  const opts = { method, headers: headers(body !== undefined ? { 'Content-Type': 'application/json' } : {}, method),
    credentials: 'same-origin' };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (!res.ok) await handle(res);
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filenameFrom(res, fallbackName);
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// Multipart upload with progress callback (0..1).
export function upload(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('X-Requested-With', 'RelayHub');
    if (csrf) xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = { error: xhr.responseText }; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError(xhr.status, (data && data.error) || `Upload failed (${xhr.status})`, data && data.details));
    };
    xhr.onerror = () => reject(new ApiError(0, 'Upload failed: the application is not responding.'));
    xhr.send(formData);
  });
}
