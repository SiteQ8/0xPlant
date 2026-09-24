/* 0xPlant console UI - no external dependencies, CSP-safe (script-src 'self') */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ago = (ts) => { if (!ts) return '—'; const d = Date.now() / 1000 - ts; if (d < 60) return `${Math.max(0, Math.floor(d))}s ago`; if (d < 3600) return `${Math.floor(d / 60)} min ago`; if (d < 86400) return `${Math.floor(d / 3600)} h ago`; return `${Math.floor(d / 86400)} d ago`; };
  const when = (ts) => ts ? new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : '—';
  const num = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const sevBadge = (s) => `<span class="badge ${s === 'critical' ? 'b-red' : s === 'warning' ? 'b-amber' : 'b-blue'}">${esc(s)}</span>`;
  const statusBadge = (s) => `<span class="badge ${s === 'online' ? 'b-green' : s === 'offline' ? 'b-red' : 'b-grey'}">${esc(s || 'unknown')}</span>`;
  const sevColor = (s) => s === 'critical' ? css('--red') : s === 'warning' ? css('--amber') : css('--blue');

  // ---------------- state ----------------
  const S = { me: null, page: 'dashboard', timer: null, search: '', sort: {}, filters: { severity: '', category: '', status: 'open' },
    cache: {}, procHist: {}, evHist: [], knownAlerts: null, theme: 'light' };
  try { S.theme = localStorage.getItem('oxp-theme') || 'light'; } catch (e) { /* storage may be blocked */ }
  document.documentElement.setAttribute('data-theme', S.theme);

  async function api(path, body) {
    const opts = { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' };
    if (body !== undefined) { opts.method = 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('unauthenticated'); }
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  function toast(m, cls) { const t = $('toast'); t.textContent = m; t.className = cls || ''; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = 'none'), 4000); }
  async function act(path, body, okMsg) { try { await api(path, body || {}); toast(okMsg || 'Done'); refresh(); } catch (e) { toast('Error: ' + e.message, 'crit'); } }

  // ---------------- tables: sort + search + drawer rows ----------------
  const rowText = (r) => r.map((c) => String(c).replace(/<[^>]+>/g, ' ')).join(' ').toLowerCase();
  function table(key, cols, rows, opts = {}) {
    const empty = opts.empty || 'Nothing to show yet.';
    let list = rows.map((r, i) => ({ r, i }));
    if (S.search) list = list.filter((x) => rowText(x.r).includes(S.search));
    const sort = S.sort[key];
    if (sort) {
      const idx = sort.col;
      const val = (x) => { const raw = String(x.r[idx]).replace(/<[^>]+>/g, '').trim(); const n = parseFloat(raw.replace(/[^0-9.\-]/g, '')); return raw !== '' && !isNaN(n) && /^[-\d.]/.test(raw) ? n : raw.toLowerCase(); };
      list.sort((a, b) => { const va = val(a), vb = val(b); return (va < vb ? -1 : va > vb ? 1 : 0) * (sort.asc ? 1 : -1); });
    }
    if (!list.length) return `<div class="empty">${S.search ? 'No rows match the filter.' : empty}</div>`;
    return `<div style="overflow-x:auto"><table data-key="${key}"><thead><tr>${cols.map((c, i) => `<th data-col="${i}" class="${sort && sort.col === i ? 'sorted' + (sort.asc ? ' asc' : '') : ''}">${c}</th>`).join('')}</tr></thead><tbody>${list.map(({ r, i }) => `<tr ${opts.drawer ? `class="click" data-drawer="${opts.drawer}" data-idx="${i}"` : ''}>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }
  document.body.addEventListener('click', (e) => {
    const th = e.target.closest('th[data-col]');
    if (th) { const key = th.closest('table').dataset.key, col = +th.dataset.col; const cur = S.sort[key]; S.sort[key] = cur && cur.col === col ? { col, asc: !cur.asc } : { col, asc: true }; refresh(); return; }
    const b = e.target.closest('[data-act]');
    if (b) { e.stopPropagation(); const [path, msg] = [b.dataset.act, b.dataset.msg]; if (b.dataset.ticket !== undefined) { const t = prompt('Change ticket / CAB reference (optional):', ''); if (t === null) return; return act(path, { ticket: t }, msg); } return act(path, {}, msg); }
    const row = e.target.closest('[data-drawer]');
    if (row) { const item = (S.cache[row.dataset.drawer] || [])[+row.dataset.idx]; if (item) openDrawer(row.dataset.drawer, item); return; }
    const node = e.target.closest('.node[data-asset]');
    if (node) { const a = (S.cache.assets || []).find((x) => x.id === node.dataset.asset); if (a) openDrawer('assets', a); }
  });
  document.body.addEventListener('change', (e) => { if (e.target.dataset.filter) { S.filters[e.target.dataset.filter] = e.target.value; refresh(); } });
  $('search').addEventListener('input', (e) => { S.search = e.target.value.trim().toLowerCase(); refresh(); });
  document.addEventListener('keydown', (e) => { if (e.key === '/' && document.activeElement.tagName !== 'INPUT') { e.preventDefault(); $('search').focus(); } if (e.key === 'Escape') { closeDrawer(); $('search').blur(); } });

  // ---------------- drawer ----------------
  function dl(pairs) { return `<dl>${pairs.filter((p) => p[1] !== undefined && p[1] !== null && p[1] !== '').map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`; }
  function openDrawer(kind, item) {
    let title = 'Details', body = '';
    if (kind === 'events') {
      title = item.title;
      body = dl([['Time', when(item.ts)], ['Severity', sevBadge(item.severity)], ['Rule', `<span class="mono">${esc(item.rule || '—')}</span>`], ['Category', esc(item.category)], ['Asset', esc(item.asset)], ['Source', `<span class="mono">${esc(item.source_ip)}</span>`], ['Destination', `<span class="mono">${esc(item.dest_ip)}</span>`], ['Protocol', esc(item.protocol)], ['Sensor', esc(item.sensor || 'console')]]) + `<b>Detail</b><pre>${esc(JSON.stringify(item.detail || {}, null, 2))}</pre>`;
      const rule = (S.cache.rules || []).find((r) => r.id === item.rule); if (rule) body += `<b>${esc(rule.title)}</b><p class="muted" style="margin:6px 0">${esc(rule.description)}</p><span class="chip">${esc(rule.reference)}</span>`;
    } else if (kind === 'alerts') {
      title = item.title;
      const can = (p) => S.me && ((p === 'ack' && ['admin', 'engineer', 'soc'].includes(S.me.role)) || (p === 'resolve' && ['admin', 'soc'].includes(S.me.role)));
      body = dl([['Status', `<span class="badge ${item.status === 'active' ? 'b-red' : item.status === 'acknowledged' ? 'b-amber' : 'b-green'}">${esc(item.status)}</span>`], ['Severity', sevBadge(item.severity)], ['Rule', `<span class="mono">${esc(item.rule || '')}</span>`], ['Asset', esc(item.asset)], ['Source', `<span class="mono">${esc(item.source_ip || '')}</span>`], ['Occurrences', item.count], ['First seen', when(item.first_ts)], ['Last seen', when(item.last_ts)], ['Handled by', esc(item.ack_by || '')], ['Resolved', item.resolved_ts ? when(item.resolved_ts) : '']])
        + `<div style="margin-bottom:12px">${item.status === 'active' && can('ack') ? `<button class="act" data-act="/api/alerts/${item.id}/ack" data-msg="Acknowledged">Acknowledge</button>` : ''}${item.status !== 'resolved' && can('resolve') ? `<button class="act" data-act="/api/alerts/${item.id}/resolve" data-msg="Resolved">Resolve</button>` : ''}</div><b>Last detail</b><pre>${esc(JSON.stringify(item.detail || {}, null, 2))}</pre>`;
      const rule = (S.cache.rules || []).find((r) => r.id === item.rule); if (rule) body += `<p class="muted" style="margin:6px 0">${esc(rule.description)}</p><span class="chip">${esc(rule.reference)}</span>`;
    } else if (kind === 'assets') {
      title = `${item.id} · ${item.name}`;
      const related = (S.cache.openAlerts || []).filter((a) => a.asset === item.id);
      const ident = item.detail && item.detail.identity;
      body = dl([['Type', esc(item.type)], ['IP', `<span class="mono">${esc(item.ip)}</span>`], ['Zone', esc(item.zone || '—')], ['Status', statusBadge(item.status) + (item.approved ? '' : ' <span class="badge b-red">unapproved</span>')], ['Criticality', esc(item.criticality)], ['Vendor', esc(item.vendor)], ['Model', esc(item.model)], ['Firmware', esc(item.firmware)], ['Application', esc(item.application)], ['Protocols', (item.protocols || []).map((p) => `<span class="chip">${esc(p)}</span>`).join('')], ['Open ports', (item.ports || []).map((p) => `<span class="chip">${p}</span>`).join('') || '—'], ['First seen', when(item.first_seen)], ['Last seen', ago(item.last_seen)], ['Discovered by', esc(item.discovered_by || '—')], ['Description', esc(item.detail && item.detail.description)]])
        + (ident ? `<b>Device identification</b><pre>${esc(JSON.stringify(ident, null, 2))}</pre>` : '')
        + `<b>Open alerts (${related.length})</b>` + (related.length ? `<ul style="margin:6px 0 0 16px">${related.map((a) => `<li>${sevBadge(a.severity)} ${esc(a.title)}</li>`).join('')}</ul>` : '<p class="muted">none</p>');
    } else if (kind === 'changes') {
      title = `${item.asset} · ${item.tag}`;
      body = dl([['Time', when(item.ts)], ['Kind', esc(item.kind)], ['Old value', num(item.old_value)], ['New value', num(item.new_value)], ['Status', esc(item.status)], ['Ticket', esc(item.ticket || '—')], ['Reviewed by', esc(item.reviewed_by || '—')], ['Reviewed', item.reviewed_ts ? when(item.reviewed_ts) : ''], ['Source', esc(item.source_asset || item.source_ip || '—')], ['Note', esc(item.note)]]);
      if (S.me && ['admin', 'engineer'].includes(S.me.role) && item.status === 'unreviewed') body += `<button class="act" data-act="/api/changes/${item.id}/approve" data-ticket data-msg="Change approved">Approve (becomes golden)</button><button class="act danger" data-act="/api/changes/${item.id}/reject" data-ticket data-msg="Change rejected">Reject</button>`;
    } else if (kind === 'flows') {
      title = `${item.source_asset || item.source_ip} → ${item.asset}`;
      body = dl([['Conduit', `<span class="mono">${esc(item.conduit)}</span>`], ['Source IP', `<span class="mono">${esc(item.source_ip)}</span>`], ['Protocol', esc(item.protocol)], ['Requests', item.requests], ['Blocked', item.denied], ['First seen', when(item.first_seen)], ['Last seen', ago(item.last_seen)], ['Sensor', esc(item.sensor)]]) + `<b>Function profile</b>${table('flowfn', ['Function', 'Count'], Object.entries(item.functions || {}).map(([k, v]) => [esc(k), v]))}`;
    } else if (kind === 'audit') {
      title = `${item.user} · ${item.action}`;
      body = dl([['Time', when(item.ts)], ['User', esc(item.user)], ['Action', esc(item.action)], ['Target', esc(item.target)], ['Result', esc(item.result)], ['Client IP', `<span class="mono">${esc(item.ip)}</span>`]]) + `<pre>${esc(JSON.stringify(item.detail || {}, null, 2))}</pre>`;
    }
    $('drawerTitle').textContent = title; $('drawerBody').innerHTML = body; $('drawer').classList.add('open'); $('scrim').classList.add('on');
  }
  function closeDrawer() { const was = $('drawer').classList.contains('open'); $('drawer').classList.remove('open'); $('scrim').classList.remove('on'); if (was) refresh(); }
  $('drawerClose').onclick = closeDrawer; $('scrim').onclick = closeDrawer;

  // ---------------- charts (canvas, DPR aware) ----------------
  function ctx2d(c) { const dpr = window.devicePixelRatio || 1; const w = c.clientWidth || 300, h = c.clientHeight || 100; if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; } const g = c.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h); return [g, w, h]; }
  function sparkline(c, values, color, fill = true) {
    if (!c) return; const [g, w, h] = ctx2d(c); if (values.length < 2) return;
    const max = Math.max(...values, 1), min = Math.min(...values, 0); const x = (i) => i / (values.length - 1) * (w - 2) + 1, y = (v) => h - 2 - (v - min) / (max - min || 1) * (h - 6);
    g.beginPath(); values.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v)));
    if (fill) { g.save(); g.lineTo(x(values.length - 1), h); g.lineTo(x(0), h); g.closePath(); g.fillStyle = color; g.globalAlpha = .12; g.fill(); g.restore(); g.beginPath(); values.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v))); }
    g.strokeStyle = color; g.lineWidth = 1.8; g.lineJoin = 'round'; g.stroke(); g.fillStyle = color; g.beginPath(); g.arc(x(values.length - 1), y(values[values.length - 1]), 2.5, 0, Math.PI * 2); g.fill();
  }
  function donut(c, segs) {
    if (!c) return; const [g, w, h] = ctx2d(c); const total = segs.reduce((a, s) => a + s.v, 0); const r = Math.min(w, h) / 2 - 4, cx = w / 2, cy = h / 2; let a0 = -Math.PI / 2;
    if (!total) { g.beginPath(); g.arc(cx, cy, r, 0, Math.PI * 2); g.strokeStyle = css('--border'); g.lineWidth = 16; g.stroke(); }
    segs.forEach((s) => { if (!s.v) return; const a1 = a0 + s.v / total * Math.PI * 2; g.beginPath(); g.arc(cx, cy, r, a0, a1); g.strokeStyle = s.c; g.lineWidth = 16; g.stroke(); a0 = a1; });
    g.fillStyle = css('--text'); g.font = `700 22px ${css('--mono')}`; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(String(total), cx, cy - 4); g.font = `11px ${css('--sans')}`; g.fillStyle = css('--text3'); g.fillText('open', cx, cy + 14);
  }
  function lineChart(c, series, opts = {}) {
    if (!c) return; const [g, w, h] = ctx2d(c); const pad = { l: 36, r: 8, t: 8, b: 20 }; const all = series.flatMap((s) => s.v); if (!all.length) { g.fillStyle = css('--text3'); g.font = `12px ${css('--sans')}`; g.fillText('collecting…', pad.l, h / 2); return; }
    let lo = Math.min(...all, opts.min == null ? Infinity : opts.min), hi = Math.max(...all, opts.max == null ? -Infinity : opts.max); if (hi === lo) { hi += 1; lo = opts.min != null ? Math.min(lo, opts.min) : lo - 1; } const span = hi - lo; if (!(opts.min != null && lo >= opts.min)) lo -= span * .08; hi += span * .08;
    const n = Math.max(...series.map((s) => s.v.length), 2); const x = (i) => pad.l + i / (n - 1) * (w - pad.l - pad.r), y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (h - pad.t - pad.b);
    if (opts.min != null && opts.max != null) { g.fillStyle = css('--green'); g.globalAlpha = .08; g.fillRect(pad.l, y(opts.max), w - pad.l - pad.r, y(opts.min) - y(opts.max)); g.globalAlpha = 1; }
    g.strokeStyle = css('--border'); g.lineWidth = 1; g.fillStyle = css('--text3'); g.font = `10px ${css('--mono')}`; g.textAlign = 'right';
    for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4, yy = y(v); g.beginPath(); g.moveTo(pad.l, yy); g.lineTo(w - pad.r, yy); g.stroke(); g.fillText(v.toFixed(Math.abs(hi) < 10 ? 2 : 0), pad.l - 4, yy + 3); }
    if (opts.min != null) { g.strokeStyle = css('--red'); g.setLineDash([4, 3]); g.beginPath(); g.moveTo(pad.l, y(opts.min)); g.lineTo(w - pad.r, y(opts.min)); g.stroke(); } if (opts.max != null) { g.beginPath(); g.moveTo(pad.l, y(opts.max)); g.lineTo(w - pad.r, y(opts.max)); g.stroke(); } g.setLineDash([]);
    series.forEach((s) => { g.strokeStyle = s.c; g.lineWidth = 2; g.lineJoin = 'round'; g.beginPath(); s.v.forEach((v, i) => i ? g.lineTo(x(i), y(v)) : g.moveTo(x(i), y(v))); g.stroke(); });
    g.textAlign = 'left'; g.fillStyle = css('--text3'); g.fillText(opts.xlabel || `last ${n} samples`, pad.l, h - 6);
    let lx = w - pad.r - series.reduce((a, s) => a + (s.name ? 16 + g.measureText(s.name).width + 14 : 0), 0);
    series.forEach((s) => { if (!s.name) return; g.fillStyle = s.c; g.fillRect(lx, pad.t + 4, 12, 3); g.fillStyle = css('--text2'); g.fillText(s.name, lx + 16, pad.t + 9); lx += 16 + g.measureText(s.name).width + 14; });
  }
  function bucketEvents(events, minutes = 30) { const now = Math.floor(Date.now() / 60000); const b = new Array(minutes).fill(0), crit = new Array(minutes).fill(0); events.forEach((e) => { const i = minutes - 1 - (now - Math.floor(e.ts / 60)); if (i >= 0 && i < minutes) { b[i]++; if (e.severity === 'critical') crit[i]++; } }); return { all: b, crit }; }

  // ---------------- login / nav ----------------
  function showLogin() { $('loginOverlay').classList.remove('off'); $('appShell').classList.remove('on'); clearInterval(S.timer); }
  async function login() {
    $('loginErr').textContent = '';
    try { const j = await api('/api/login', { username: $('loginUser').value, password: $('loginPass').value }); enter(j); $('loginPass').value = ''; }
    catch (e) { $('loginErr').textContent = e.message; }
  }
  function enter(m) { S.me = m; $('loginOverlay').classList.add('off'); $('appShell').classList.add('on'); $('logoutBtn').textContent = `${m.username} · ${m.role} ⏻`; start(); }
  $('loginBtn').onclick = login;
  $('loginPass').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
  $('loginUser').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('loginPass').focus(); });
  $('logoutBtn').onclick = async () => { try { await api('/api/logout', {}); } catch (e) { /* ignore */ } S.me = null; showLogin(); };
  $('themeBtn').onclick = () => { S.theme = S.theme === 'dark' ? 'light' : 'dark'; document.documentElement.setAttribute('data-theme', S.theme); try { localStorage.setItem('oxp-theme', S.theme); } catch (e) { /* ignore */ } refresh(); };
  const titles = { dashboard: 'Dashboard', inventory: 'Asset Inventory', topology: 'Network Topology', protocols: 'Protocol Analysis', zones: 'Purdue Zones', process: 'Process Integrity', events: 'Event Log', changes: 'Change Tracking', alerts: 'Alerts', policy: 'Conduit Policy', sensors: 'Sensors & Discovery', audit: 'Audit Log', settings: 'Settings' };
  document.querySelectorAll('.side-item').forEach((it) => it.addEventListener('click', () => {
    document.querySelectorAll('.side-item').forEach((i) => i.classList.remove('active')); document.querySelectorAll('.page').forEach((p) => p.classList.remove('active'));
    it.classList.add('active'); S.page = it.dataset.page; $('pg-' + S.page).classList.add('active'); $('pageTitle').textContent = titles[S.page] || S.page; closeDrawer(); refresh();
  }));

  // ---------------- pages ----------------
  const P = {};
  P.dashboard = async () => {
    const [s, ev] = await Promise.all([api('/api/summary'), api('/api/events?limit=600')]);
    $('ver').textContent = 'v' + s.version + ' · ' + s.site; S.cache.events = s.recent; S.cache.rules = S.cache.rules || await api('/api/rules');
    const buckets = bucketEvents(ev), blocked = s.conduits.reduce((a, c) => a + c.denied, 0), inspected = s.conduits.reduce((a, c) => a + c.requests, 0);
    const types = Object.entries(s.assets.by_type).sort((a, b) => b[1].count - a[1].count);
    $('pg-dashboard').innerHTML = `
      <div class="metrics">
        <div class="metric"><div class="metric-val" style="color:var(--green2)">${s.assets.total}</div><div class="metric-label">Assets</div><div class="metric-change">${s.assets.online} online · ${s.assets.offline} offline${s.assets.rogue ? ` · <b style="color:var(--red)">${s.assets.rogue} unapproved</b>` : ''}</div><canvas class="spark" id="sp-assets"></canvas></div>
        <div class="metric"><div class="metric-val" style="color:var(--red)">${s.alerts.critical}</div><div class="metric-label">Critical alerts</div><div class="metric-change">${s.alerts.warning} warning · ${s.alerts.resolved_7d} resolved (7d)</div><canvas class="spark" id="sp-crit"></canvas></div>
        <div class="metric"><div class="metric-val" style="color:var(--blue)">${s.events_24h.total || 0}</div><div class="metric-label">Events (24h)</div><div class="metric-change">${s.events_24h.critical || 0} critical · ${s.events_24h.warning || 0} warning</div><canvas class="spark" id="sp-ev"></canvas></div>
        <div class="metric"><div class="metric-val" style="color:var(--amber)">${blocked}</div><div class="metric-label">Requests blocked</div><div class="metric-change">${inspected} inspected on ${s.conduits.length} conduits</div><canvas class="spark" id="sp-blk"></canvas></div>
        <div class="metric"><div class="metric-val" style="color:var(--purple)">${s.sensors.length}</div><div class="metric-label">Sensors</div><div class="metric-change">${s.sensors.map((x) => `${esc(x.name)} ${ago(x.last_seen)}`).join(', ') || 'none reporting'}</div></div>
      </div>
      <div class="grid21">
        <div class="card"><div class="card-title">Event rate <small>per minute · last 30 min · red = critical</small></div><canvas class="chart" id="ch-events"></canvas></div>
        <div class="card"><div class="card-title">Open alerts</div><div class="donut-wrap"><canvas id="donut"></canvas><div class="donut-legend"><div><i style="background:var(--red)"></i>Critical <b>${s.alerts.critical}</b></div><div><i style="background:var(--amber)"></i>Warning <b>${s.alerts.warning}</b></div><div><i style="background:var(--blue)"></i>Info <b>${s.alerts.info}</b></div><div><i style="background:var(--green)"></i>Resolved 7d <b>${s.alerts.resolved_7d}</b></div></div></div></div>
      </div>
      <div class="grid2">
        <div class="card"><div class="card-title">Recent activity <small>click for details</small></div>${s.recent.map((e, i) => `<div class="ev-item" data-drawer="events" data-idx="${i}"><div class="ev-dot" style="background:${sevColor(e.severity)};color:${sevColor(e.severity)}"></div><div><div class="ev-time">${ago(e.ts)} · ${esc(e.rule || e.category)}</div><div class="ev-title">${esc(e.title)}</div><div class="ev-desc">${esc(e.source_ip || '')}${e.source_ip && e.asset ? ' → ' : ''}${esc(e.asset || '')}</div></div></div>`).join('') || '<div class="empty">No events yet.</div>'}</div>
        <div>
          <div class="card"><div class="card-title">Conduits <small>inspected / blocked</small></div>${table('dash-cond', ['Conduit', 'Protects', 'Sources', 'Inspected', 'Blocked', ''], s.conduits.map((c) => [`<span class="mono">${esc(c.id)}</span>`, esc(c.asset), c.sources, c.requests, c.denied ? `<span class="badge b-red">${c.denied}</span>` : '0', `<div class="bar"><i style="width:${inspected ? Math.round(c.requests / inspected * 100) : 0}%"></i></div>`]))}</div>
          <div class="card"><div class="card-title">Asset distribution</div>${table('dash-types', ['Type', 'Count', 'Online', 'Open alerts'], types.map(([t, v]) => [`<b>${esc(t)}</b>`, v.count, `<span class="badge ${v.online === v.count ? 'b-green' : v.online ? 'b-amber' : 'b-grey'}">${v.count ? Math.round(v.online / v.count * 100) : 0}%</span>`, v.alerts ? `<span class="badge b-red">${v.alerts}</span>` : '0']))}</div>
          <div class="card"><div class="card-title">Process integrity</div>${table('dash-proc', ['Asset', 'Status', 'Excursions'], Object.entries(s.process).map(([a, v]) => [`<b>${esc(a)}</b>`, statusBadge(v.online ? 'online' : 'offline'), v.excursions ? `<span class="badge b-red">${v.excursions}</span>` : '<span class="badge b-green">none</span>']), { empty: 'Integrity monitor has not polled yet.' })}</div>
        </div>
      </div>`;
    S.evHist.push({ t: Date.now(), blocked, inspected, online: s.assets.online, crit: s.alerts.critical }); if (S.evHist.length > 60) S.evHist.shift();
    sparkline($('sp-assets'), S.evHist.map((x) => x.online), css('--green')); sparkline($('sp-crit'), S.evHist.map((x) => x.crit), css('--red')); sparkline($('sp-ev'), buckets.all, css('--blue')); sparkline($('sp-blk'), S.evHist.map((x) => x.blocked), css('--amber'));
    lineChart($('ch-events'), [{ v: buckets.all, c: css('--blue'), name: 'all events' }, { v: buckets.crit, c: css('--red'), name: 'critical' }], { min: 0, xlabel: '30 minutes ago → now' });
    donut($('donut'), [{ v: s.alerts.critical, c: css('--red') }, { v: s.alerts.warning, c: css('--amber') }, { v: s.alerts.info, c: css('--blue') }]);
    counts(s);
  };
  function counts(s) { const open = s.alerts.critical + s.alerts.warning + s.alerts.info; $('n-alerts').textContent = open || ''; $('n-alerts').className = 'cnt' + (s.alerts.critical ? ' red' : ''); $('n-assets').textContent = s.assets.total; $('n-process').textContent = Object.values(s.process).reduce((a, v) => a + v.excursions, 0) || ''; }
  P.inventory = async () => {
    const a = await api('/api/assets'); S.cache.assets = a; S.cache.openAlerts = await api('/api/alerts?status=open');
    const can = S.me && ['admin', 'engineer'].includes(S.me.role);
    $('pg-inventory').innerHTML = `<div class="page-head"><div><h2>Asset Inventory</h2><p>Approved baseline plus everything discovered by the console and the sensors · click a row for details</p></div><div class="filters"><span class="chip">${a.length} assets</span><span class="chip">${a.filter((x) => x.status === 'online').length} online</span><span class="chip" style="${a.some((x) => !x.approved) ? 'border-color:var(--red);color:var(--red)' : ''}">${a.filter((x) => !x.approved).length} unapproved</span></div></div><div class="card">${table('inv', ['Asset', 'Name', 'Type', 'Vendor / Model', 'Firmware', 'Protocols', 'Zone', 'IP', 'Status', 'Last seen', ''], a.map((x) => [
      `<b class="mono">${esc(x.id)}</b>`, esc(x.name), esc(x.type), esc([x.vendor, x.model].filter(Boolean).join(' ') || '—'), `<span class="mono">${esc(x.firmware || '—')}</span>`, (x.protocols || []).map((p) => `<span class="chip">${esc(p)}</span>`).join('') || '—', esc(x.zone || '—'), `<span class="mono">${esc(x.ip)}</span>`,
      statusBadge(x.status) + (x.approved ? '' : ' <span class="badge b-red">unapproved</span>'), ago(x.last_seen),
      can ? (x.approved ? `<button class="act" data-act="/api/assets/${esc(x.id)}/unapprove" data-msg="Asset unapproved">Unapprove</button>` : `<button class="act" data-act="/api/assets/${esc(x.id)}/approve" data-msg="Asset approved">Approve</button>`) : '']), { drawer: 'assets' })}</div>`;
  };
  P.topology = async () => {
    const [f, a, s] = await Promise.all([api('/api/flows'), api('/api/assets'), api('/api/summary')]);
    S.cache.flows = f; S.cache.assets = a; S.cache.openAlerts = S.cache.openAlerts || [];
    $('pg-topology').innerHTML = `<div class="page-head"><div><h2>Network Topology</h2><p>Zones, assets and the conversations observed on the conduits · animated edges carry live traffic, red edges carry blocked requests · click a node</p></div></div>
      <div class="card">${topologySvg(s.zones, a, f)}<div class="legend"><span><i style="background:var(--green)"></i>permitted traffic</span><span><i style="background:var(--red)"></i>blocked requests seen</span><span><i style="background:var(--text3)"></i>conduit → protected PLC</span><span>■ node colour = status</span></div></div>
      <div class="card"><div class="card-title">Conversation map <small>click a row for the function profile</small></div>${table('flows', ['Source', 'Source IP', 'Conduit', 'Protected asset', 'Protocol', 'Functions', 'Requests', 'Blocked', 'First seen', 'Last seen'], f.map((x) => [
      `<b>${esc(x.source_asset || 'unlisted')}</b>`, `<span class="mono">${esc(x.source_ip)}</span>`, `<span class="mono">${esc(x.conduit)}</span>`, esc(x.asset), esc(x.protocol), `<span class="mono">${esc(Object.entries(x.functions || {}).map(([k, v]) => `${k} ×${v}`).join(', '))}</span>`, x.requests, x.denied ? `<span class="badge b-red">${x.denied}</span>` : '0', ago(x.first_seen), ago(x.last_seen)]), { drawer: 'flows', empty: 'No conversations observed yet. Start the HMI to see traffic.' })}</div>`;
  };
  function topologySvg(zones, assets, flows) {
    const W = 1200, H = 520; const zs = [...zones].sort((x, y) => y.level - x.level); const bandH = H / Math.max(zs.length, 1);
    const pos = {}; let out = `<svg class="topo" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">`;
    zs.forEach((z, zi) => {
      const y0 = zi * bandH; out += `<rect x="0" y="${y0}" width="${W}" height="${bandH}" fill="${z.color}" opacity=".06"/><rect x="0" y="${y0}" width="6" height="${bandH}" fill="${z.color}"/><text x="18" y="${y0 + 20}" font-size="12" fill="${z.color}" font-weight="700">${esc(z.name)}</text><text x="18" y="${y0 + 36}" font-size="10" fill="${css('--text3')}">${z.assets} assets · ${z.findings} findings</text>`;
      const za = assets.filter((a) => a.zone === z.id).sort((p, q) => (p.type === 'Conduit') - (q.type === 'Conduit')); const gap = (W - 260) / Math.max(za.length, 1);
      const mixed = za.some((a) => a.type === 'Conduit') && za.some((a) => a.type !== 'Conduit');
      za.forEach((a, i) => { const x = 260 + gap * i + gap / 2, y = y0 + bandH / 2 + (mixed ? (a.type === 'Conduit' ? 34 : -22) : 8); pos[a.id] = { x, y }; const col = a.status === 'online' ? css('--green') : a.status === 'offline' ? css('--red') : css('--text3'); const stroke = a.approved ? col : css('--red');
        out += `<g class="node" data-asset="${esc(a.id)}"><rect x="${x - 62}" y="${y - 22}" width="124" height="44" rx="9" fill="${css('--white')}" stroke="${stroke}" stroke-width="1.5"/><circle cx="${x - 50}" cy="${y}" r="4" fill="${col}"/><text x="${x - 40}" y="${y - 4}" font-size="11" font-weight="700" fill="${css('--text')}">${esc(a.id)}</text><text x="${x - 40}" y="${y + 11}" font-size="9" fill="${css('--text3')}">${esc((a.type + ' · ' + a.ip).slice(0, 26))}</text></g>`; });
    });
    const edges = [];
    flows.forEach((fl) => { const src = assets.find((a) => a.ip === fl.source_ip) || assets.find((a) => a.id === fl.source_asset); const cd = assets.find((a) => a.id === fl.conduit); const dst = assets.find((a) => a.id === fl.asset);
      if (src && cd && pos[src.id] && pos[cd.id]) edges.push({ a: pos[src.id], b: pos[cd.id], blocked: fl.denied > 0, w: Math.min(4, 1 + Math.log10(1 + fl.requests)) });
      if (cd && dst && pos[cd.id] && pos[dst.id]) edges.push({ a: pos[cd.id], b: pos[dst.id], blocked: false, w: 1.2, grey: true }); });
    let lines = ''; edges.forEach((e) => { const mx = (e.a.x + e.b.x) / 2; lines += `<path d="M${e.a.x} ${e.a.y + 22} C ${e.a.x} ${(e.a.y + e.b.y) / 2}, ${e.b.x} ${(e.a.y + e.b.y) / 2}, ${e.b.x} ${e.b.y - 22}" fill="none" class="flow ${e.blocked ? 'blocked' : ''}" stroke="${e.grey ? css('--text3') : css('--green')}" stroke-width="${e.w}" opacity=".85"/>`; void mx; });
    return out.replace('<svg', '<svg').replace(`preserveAspectRatio="xMidYMid meet">`, `preserveAspectRatio="xMidYMid meet">${lines}`) + '</svg>';
  }
  P.protocols = async () => {
    const p = await api('/api/protocols'); const max = Math.max(1, ...p.map((x) => x.requests));
    $('pg-protocols').innerHTML = `<div class="page-head"><div><h2>Protocol Analysis</h2><p>Protocols in use on the plant network and their function-code profile</p></div></div><div class="grid3">${p.map((x) => `<div class="proto-item"><div class="proto-name">${esc(x.protocol)}</div><div class="proto-port">${x.pairs} conversation(s) · ${x.assets.length} asset(s): ${esc(x.assets.join(', '))}</div><div class="bar"><i style="width:${Math.round(x.requests / max * 100)}%"></i></div><div class="proto-stat">${x.requests} requests · ${x.denied ? `<b style="color:var(--red)">${x.denied} blocked</b>` : 'no blocks'}<br>${Object.entries(x.functions).map(([k, v]) => `<span class="chip">${esc(k)} ×${v}</span>`).join('') || '<span class="muted">no traffic profile yet</span>'}</div></div>`).join('') || '<div class="empty">Nothing observed yet.</div>'}</div>`;
  };
  P.zones = async () => {
    const [s, a] = await Promise.all([api('/api/summary'), api('/api/assets')]); S.cache.assets = a;
    $('pg-zones').innerHTML = `<div class="page-head"><div><h2>Purdue Zones</h2><p>IEC 62443 zones and conduits · every path into Level 1 passes through a 0xPlant conduit</p></div></div>` + s.zones.sort((x, y) => y.level - x.level).map((z) => `<div class="zone-bar"><div class="zone-color" style="background:${esc(z.color)}"></div><div style="flex:1"><div class="zone-name">${esc(z.name)}</div><div class="zone-info">${z.assets} assets · ${z.online} online · ${z.conduits.length} conduit(s) ${z.conduits.map(esc).join(', ')} · ${z.findings} open finding(s)<br>${a.filter((x) => x.zone === z.id).map((x) => `<span class="chip" style="cursor:pointer" data-drawer="assets" data-idx="${a.indexOf(x)}">${esc(x.id)} · ${esc(x.ip)}</span>`).join('')}</div></div><span class="badge ${z.findings ? 'b-red' : 'b-green'}">${z.findings ? 'Findings' : 'Healthy'}</span></div>`).join('');
  };
  P.process = async () => {
    const live = await api('/api/process'); const roles = { process: 'b-blue', setpoint: 'b-grey', config: 'b-amber', interlock: 'b-red', status: 'b-grey' };
    Object.entries(live).forEach(([asset, v]) => { const h = S.procHist[asset] = S.procHist[asset] || {}; Object.entries(v.tags || {}).forEach(([n, t]) => { if (t.value == null) return; const arr = h[n] = h[n] || []; arr.push(t.value); if (arr.length > 120) arr.shift(); }); });
    $('pg-process').innerHTML = `<div class="page-head"><div><h2>Process Integrity</h2><p>Read-only polling through the conduits · live trends with safe envelopes (green band), golden configuration, interlocks, device identity</p></div></div>` + (Object.entries(live).map(([asset, v]) => `<div class="card"><div class="card-title"><span>${esc(asset)} ${statusBadge(v.online ? 'online' : 'offline')} <small>${esc(v.host || '')} · ${v.identity ? esc(`${v.identity.VendorName} ${v.identity.ModelName} · rev ${v.identity.MajorMinorRevision} · ${v.identity.UserApplicationName}`) : 'identity not read'}</small></span><span class="ev-time">${ago(v.ts)}</span></div>${v.online ? `<div class="tags">${Object.entries(v.tags).map(([n, t]) => {
      const cls = t.status === 'ok' ? '' : t.status === 'drift' ? 'drift' : 'bad'; const env = t.role === 'process' && (t.min != null || t.max != null) ? `envelope ${num(t.min)} … ${num(t.max)}` : t.role === 'config' ? `golden ${num(t.golden)}` : t.desc || '';
      return `<div class="tagcard ${cls}"><div class="tn"><span>${esc(n)}</span><span class="badge ${roles[t.role] || 'b-grey'}">${esc(t.role)}</span></div><div class="tv">${num(t.value)}<small>${esc(t.unit)}</small> ${t.status !== 'ok' ? `<span class="badge b-red">${esc(t.status)}</span>` : ''}</div><canvas data-trend="${esc(asset)}|${esc(n)}"></canvas><div class="td">${esc(env)}</div></div>`; }).join('')}</div>` : `<div class="empty">${esc(v.error || 'offline')}</div>`}</div>`).join('') || '<div class="empty">No integrity targets configured.</div>');
    document.querySelectorAll('canvas[data-trend]').forEach((c) => { const [asset, n] = c.dataset.trend.split('|'); const t = live[asset].tags[n]; const vals = (S.procHist[asset] || {})[n] || []; const col = t.status === 'ok' ? css('--green') : css('--red'); sparkline(c, vals.length > 1 ? vals : [t.value, t.value], col); });
  };
  P.events = async () => {
    const e = await api(`/api/events?limit=400&severity=${S.filters.severity}&category=${S.filters.category}`); S.cache.events = e; S.cache.rules = S.cache.rules || await api('/api/rules');
    $('pg-events').innerHTML = `<div class="page-head"><div><h2>Event Log</h2><p>Every decision and observation from the conduits, discovery, integrity monitor and console · click a row</p></div><div class="filters"><select data-filter="severity"><option value="">All severities</option>${['critical', 'warning', 'info'].map((s) => `<option ${S.filters.severity === s ? 'selected' : ''}>${s}</option>`).join('')}</select><select data-filter="category"><option value="">All categories</option>${['SECURITY', 'PROCESS', 'CONFIG', 'NETWORK', 'AVAILABILITY', 'OPERATIONS', 'AUTH', 'SYSTEM'].map((s) => `<option ${S.filters.category === s ? 'selected' : ''}>${s}</option>`).join('')}</select><span class="chip">${e.length} shown</span></div></div><div class="card">${table('events', ['Time', 'Severity', 'Rule', 'Event', 'Asset', 'Source', 'Sensor', 'Detail'], e.map((x) => [`<span class="mono">${when(x.ts)}</span>`, sevBadge(x.severity), `<span class="mono">${esc(x.rule || x.category)}</span>`, esc(x.title), esc(x.asset), `<span class="mono">${esc(x.source_ip)}</span>`, esc(x.sensor || 'console'), `<span class="mono muted">${esc(x.detail && x.detail.reason ? x.detail.reason : JSON.stringify(x.detail || {}).slice(0, 120))}</span>`]), { drawer: 'events' })}</div>`;
  };
  P.changes = async () => {
    const c = await api('/api/changes?limit=300'); S.cache.changes = c; const can = S.me && ['admin', 'engineer'].includes(S.me.role); const pending = c.filter((x) => x.status === 'unreviewed').length;
    $('pg-changes').innerHTML = `<div class="page-head"><div><h2>Change Tracking</h2><p>Setpoint and configuration changes observed on the PLCs · approving a configuration change makes it the new golden value</p></div><div class="filters"><span class="chip" style="${pending ? 'border-color:var(--amber);color:#B45309' : ''}">${pending} awaiting review</span></div></div><div class="card">${table('changes', ['Time', 'Asset', 'Tag', 'Kind', 'Old', 'New', 'Status', 'Ticket', 'Reviewed by', ''], c.map((x) => [`<span class="mono">${when(x.ts)}</span>`, esc(x.asset), `<b class="mono">${esc(x.tag)}</b>`, `<span class="badge ${x.kind === 'config' ? 'b-amber' : 'b-grey'}">${esc(x.kind)}</span>`, num(x.old_value), num(x.new_value), `<span class="badge ${x.status === 'approved' || x.status === 'logged' ? 'b-green' : x.status === 'rejected' ? 'b-red' : 'b-amber'}">${esc(x.status)}</span>`, esc(x.ticket || '—'), esc(x.reviewed_by || '—'), can && x.status === 'unreviewed' ? `<button class="act" data-act="/api/changes/${x.id}/approve" data-ticket data-msg="Change approved">Approve</button><button class="act danger" data-act="/api/changes/${x.id}/reject" data-ticket data-msg="Change rejected">Reject</button>` : '']), { drawer: 'changes' })}</div>`;
    $('n-changes').textContent = pending || '';
  };
  P.alerts = async () => {
    const [open, all] = await Promise.all([api('/api/alerts?status=open'), api('/api/alerts?limit=100')]); S.cache.rules = S.cache.rules || await api('/api/rules');
    const shown = S.filters.status === 'open' ? open : S.filters.status === 'resolved' ? all.filter((x) => x.status === 'resolved') : all; S.cache.alerts = shown; S.cache.openAlerts = open;
    const can = (p) => S.me && ((p === 'ack' && ['admin', 'engineer', 'soc'].includes(S.me.role)) || (p === 'resolve' && ['admin', 'soc'].includes(S.me.role)));
    const row = (x) => [sevBadge(x.severity), `<span class="mono">${esc(x.rule || '')}</span>`, esc(x.asset), esc(x.title), `<span class="mono">${esc(x.source_ip || '')}</span>`, x.count, `<span class="mono">${when(x.last_ts)}</span>`, `<span class="badge ${x.status === 'active' ? 'b-red' : x.status === 'acknowledged' ? 'b-amber' : 'b-green'}">${esc(x.status)}</span>${x.ack_by ? ` <span class="mono muted">${esc(x.ack_by)}</span>` : ''}`,
      (x.status === 'active' && can('ack') ? `<button class="act" data-act="/api/alerts/${x.id}/ack" data-msg="Acknowledged">Ack</button>` : '') + (x.status !== 'resolved' && can('resolve') ? `<button class="act" data-act="/api/alerts/${x.id}/resolve" data-msg="Resolved">Resolve</button>` : '')];
    const cnt = (s) => open.filter((x) => x.severity === s).length;
    $('pg-alerts').innerHTML = `<div class="page-head"><div><h2>Alerts</h2><p>Correlated findings · repeated events update the same alert instead of flooding the queue · click a row</p></div><div class="filters"><select data-filter="status"><option value="open" ${S.filters.status === 'open' ? 'selected' : ''}>Open</option><option value="resolved" ${S.filters.status === 'resolved' ? 'selected' : ''}>Resolved</option><option value="all" ${S.filters.status === 'all' ? 'selected' : ''}>All</option></select></div></div><div class="metrics"><div class="metric"><div class="metric-val" style="color:var(--red)">${cnt('critical')}</div><div class="metric-label">Critical</div></div><div class="metric"><div class="metric-val" style="color:var(--amber)">${cnt('warning')}</div><div class="metric-label">Warning</div></div><div class="metric"><div class="metric-val" style="color:var(--blue)">${cnt('info')}</div><div class="metric-label">Info</div></div><div class="metric"><div class="metric-val" style="color:var(--green2)">${all.filter((x) => x.status === 'resolved').length}</div><div class="metric-label">Resolved (recent)</div></div></div><div class="card">${table('alerts', ['Severity', 'Rule', 'Asset', 'Alert', 'Source', 'Count', 'Last seen', 'Status', ''], shown.map(row), { drawer: 'alerts', empty: 'No alerts in this view.' })}</div>`;
    if (S.knownAlerts) open.filter((x) => x.severity === 'critical' && !S.knownAlerts.has(x.id)).slice(0, 1).forEach((x) => toast(`New critical alert: ${x.title}`, 'crit'));
    S.knownAlerts = new Set(open.map((x) => x.id));
  };
  P.policy = async () => {
    const p = await api('/api/policy');
    $('pg-policy').innerHTML = `<div class="page-head"><div><h2>Conduit Policy</h2><p>Allowlists enforced by the sensors · default deny, per-source functions and write ranges, rate limits</p></div></div>` + p.map((c) => `<div class="card"><div class="card-title"><span><span class="mono">${esc(c.id)}</span> protects <b>${esc(c.asset)}</b></span><small>${esc(c.listen)} → ${esc(c.upstream)} · default <b style="color:${c.default === 'deny' ? 'var(--green2)' : 'var(--red)'}">${esc(c.default)}</b> · ${c.max_rps} req/s</small></div>${table('pol-' + c.id, ['Source', 'Address', 'Allowed', 'Write ranges', 'Extra functions', 'Rate limit'], c.rules.map((r) => [`<b>${esc(r.asset || '—')}</b>`, `<span class="mono">${esc(r.source)}</span>`, r.allow.map((a) => `<span class="badge ${a === 'write' ? 'b-amber' : 'b-green'}">${esc(a)}</span>`).join(' ') || '<span class="badge b-grey">none</span>', Object.entries(r.writes).map(([t, v]) => `<span class="chip">${esc(t)}: ${esc(v.join(', '))}</span>`).join('') || '—', `<span class="mono">${esc(r.functions.join(', ') || '—')}</span>`, r.max_rps ? `${r.max_rps} req/s` : 'conduit default']))}</div>`).join('');
  };
  P.sensors = async () => {
    const [s, st] = await Promise.all([api('/api/sensors'), api('/api/settings')]);
    $('pg-sensors').innerHTML = `<div class="page-head"><div><h2>Sensors &amp; Discovery</h2><p>Sensors run the conduits next to the PLCs and report to this console over an authenticated channel</p></div></div><div class="card"><div class="card-title">Sensors</div>${table('sensors', ['Sensor', 'First seen', 'Last heartbeat', 'Events received', 'Configured conduits'], s.map((x) => [`<b>${esc(x.name)}</b>`, when(x.first_seen), `${ago(x.last_seen)} ${Date.now() / 1000 - x.last_seen > 30 ? '<span class="badge b-red">stale</span>' : '<span class="badge b-green">alive</span>'}`, x.events, ((st.sensors.find((y) => y.name === x.name) || {}).conduits || []).map((c) => `<span class="chip">${esc(c)}</span>`).join('')]), { empty: 'No sensor has reported yet.' })}</div><div class="card"><div class="card-title">Discovery scopes</div>${table('scopes', ['Runner', 'Zone', 'Targets', 'Ports', 'Interval'], st.discovery.map((d) => [esc(d.runner), esc(d.zone), `<span class="mono">${esc(d.targets.join(', '))}</span>`, d.ports.map((p) => `<span class="chip">${p}</span>`).join(''), `${d.interval_s}s`]))}</div>`;
  };
  P.audit = async () => {
    const a = await api('/api/audit?limit=300'); S.cache.audit = a.entries;
    $('pg-audit').innerHTML = `<div class="page-head"><div><h2>Audit Log</h2><p>${a.total} entries · logins, lockouts, alert handling, change approvals, denied actions</p></div></div><div class="card">${table('audit', ['Time', 'User', 'Action', 'Target', 'Result', 'Client IP'], a.entries.map((x) => [`<span class="mono">${when(x.ts)}</span>`, `<b>${esc(x.user)}</b>`, `<span class="mono">${esc(x.action)}</span>`, esc(x.target), `<span class="badge ${String(x.result).startsWith('ok') ? 'b-green' : 'b-red'}">${esc(x.result)}</span>`, `<span class="mono">${esc(x.ip)}</span>`]), { drawer: 'audit' })}</div>`;
  };
  P.settings = async () => {
    const s = await api('/api/settings'); S.cache.rules = s.rules;
    $('pg-settings').innerHTML = `<div class="page-head"><div><h2>Settings</h2><p>Configuration is file-based (<span class="mono">${esc(s.config_path)}</span>) · the console is read-only by design</p></div></div><div class="grid2"><div class="card"><div class="card-title">Console</div><div class="kv"><div><b>Site</b>${esc(s.site)}</div><div><b>Version</b>${esc(s.version)}</div><div><b>Database</b><span class="mono">${esc(s.database)}</span></div><div><b>Event retention</b>${s.retention_days} days</div><div><b>TLS</b>${s.tls ? '<span class="badge b-green">enabled</span>' : '<span class="badge b-amber">off · set console.tls</span>'}</div><div><b>Session lifetime</b>${s.session_hours} h</div><div><b>Syslog (CEF)</b>${s.outputs.syslog ? `<span class="mono">${esc(s.outputs.syslog)}</span> · ${s.outputs.sent_syslog} sent` : '<span class="badge b-grey">off</span>'}</div><div><b>Webhook</b>${s.outputs.webhook ? `<span class="mono">${esc(s.outputs.webhook)}</span> · ${s.outputs.sent_webhook} sent` : '<span class="badge b-grey">off</span>'}</div><div><b>Forwarding threshold</b>${esc(s.outputs.min_severity)}</div></div></div><div class="card"><div class="card-title">Users &amp; roles</div>${table('users', ['User', 'Role'], s.users.map((u) => [`<b>${esc(u.username)}</b>`, `<span class="badge b-blue">${esc(u.role)}</span>`]))}<p class="empty">admin: everything · engineer: approve changes and assets · soc: acknowledge and resolve alerts · viewer: read-only</p></div></div><div class="card"><div class="card-title">Integrity targets</div>${table('itargets', ['Asset', 'Via conduit', 'Tags monitored'], s.integrity.targets.map((t) => [`<b>${esc(t.asset)}</b>`, `<span class="mono">${esc(t.host)}:${t.port}</span>`, t.tags]))}</div><div class="card"><div class="card-title">Detection rules</div>${table('rules', ['Rule', 'Title', 'Severity', 'Category', 'Description', 'Reference'], s.rules.map((r) => [`<span class="mono">${esc(r.id)}</span>`, `<b>${esc(r.title)}</b>`, sevBadge(r.severity), esc(r.category), esc(r.description), `<span class="mono muted">${esc(r.reference)}</span>`]))}</div>`;
  };

  async function refresh() {
    if ($('drawer').classList.contains('open')) return;   // never re-render under an open drawer
    try {
      await P[S.page]();
      if (S.page !== 'dashboard') counts(await api('/api/summary'));
      $('liveBadge').className = 'badge-live'; $('liveBadge').lastElementChild.textContent = 'live · ' + new Date().toLocaleTimeString();
    } catch (e) { if (e.message !== 'unauthenticated') { $('liveBadge').className = 'badge-live down'; $('liveBadge').lastElementChild.textContent = e.message; } }
  }
  function start() { clearInterval(S.timer); refresh(); S.timer = setInterval(() => { if (!document.hidden) refresh(); }, S.page === 'process' ? 2500 : 4000); }
  (async () => { try { const m = await api('/api/me'); if (m.authenticated) enter(m); } catch (e) { /* login shown */ } })();
})();
