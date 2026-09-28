/* AdVanta pipeline console (Docker stack frontend).
   Vanilla JS, no build step, no CDN. Talks to the real API through Nginx:
     /auth/*  /api/*  /clicks  — same origin, JWT via Authorization header.
   Every response's X-Served-By header is captured and surfaced in the UI. */
'use strict';

/* ---------------------------------------------------------------- utils */

const $ = (sel, root) => (root || document).querySelector(sel);
const ROOT = document.getElementById('root');

const STORE = {
  get token() { return localStorage.getItem('adv_token') || ''; },
  set token(v) { v ? localStorage.setItem('adv_token', v) : localStorage.removeItem('adv_token'); },
  get apiKey() { return localStorage.getItem('adv_api_key') || ''; },
  set apiKey(v) { v ? localStorage.setItem('adv_api_key', v) : localStorage.removeItem('adv_api_key'); },
};

let toastTimer = null;
function toast(msg, kind) {
  const t = $('#toast');
  t.hidden = false;
  t.textContent = msg;
  t.className = 'toast ' + (kind || '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4200);
}

const fmt = (n) => Number(n || 0).toLocaleString('en-US');
const hhmm = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/* ------------------------------------------------------------- api layer */

const state = {
  me: null,
  campaigns: [],
  ads: [],
  selectedAdId: '',
  window: localStorage.getItem('adv_window') || '1h',
  campaignFilter: '',
  servedBy: '',
  pollTimer: null,
  sim: null,
};

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers);
  if (opts.body !== undefined && !(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(path, {
    method: opts.method || (opts.body !== undefined ? 'POST' : 'GET'),
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const sb = res.headers.get('X-Served-By');
  if (sb) state.servedBy = sb;
  if (res.status === 401 && state.token && !path.startsWith('/auth/login')) {
    logout('Session expired — sign in again');
    throw new ApiError(401, 'Session expired');
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === 'string' ? j.detail
        : j.detail && (j.detail.error || j.detail.message) ? (j.detail.error + ': ' + (j.detail.message || (j.detail.ad_ids || j.detail.event_ids || []).join(', ')))
        : j.error || JSON.stringify(j).slice(0, 200);
    } catch (_) { /* keep statusText */ }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ---------------------------------------------------------------- charts */

function lineChart(series, color) {
  // series: [{t, clicks}] — returns an SVG string sized to its box.
  if (!series.length) return '<div class="empty">No data in this window yet — send some clicks.</div>';
  const W = 900, H = 240, P = { l: 46, r: 12, t: 12, b: 26 };
  const max = Math.max(1, ...series.map((s) => s.clicks));
  const x = (i) => P.l + (i / Math.max(1, series.length - 1)) * (W - P.l - P.r);
  const y = (v) => H - P.b - (v / max) * (H - P.t - P.b);
  const pts = series.map((s, i) => x(i).toFixed(1) + ',' + y(s.clicks).toFixed(1)).join(' ');
  const ticks = [0, 0.5, 1].map((f) => ({ v: Math.round(max * f), yv: y(max * f) }));
  const xt = series.filter((_, i) => i % Math.max(1, Math.floor(series.length / 6)) === 0);
  return (
    '<svg viewBox="0 0 ' + W + ' ' + H + '" class="chart-box" preserveAspectRatio="none" style="height:240px">' +
    ticks.map((t) => '<line class="grid" x1="' + P.l + '" x2="' + (W - P.r) + '" y1="' + t.yv + '" y2="' + t.yv + '" />' +
      '<text x="4" y="' + (t.yv + 3) + '">' + fmt(t.v) + '</text>').join('') +
    xt.map((s, i) => '<text x="' + (x(i * Math.max(1, Math.floor(series.length / 6)))) + '" y="' + (H - 8) + '">' + hhmm(s.t) + '</text>').join('') +
    '<polyline fill="none" stroke="' + color + '" stroke-width="2" points="' + pts + '" />' +
    series.map((s, i) => '<circle cx="' + x(i).toFixed(1) + '" cy="' + y(s.clicks).toFixed(1) + '" r="2" fill="' + color + '" />').join('') +
    '</svg>'
  );
}

function barChart(rows, labelKey, valueKey, color) {
  if (!rows.length) return '<div class="empty">No data yet.</div>';
  const max = Math.max(1, ...rows.map((r) => Number(r[valueKey])));
  return rows.slice(0, 8).map((r) => {
    const pct = Math.round((Number(r[valueKey]) / max) * 100);
    return '<div class="row" style="margin:7px 0">' +
      '<div class="mono small" style="width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(r[labelKey]) + '">' + esc(r[labelKey]) + '</div>' +
      '<div class="grow" style="height:14px;background:var(--surface-2);border-radius:7px;overflow:hidden">' +
      '<div style="height:100%;width:' + pct + '%;background:' + color + ';border-radius:7px"></div></div>' +
      '<div class="mono small" style="width:64px;text-align:right">' + fmt(r[valueKey]) + '</div></div>';
  }).join('');
}

function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

/* ------------------------------------------------------------------ auth */

function renderAuth(tab) {
  clearInterval(state.pollTimer);
  stopSim();
  const isRegister = tab === 'register';
  ROOT.innerHTML =
    '<div class="auth-wrap"><div class="auth-card">' +
    '<div class="auth-logo">AdVANTA</div>' +
    '<div class="auth-sub">Distributed click pipeline console — Kafka · Redis · PostgreSQL</div>' +
    '<div class="auth-tabs">' +
    '<button data-tab="login" class="' + (!isRegister ? 'active' : '') + '">Sign in</button>' +
    '<button data-tab="register" class="' + (isRegister ? 'active' : '') + '">Create workspace</button>' +
    '</div><div class="auth-form">' +
    (isRegister ? '<label>Organization</label><input id="f-org" placeholder="Acme Media" maxlength="80" />' : '') +
    '<label>Email</label><input id="f-email" type="email" autocomplete="username" placeholder="you@company.com" />' +
    '<label>Password</label><input id="f-pass" type="password" autocomplete="' + (isRegister ? 'new-password' : 'current-password') + '" placeholder="' + (isRegister ? 'min 8 characters' : '') + '" />' +
    '<div class="form-error" id="f-err"></div>' +
    '<button class="primary" style="width:100%" id="f-go">' + (isRegister ? 'Create workspace' : 'Sign in') + '</button>' +
    '<div class="demo-hint">Demo seed (migration 002): <span class="mono">demo@advanta.dev</span> / <span class="mono">advanta-demo-2026</span></div>' +
    '</div></div></div>';

  ROOT.querySelectorAll('.auth-tabs button').forEach((b) =>
    b.addEventListener('click', () => renderAuth(b.dataset.tab)));
  $('#f-go').addEventListener('click', submitAuth);
  ROOT.addEventListener('keydown', function onKey(e) { if (e.key === 'Enter') { ROOT.removeEventListener('keydown', onKey); submitAuth(); } });
  $('#f-email').focus();

  async function submitAuth() {
    const err = $('#f-err');
    err.textContent = '';
    try {
      if (isRegister) {
        const r = await api('/auth/register', { body: { email: $('#f-email').value.trim(), password: $('#f-pass').value, organization: $('#f-org').value.trim() } });
        STORE.token = r.token;
      } else {
        const r = await api('/auth/login', { body: { email: $('#f-email').value.trim(), password: $('#f-pass').value } });
        STORE.token = r.token;
      }
      await enterApp();
    } catch (e) {
      err.textContent = e.message || 'Request failed — is the API running?';
    }
  }
}

function logout(msg) {
  STORE.token = '';
  state.me = null;
  toast(msg || 'Signed out', 'ok');
  renderAuth('login');
}

/* ----------------------------------------------------------------- shell */

async function enterApp() {
  state.me = await api('/auth/me');
  const [campaigns, ads] = await Promise.all([api('/api/campaigns'), api('/api/ads')]);
  state.campaigns = campaigns;
  state.ads = ads;
  state.selectedAdId = (ads[0] || {}).id || '';
  renderShell('overview');
  await refreshAll();
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(refreshAll, 5000);
}

function renderShell(page) {
  const nav = [
    ['overview', 'Overview'], ['traffic', 'Traffic'], ['campaigns', 'Campaigns & Ads'],
    ['fraud', 'Fraud'], ['infra', 'Infrastructure'], ['simulator', 'Simulator'],
  ];
  ROOT.innerHTML =
    '<div class="topbar">' +
    '<span class="brand">AdVANTA</span>' +
    '<span class="muted small">' + esc(state.me.advertiser_name || state.me.email) + '</span>' +
    '<div class="grow"></div>' +
    '<label style="margin:0">Window</label>' +
    '<select id="sel-window">' + ['1m', '10m', '1h', '1d'].map((w) => '<option ' + (w === state.window ? 'selected' : '') + '>' + w + '</option>').join('') + '</select>' +
    '<select id="sel-campaign" style="max-width:180px"><option value="">All campaigns</option>' +
    state.campaigns.map((c) => '<option value="' + esc(c.id) + '" ' + (c.id === state.campaignFilter ? 'selected' : '') + '>' + esc(c.name) + '</option>').join('') + '</select>' +
    '<span class="served-by">served by <b id="served-by">—</b></span>' +
    '<button class="compact" id="btn-logout">Sign out</button>' +
    '</div>' +
    '<div class="layout"><div class="nav">' +
    nav.map(([id, label]) => '<button data-page="' + id + '" class="' + (page === id ? 'active' : '') + '">' + label + '</button>').join('') +
    '</div><div class="content" id="content"></div></div>';

  $('#sel-window').addEventListener('change', (e) => { state.window = e.target.value; localStorage.setItem('adv_window', state.window); refreshAll(); });
  $('#sel-campaign').addEventListener('change', (e) => { state.campaignFilter = e.target.value; refreshAll(); });
  $('#btn-logout').addEventListener('click', () => logout());
  ROOT.querySelectorAll('.nav button').forEach((b) => b.addEventListener('click', () => renderShell(b.dataset.page)));
  renderPage(page);
}

function setPage(page) {
  ROOT.querySelectorAll('.nav button').forEach((b) => b.classList.toggle('active', b.dataset.page === page));
  renderPage(page);
}

let lastPage = 'overview';
function renderPage(page) {
  lastPage = page;
  const c = $('#content');
  if (page === 'overview') c.innerHTML = overviewHtml();
  else if (page === 'traffic') c.innerHTML = trafficHtml();
  else if (page === 'campaigns') c.innerHTML = campaignsHtml();
  else if (page === 'fraud') c.innerHTML = fraudHtml();
  else if (page === 'infra') c.innerHTML = infraHtml();
  else if (page === 'simulator') { c.innerHTML = simulatorHtml(); bindSimulator(); return; }
  updateServedBy();
}

function updateServedBy() {
  const el = $('#served-by');
  if (el) el.textContent = state.servedBy || '—';
}

/* ------------------------------------------------------------- page HTML */

function overviewHtml() {
  return '<div class="page-head"><h1>Overview</h1><p>Aggregated 1-minute windows from PostgreSQL + hot counters from Redis.</p></div>' +
    '<div class="grid-kpi" id="kpi">Loading…</div>' +
    '<div class="grid-2"><div class="card"><h3>Clicks over time</h3><div id="chart-series">…</div></div>' +
    '<div class="card"><h3>Pipeline (worker counters)</h3><div id="pipeline">…</div></div></div>' +
    '<div class="card" style="margin-top:16px"><h3>Recent click events (last 25 processed)</h3><div id="recent">…</div></div>';
}

function trafficHtml() {
  return '<div class="page-head"><h1>Traffic</h1><p>Breakdowns over the selected window.</p></div>' +
    '<div class="grid-3"><div class="card"><h3>Top ads</h3><div id="top-ads">…</div></div>' +
    '<div class="card"><h3>Countries</h3><div id="countries">…</div></div>' +
    '<div class="card"><h3>Devices</h3><div id="devices">…</div></div></div>' +
    '<div class="card" style="margin-top:16px"><h3>Clicks over time</h3><div id="chart-series">…</div></div>';
}

function campaignsHtml() {
  return '<div class="page-head"><h1>Campaigns & Ads</h1><p>Tenant-owned hierarchy: campaign → ad. Ads are what clicks reference.</p></div>' +
    '<div class="grid-2"><div class="card"><h3>Campaigns</h3><div id="camp-list">…</div>' +
    '<div class="row" style="margin-top:12px"><input id="new-camp" placeholder="New campaign name" /><button class="primary compact" id="add-camp">Create</button></div></div>' +
    '<div class="card"><h3>Ads</h3><div id="ad-list">…</div>' +
    '<div class="row" style="margin-top:12px"><select id="new-ad-camp" style="width:auto">' + state.campaigns.map((c) => '<option value="' + esc(c.id) + '">' + esc(c.name) + '</option>').join('') + '</select>' +
    '<input id="new-ad-title" placeholder="New ad title" /><button class="primary compact" id="add-ad">Create</button></div></div></div>';
}

function fraudHtml() {
  return '<div class="page-head"><h1>Fraud detections</h1><p>Rules: IP burst ≥100 clicks/10s · viewer ≥50 clicks on one ad/minute · campaign spike &gt;5× trailing 10-minute average with ≥200 clicks.</p></div>' +
    '<div class="card"><h3>Detections</h3><div id="fraud-list">…</div></div>';
}

function infraHtml() {
  return '<div class="page-head"><h1>Infrastructure</h1><p>Live status via the API dependency checks and Redis heartbeats (15s TTL).</p></div>' +
    '<div id="infra-body">Loading…</div>';
}

function simulatorHtml() {
  const adOpts = state.ads.map((a) => '<option value="' + esc(a.id) + '" ' + (a.id === state.selectedAdId ? 'selected' : '') + '>' + esc(a.title) + ' (' + esc(a.id) + ')</option>').join('');
  return '<div class="page-head"><h1>Click simulator</h1><p>Generates real events into the pipeline: API → Kafka → aggregator → Redis + PostgreSQL → analytics.</p></div>' +
    '<div class="card">' +
    '<div class="spread"><div class="row"><span class="muted small">Ad</span><select id="sim-ad" style="width:auto;max-width:320px">' + adOpts + '</select>' +
    '<span class="muted small">Rate</span><select id="sim-rate" style="width:auto"><option value="2000">0.5/s</option><option value="500" selected>2/s</option><option value="100">10/s</option><option value="20">50/s</option></select></div>' +
    '<span id="sim-live"></span></div>' +
    '<div class="sim-controls">' +
    '<button class="primary" id="sim-1">Send 1 click</button>' +
    '<button class="primary" id="sim-10">Send 10</button>' +
    '<button class="primary" id="sim-100">Send 100</button>' +
    '<button id="sim-dup">Duplicate ×3</button>' +
    '<button class="danger" id="sim-fraud">Fraud burst</button>' +
    '<button id="sim-start">▶ Continuous</button>' +
    '<button id="sim-stop">Stop</button>' +
    '</div>' +
    '<div class="sim-stats">' +
    '<div class="sim-stat"><div class="muted small">submitted</div><div class="v" id="s-sent">0</div></div>' +
    '<div class="sim-stat"><div class="muted small">accepted (202)</div><div class="v" id="s-ok" style="color:var(--ok)">0</div></div>' +
    '<div class="sim-stat"><div class="muted small">failed</div><div class="v" id="s-fail" style="color:var(--danger)">0</div></div>' +
    '<div class="sim-stat"><div class="muted small">dup re-sends</div><div class="v" id="s-dup" style="color:var(--warn)">0</div></div>' +
    '<div class="sim-stat"><div class="muted small">dups dropped (worker)</div><div class="v" id="s-wdup" style="color:var(--warn)">0</div></div>' +
    '<div class="sim-stat"><div class="muted small">fraud detections</div><div class="v" id="s-fraud" style="color:var(--violet)">0</div></div>' +
    '</div>' +
    '<div class="spread"><span class="muted small">Last response: <span class="mono" id="s-served">—</span></span><span class="muted small">seen by worker (stats:adv counters)</span></div>' +
    '<div class="sim-log" id="sim-log"></div>' +
    '</div>' +
    '<div class="card" style="margin-top:16px"><h3>Ingest API key</h3>' +
    '<div class="muted small" style="margin-bottom:8px">Clicks are authenticated with an X-API-Key minted per advertiser. Generate one here (shown once) or paste an existing key.</div>' +
    '<div class="row"><input id="sim-key" class="mono" placeholder="ck_…" value="' + esc(STORE.apiKey) + '" /><button class="primary compact" id="gen-key">Generate key</button><button class="compact" id="save-key">Save</button></div>' +
    '<div class="muted small" style="margin-top:8px" id="key-note"></div></div>';
}

/* ------------------------------------------------------------ data render */

let lastDash = null;

async function refreshAll() {
  if (!state.token) return;
  const params = new URLSearchParams({ window: state.window });
  if (state.campaignFilter) params.set('campaign_id', state.campaignFilter);
  try {
    const [dash, recent, fraud, infra] = await Promise.all([
      api('/api/dashboard?' + params),
      api('/api/events/recent'),
      api('/api/suspicious'),
      api('/api/infra'),
    ]);
    lastDash = dash;
    updateServedBy();
    if (lastPage === 'overview') renderOverview(dash, recent);
    else if (lastPage === 'traffic') renderTraffic(dash);
    else if (lastPage === 'fraud') renderFraud(fraud);
    else if (lastPage === 'infra') renderInfra(infra);
    if (state.sim) simRenderWorkerStats(infra, dash, fraud);
  } catch (e) {
    if (e.status !== 401) console.warn('refresh failed:', e.message);
  }
}

function renderOverview(d, recent) {
  const kpi = $('#kpi');
  if (!kpi) return;
  const pipeline = d.pipeline || {};
  const fraudTotal = (pipeline.fraud_ip_burst || 0) + (pipeline.fraud_viewer_repeat || 0) + (pipeline.fraud_campaign_spike || 0);
  kpi.innerHTML = [
    ['Total clicks', fmt(d.total_clicks), ''],
    ['Unique viewers', fmt(d.unique_viewers), ''],
    ['Clicks this minute', fmt(d.clicks_current_minute), 'accent'],
    ['Clicks last minute', fmt(d.clicks_last_minute), ''],
    ['Suspicious', fmt(d.suspicious), d.suspicious > 0 ? 'warn' : ''],
  ].map(([label, v, cls]) => '<div class="kpi ' + cls + '"><div class="muted small">' + label + '</div><div class="v">' + v + '</div></div>').join('');

  $('#chart-series').innerHTML = lineChart(d.series || [], 'var(--accent)');
  $('#pipeline').innerHTML = [
    ['processed', pipeline.processed || 0, 'ok'],
    ['duplicates dropped', pipeline.duplicates || 0, 'warn'],
    ['ip_burst flags', pipeline.fraud_ip_burst || 0, ''],
    ['viewer_repeat flags', pipeline.fraud_viewer_repeat || 0, ''],
    ['campaign_spike flags', pipeline.fraud_campaign_spike || 0, ''],
  ].map(([label, v, cls]) =>
    '<div class="row" style="margin:6px 0"><span class="grow muted small">' + label + '</span><span class="mono badge ' + cls + '">' + fmt(v) + '</span></div>').join('');

  $('#recent').innerHTML = recent.length ? table(
    ['event_id', 'ad', 'country', 'device', 'event_time', 'late', 'served_by', 'processed_by'],
    recent.map((r) => [
      r.event_id.slice(0, 24), r.ad_id, r.country, r.device, hhmm(r.event_time),
      r.is_late ? '<span class="badge warn">late</span>' : '',
      r.served_by || '', r.processed_by || '',
    ])) : '<div class="empty">No events processed yet — open the Simulator.</div>';
}

function renderTraffic(d) {
  const t = $('#top-ads');
  if (!t) return;
  $('#top-ads').innerHTML = barChart((d.top_ads || []).map((r) => ({ label: r.title || r.ad_id, clicks: r.clicks })), 'label', 'clicks', 'var(--accent)');
  $('#countries').innerHTML = barChart((d.countries || []).map((r) => ({ label: r.country, clicks: r.clicks })), 'label', 'clicks', 'var(--violet)');
  $('#devices').innerHTML = barChart((d.devices || []).map((r) => ({ label: r.device, clicks: r.clicks })), 'label', 'clicks', 'var(--ok)');
  $('#chart-series').innerHTML = lineChart(d.series || [], 'var(--accent)');
}

function renderCampaigns() {
  const cl = $('#camp-list'), al = $('#ad-list');
  if (!cl) return;
  const campCounts = {};
  (lastDash ? lastDash.campaigns : []).forEach((c) => { campCounts[c.campaign_id] = c.clicks; });
  cl.innerHTML = state.campaigns.length ? table(['id', 'name', 'status', 'clicks (window)'],
    state.campaigns.map((c) => [c.id, '<span class="wrap">' + esc(c.name) + '</span>', '<span class="badge ' + (c.status === 'active' ? 'ok' : 'warn') + '">' + esc(c.status) + '</span>', fmt(campCounts[c.id] || 0)])) : '<div class="empty">No campaigns yet — create one below.</div>';
  const byCamp = {};
  state.campaigns.forEach((c) => { byCamp[c.id] = c.name; });
  al.innerHTML = state.ads.length ? table(['id', 'title', 'campaign'],
    state.ads.map((a) => [a.id, '<span class="wrap">' + esc(a.title) + '</span>', byCamp[a.campaign_id] || a.campaign_id])) : '<div class="empty">No ads yet — create one below.</div>';

  $('#add-camp').onclick = async () => {
    const name = $('#new-camp').value.trim();
    if (!name) return toast('Enter a campaign name', 'err');
    const c = await api('/api/campaigns', { body: { name } });
    state.campaigns.unshift(c);
    $('#new-camp').value = '';
    toast('Campaign created: ' + c.id, 'ok');
    renderShell('campaigns'); renderCampaigns();
  };
  $('#add-ad').onclick = async () => {
    const campaign_id = $('#new-ad-camp').value, title = $('#new-ad-title').value.trim();
    if (!title) return toast('Enter an ad title', 'err');
    const a = await api('/api/ads', { body: { campaign_id, title } });
    state.ads.unshift(a);
    $('#new-ad-title').value = '';
    toast('Ad created: ' + a.id, 'ok');
    renderShell('campaigns'); renderCampaigns();
  };
}

function renderFraud(rows) {
  const el = $('#fraud-list');
  if (!el) return;
  const badge = { ip_burst: 'err', viewer_repeat: 'warn', campaign_spike: 'vio' };
  el.innerHTML = rows.length ? table(['rule', 'ad', 'campaign', 'count', 'window', 'details', 'detected'],
    rows.map((r) => [
      '<span class="badge ' + (badge[r.rule] || '') + '">' + esc(r.rule) + '</span>',
      r.ad_id, r.campaign_id, fmt(r.click_count), hhmm(r.window_start),
      '<span class="wrap muted small">' + esc(JSON.stringify(r.details)) + '</span>', hhmm(r.detected_at),
    ])) : '<div class="empty">No detections — try the Fraud burst in the Simulator.</div>';
}

function renderInfra(infra) {
  const el = $('#infra-body');
  if (!el) return;
  const dot = (s) => '<span class="status-dot ' + (s === 'healthy' || s === 'connected' ? 'ok' : 'err') + '"></span>';
  const parts = (infra.kafka_partitions != null ? infra.kafka_partitions : '—') + '/6';
  el.innerHTML =
    '<div class="grid-kpi">' +
    ['postgres', 'redis', 'kafka'].map((k) =>
      '<div class="kpi"><div class="muted small">' + k + '</div><div class="v" style="font-size:16px">' + dot(infra[k]) + (infra[k] || '?') + '</div>' +
      (k === 'kafka' ? '<div class="d">partitions: ' + parts + '</div>' : '') + '</div>').join('') +
    '</div>' +
    '<div class="grid-2">' +
    '<div class="card"><h3>API replicas (heartbeat)</h3>' + (infra.api_replicas.length ? table(['instance', 'last beat'],
      infra.api_replicas.map((r) => [esc(r.instance), hhmm(r.at)])) : '<div class="empty">No live heartbeats</div>') + '</div>' +
    '<div class="card"><h3>Aggregator workers (heartbeat)</h3>' + (infra.workers.length ? table(['instance', 'partitions', 'lag', 'last beat'],
      infra.workers.map((w) => [esc(w.instance), (w.partitions || []).join(',') || '—', fmt(w.lag || 0), hhmm(w.at)])) : '<div class="empty">No live heartbeats</div>') + '</div>' +
    '</div>' +
    '<div class="muted small" style="margin-top:12px">This response was served by <span class="mono">' + esc(infra.served_by) + '</span>. With <span class="mono">--scale api=3</span> you should see three instances above and changing served-by values as Nginx rotates replicas.</div>';
}

function table(cols, rows) {
  return '<table><thead><tr>' + cols.map((c) => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>' +
    rows.map((r) => '<tr>' + r.map((cell) => '<td>' + cell + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
}

/* -------------------------------------------------------------- simulator */

const COUNTRIES = ['US', 'GB', 'DE', 'FR', 'IN', 'BR', 'JP', 'CA'];
const DEVICES = [['mobile', 55], ['desktop', 35], ['tablet', 8], ['other', 2]];
let simCounter = 0;

function newEventId() { simCounter += 1; return 'sim-' + Date.now().toString(36) + '-' + simCounter.toString(36) + '-' + Math.random().toString(36).slice(2, 8); }
function randIp() { return [93, 184, 216 + (simCounter % 8), 1 + (simCounter % 250)].join('.'); }
function pickDevice() {
  const r = Math.random() * 100;
  let acc = 0;
  for (const [d, w] of DEVICES) { acc += w; if (r <= acc) return d; }
  return 'desktop';
}
function makeEvent(opts) {
  opts = opts || {};
  return {
    event_id: opts.event_id || newEventId(),
    ad_id: opts.ad_id || state.selectedAdId,
    viewer_id: opts.viewer_id || 'v_' + Math.random().toString(36).slice(2, 10),
    timestamp: new Date().toISOString(),
    ip: opts.ip || randIp(),
    country: opts.country || COUNTRIES[Math.floor(Math.random() * COUNTRIES.length)],
    device: opts.device || pickDevice(),
  };
}

function startSim() {
  stopSim();
  state.sim = { sent: 0, ok: 0, fail: 0, dupResend: 0, log: [], timer: null };
  $('#sim-start').innerHTML = '<span class="pulse"></span>Continuous running';
  $('#sim-start').disabled = true;
  const rate = Number($('#sim-rate').value);
  const batch = rate >= 20 ? 10 : 2;
  state.sim.timer = setInterval(() => sendBatch(makeBatch(batch, 0)), Math.max(20, (batch / rate) * 1000));
  simLog('continuous traffic started — ' + rate + ' events/s target', 'ok');
}

function stopSim() {
  if (state.sim && state.sim.timer) { clearInterval(state.sim.timer); simLog('continuous traffic stopped', 'ok'); }
  if (state.sim) { state.sim.timer = null; }
  const b = $('#sim-start');
  if (b) { b.disabled = false; b.textContent = '▶ Continuous'; }
  const live = $('#sim-live');
  if (live) live.innerHTML = '';
}

function makeBatch(n, dupEvery) {
  const events = [];
  for (let i = 0; i < n; i++) {
    const e = makeEvent();
    if (dupEvery && events.length && Math.random() < dupEvery) { e.event_id = events[events.length - 1].event_id; state.sim.dupResend++; }
    events.push(e);
  }
  return events;
}

async function sendBatch(events) {
  if (!state.sim) state.sim = { sent: 0, ok: 0, fail: 0, dupResend: 0, log: [] };
  const s = state.sim;
  s.sent += events.length;
  try {
    if (!STORE.apiKey) throw new Error('no ingest API key — generate one below the simulator');
    const res = await api('/clicks/batch', { headers: { 'X-API-Key': STORE.apiKey }, body: { events } });
    s.ok += res.accepted;
    simLog('→ ' + res.accepted + ' accepted via ' + res.served_by + (res.partitions.length ? ' (p' + res.partitions.join(',') + ')' : ''), 'ok');
    $('#s-served').textContent = res.served_by;
  } catch (e) {
    s.fail += events.length;
    simLog('✗ ' + e.message, 'err');
  }
  simUpdateCounters();
}

async function sendDuplicates() {
  if (!state.selectedAdId) return toast('Create an ad first', 'err');
  const n = 5;
  const events = [];
  for (let i = 0; i < n; i++) events.push(makeEvent());
  const ids = events.map((e) => e.event_id);
  await sendBatch(events);
  for (const id of ids) {
    const dup = makeEvent({ event_id: id });
    await sendBatch([dup]);
    state.sim.dupResend += 1;
  }
  // Expect: 3 submissions per event_id → 1 counted, 2 duplicates (see worker counters).
  simLog('sent ' + n + ' events ×3 — expect 5 counted, 10 duplicates (worker counters)', 'fraud');
}

async function sendFraudBurst() {
  if (!state.selectedAdId) return toast('Create an ad first', 'err');
  const viewer = 'fraud_' + Date.now().toString(36);
  const ip = randIp();
  const events = [];
  for (let i = 0; i < 110; i++) {
    events.push(makeEvent({ viewer_id: viewer, ip: ip, country: 'US' }));
  }
  simLog('fraud burst: 110 events, one viewer + one IP on ' + state.selectedAdId, 'fraud');
  await sendBatch(events);
  simLog('expect: viewer_repeat (≥50/ad/min) + ip_burst (≥100/10s) flags shortly', 'fraud');
}

function simLog(line, cls) {
  const log = $('#sim-log');
  if (!log || !state.sim) return;
  state.sim.log.unshift(hhmm(new Date().toISOString()) + ' ' + line);
  state.sim.log = state.sim.log.slice(0, 120);
  log.innerHTML = state.sim.log.map((l, i) => '<div class="' + (i === 0 ? cls || '' : '') + '">' + esc(l) + '</div>').join('');
}

function simUpdateCounters() {
  const s = state.sim;
  if (!s) return;
  $('#s-sent').textContent = fmt(s.sent);
  $('#s-ok').textContent = fmt(s.ok);
  $('#s-fail').textContent = fmt(s.fail);
  $('#s-dup').textContent = fmt(s.dupResend);
}

function simRenderWorkerStats(infra, dash, fraud) {
  const p = (dash && dash.pipeline) || {};
  const totalFraud = (p.fraud_ip_burst || 0) + (p.fraud_viewer_repeat || 0) + (p.fraud_campaign_spike || 0);
  const wd = $('#s-wdup'); if (wd) wd.textContent = fmt(p.duplicates || 0);
  const fd = $('#s-fraud'); if (fd) fd.textContent = fmt(totalFraud);
}

function bindSimulator() {
  const s = $('#sel-sim-note');
  $('#sim-ad').addEventListener('change', (e) => { state.selectedAdId = e.target.value; });
  $('#sim-1').onclick = () => sendBatch([makeEvent()]);
  $('#sim-10').onclick = () => sendBatch(makeBatch(10, 0));
  $('#sim-100').onclick = () => sendBatch(makeBatch(100, 0));
  $('#sim-dup').onclick = sendDuplicates;
  $('#sim-fraud').onclick = sendFraudBurst;
  $('#sim-start').onclick = startSim;
  $('#sim-stop').onclick = stopSim;
  $('#save-key').onclick = () => {
    const v = $('#sim-key').value.trim();
    STORE.apiKey = v;
    $('#key-note').textContent = v ? 'Key saved locally for this browser.' : 'Key cleared.';
    toast(v ? 'API key saved' : 'API key cleared', 'ok');
  };
  $('#gen-key').onclick = async () => {
    try {
      const r = await api('/api/ingest-keys', { body: { label: 'simulator' } });
      $('#sim-key').value = r.key;
      STORE.apiKey = r.key;
      $('#key-note').innerHTML = '<b style="color:var(--warn)">Copy it now — this is shown only once.</b>';
      toast('Ingest key generated', 'ok');
    } catch (e) { toast(e.message, 'err'); }
  };
  simUpdateCounters();
}

/* -------------------------------------------------------------- campaigns */

function refreshCampaignsPage() {
  if (lastPage === 'campaigns') renderCampaigns();
}

/* ------------------------------------------------------------------ boot */

(async function boot() {
  if (!STORE.token) return renderAuth('login');
  try {
    await enterApp();
    // Re-render campaign pages when data changes between polls.
    setInterval(refreshCampaignsPage, 5000);
  } catch (e) {
    if (e.status !== 401) toast('Failed to load: ' + e.message, 'err');
    renderAuth('login');
  }
})();

// Hook: after overview renders, keep campaigns page fresh.
const _renderPage = renderPage;
renderPage = function (page) { _renderPage(page); if (page === 'campaigns') renderCampaigns(); };
