/* 0xPlant console UI */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ago = (ts) => { if (!ts) return '—'; const d = Date.now() / 1000 - ts; if (d < 60) return `${Math.floor(d)}s ago`; if (d < 3600) return `${Math.floor(d / 60)} min ago`; if (d < 86400) return `${Math.floor(d / 3600)} h ago`; return `${Math.floor(d / 86400)} d ago`; };
  const when = (ts) => ts ? new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : '—';
  const sevBadge = (s) => `<span class="badge ${s === 'critical' ? 'b-red' : s === 'warning' ? 'b-amber' : 'b-blue'}">${esc(s)}</span>`;
  const statusBadge = (s) => `<span class="badge ${s === 'online' ? 'b-green' : s === 'offline' ? 'b-red' : 'b-grey'}">${esc(s || 'unknown')}</span>`;
  const num = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const table = (cols, rows, empty = 'Nothing to show yet.') => rows.length
    ? `<div style="overflow-x:auto"><table><thead><tr>${cols.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`
    : `<div class="empty">${empty}</div>`;
  const toast = (m) => { const t = $('toast'); t.textContent = m; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = 'none'), 3500); };

  let me = null, page = 'dashboard', timer = null;
  const filters = { severity: '', category: '' };

  async function api(path, body) {
    const opts = { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' };
    if (body !== undefined) { opts.method = 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('unauthenticated'); }
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }
  async function act(path, body, okMsg) { try { await api(path, body || {}); toast(okMsg || 'Done'); refresh(); } catch (e) { toast('Error: ' + e.message); } }
  window.oxp = { act };

  // ---- login ----
  function showLogin() { $('loginOverlay').classList.remove('off'); $('appShell').classList.remove('on'); clearInterval(timer); }
  async function login() {
    $('loginErr').textContent = '';
    try {
      const j = await api('/api/login', { username: $('loginUser').value, password: $('loginPass').value });
      me = j; $('loginOverlay').classList.add('off'); $('appShell').classList.add('on'); $('logoutBtn').textContent = `${j.username} (${j.role}) ⏻`;
      $('loginPass').value = ''; start();
    } catch (e) { $('loginErr').textContent = e.message; }
  }
  $('loginBtn').onclick = login;
  $('loginPass').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
  $('loginUser').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('loginPass').focus(); });
  $('logoutBtn').onclick = async () => { try { await api('/api/logout', {}); } catch (e) { /* ignore */ } me = null; showLogin(); };

  // ---- navigation ----
  const titles = { dashboard: 'Dashboard', inventory: 'Asset Inventory', topology: 'Network Topology', protocols: 'Protocol Analysis', zones: 'Purdue Zones', process: 'Process Integrity', events: 'Event Log', changes: 'Change Tracking', alerts: 'Alerts', policy: 'Conduit Policy', sensors: 'Sensors & Discovery', audit: 'Audit Log', settings: 'Settings' };
  document.querySelectorAll('.side-item').forEach((it) => it.addEventListener('click', () => {
    document.querySelectorAll('.side-item').forEach((i) => i.classList.remove('active'));
    document.querySelectorAll('.page').forEach((p) => p.classList.remove('active'));
    it.classList.add('active'); page = it.dataset.page; $('pg-' + page).classList.add('active'); $('pageTitle').textContent = titles[page] || page; refresh();
  }));
  document.body.addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const [path, msg] = [b.dataset.act, b.dataset.msg];
    if (b.dataset.ticket !== undefined) { const t = prompt('Change ticket / CAB reference (optional):', ''); if (t === null) return; return act(path, { ticket: t }, msg); }
    act(path, {}, msg);
  });
  document.body.addEventListener('change', (e) => { if (e.target.dataset.filter) { filters[e.target.dataset.filter] = e.target.value; refresh(); } });

  // ---- pages ----
  const P = {};
  P.dashboard = async () => {
    const s = await api('/api/summary');
    $('ver').textContent = 'v' + s.version;
    const types = Object.entries(s.assets.by_type).sort((a, b) => b[1].count - a[1].count);
    $('pg-dashboard').innerHTML = `
      <div class="metrics">
        <div class="metric"><div class="metric-val" style="color:var(--green)">${s.assets.total}</div><div class="metric-label">Assets</div><div class="metric-change">${s.assets.online} online · ${s.assets.offline} offline${s.assets.rogue ? ` · <b style="color:var(--red)">${s.assets.rogue} unapproved</b>` : ''}</div></div>
        <div class="metric"><div class="metric-val" style="color:var(--red)">${s.alerts.critical}</div><div class="metric-label">Critical alerts</div><div class="metric-change">${s.alerts.warning} warning · ${s.alerts.resolved_7d} resolved (7d)</div></div>
        <div class="metric"><div class="metric-val" style="color:var(--blue)">${s.events_24h.total || 0}</div><div class="metric-label">Events (24h)</div><div class="metric-change">${s.events_24h.critical || 0} critical · ${s.events_24h.warning || 0} warning</div></div>
        <div class="metric"><div class="metric-val" style="color:var(--amber)">${s.conduits.reduce((a, c) => a + c.denied, 0)}</div><div class="metric-label">Requests blocked</div><div class="metric-change">${s.conduits.reduce((a, c) => a + c.requests, 0)} inspected on ${s.conduits.length} conduits</div></div>
        <div class="metric"><div class="metric-val" style="color:var(--purple)">${s.sensors.length}</div><div class="metric-label">Sensors</div><div class="metric-change">${s.sensors.map((x) => `${esc(x.name)} ${ago(x.last_seen)}`).join(', ') || 'none reporting'}</div></div>
      </div>
      <div class="grid2">
        <div class="card"><div class="card-title">Asset distribution</div>${table(['Type', 'Count', 'Online', 'Open alerts'], types.map(([t, v]) => [`<b>${esc(t)}</b>`, v.count, `<span class="badge ${v.online === v.count ? 'b-green' : 'b-amber'}">${v.count ? Math.round(v.online / v.count * 100) : 0}%</span>`, v.alerts]))}</div>
        <div class="card"><div class="card-title">Recent activity</div>${s.recent.map((e) => `<div class="ev-item"><div class="ev-dot" style="background:var(--${e.severity === 'critical' ? 'red' : e.severity === 'warning' ? 'amber' : 'green'})"></div><div><div class="ev-time">${ago(e.ts)} · ${esc(e.rule || e.category)}</div><div class="ev-title">${esc(e.title)}</div><div class="ev-desc">${esc(e.source_ip || '')}${e.source_ip && e.asset ? ' → ' : ''}${esc(e.asset || '')}</div></div></div>`).join('') || '<div class="empty">No events yet.</div>'}</div>
      </div>
      <div class="grid2">
        <div class="card"><div class="card-title">Conduits</div>${table(['Conduit', 'Protects', 'Sources', 'Inspected', 'Blocked'], s.conduits.map((c) => [`<span class="mono">${esc(c.id)}</span>`, esc(c.asset), c.sources, c.requests, c.denied ? `<span class="badge b-red">${c.denied}</span>` : '0']))}</div>
        <div class="card"><div class="card-title">Process integrity</div>${table(['Asset', 'Status', 'Excursions'], Object.entries(s.process).map(([a, v]) => [`<b>${esc(a)}</b>`, statusBadge(v.online ? 'online' : 'offline'), v.excursions ? `<span class="badge b-red">${v.excursions}</span>` : '<span class="badge b-green">none</span>']), 'Integrity monitor has not polled yet.')}</div>
      </div>`;
    $('n-alerts').textContent = s.alerts.critical + s.alerts.warning + s.alerts.info || ''; $('n-alerts').className = 'cnt' + (s.alerts.critical ? ' red' : '');
    $('n-assets').textContent = s.assets.total; $('n-process').textContent = Object.values(s.process).reduce((a, v) => a + v.excursions, 0) || '';
  };
  P.inventory = async () => {
    const a = await api('/api/assets');
    $('pg-inventory').innerHTML = `<div class="page-head"><h2>Asset Inventory</h2><p>Approved baseline plus everything discovered by the console and the sensors</p></div><div class="card">${table(['Asset', 'Name', 'Type', 'Vendor / Model', 'Firmware', 'Protocols', 'Zone', 'IP', 'Status', 'Last seen', ''], a.map((x) => [
      `<span class="mono">${esc(x.id)}</span>`, esc(x.name), esc(x.type), esc([x.vendor, x.model].filter(Boolean).join(' ') || '—'), esc(x.firmware || '—'), esc((x.protocols || []).join(', ') || '—'), esc(x.zone || '—'), `<span class="mono">${esc(x.ip)}</span>`,
      statusBadge(x.status) + (x.approved ? '' : ' <span class="badge b-red">unapproved</span>'), ago(x.last_seen),
      me && ['admin', 'engineer'].includes(me.role) ? (x.approved ? `<button class="act" data-act="/api/assets/${esc(x.id)}/unapprove" data-msg="Asset unapproved">Unapprove</button>` : `<button class="act" data-act="/api/assets/${esc(x.id)}/approve" data-msg="Asset approved">Approve</button>`) : '']))}</div>`;
  };
  P.topology = async () => {
    const f = await api('/api/flows');
    $('pg-topology').innerHTML = `<div class="page-head"><h2>Network Topology</h2><p>Conversations observed on the conduits (Level 2/3 → Level 1)</p></div><div class="card"><div class="card-title">Conversation map</div>${table(['Source', 'Source IP', 'Conduit', 'Protected asset', 'Protocol', 'Functions', 'Requests', 'Blocked', 'First seen', 'Last seen'], f.map((x) => [
      `<b>${esc(x.source_asset || 'unlisted')}</b>`, `<span class="mono">${esc(x.source_ip)}</span>`, `<span class="mono">${esc(x.conduit)}</span>`, esc(x.asset), esc(x.protocol), `<span class="mono">${esc(Object.entries(x.functions || {}).map(([k, v]) => `${k} ×${v}`).join(', '))}</span>`, x.requests, x.denied ? `<span class="badge b-red">${x.denied}</span>` : '0', ago(x.first_seen), ago(x.last_seen)]), 'No conversations observed yet. Start the HMI to see traffic.')}</div>`;
  };
  P.protocols = async () => {
    const p = await api('/api/protocols');
    $('pg-protocols').innerHTML = `<div class="page-head"><h2>Protocol Analysis</h2><p>Protocols in use on the plant network and their function-code profile</p></div><div class="grid3">${p.map((x) => `<div class="proto-item"><div class="proto-name">${esc(x.protocol)}</div><div class="proto-port">${x.pairs} pair(s) · ${x.assets.length} asset(s)</div><div class="proto-stat">${x.requests} requests · ${x.denied ? `<b style="color:var(--red)">${x.denied} blocked</b>` : 'no blocks'}<br><span class="mono">${esc(Object.entries(x.functions).map(([k, v]) => `${k} ×${v}`).join(' · ')) || 'no traffic profile yet'}</span></div></div>`).join('') || '<div class="empty">Nothing observed yet.</div>'}</div>`;
  };
  P.zones = async () => {
    const [s, a] = await Promise.all([api('/api/summary'), api('/api/assets')]);
    $('pg-zones').innerHTML = `<div class="page-head"><h2>Purdue Zones</h2><p>IEC 62443 zones and conduits — every path into Level 1 passes through a 0xPlant conduit</p></div>` + s.zones.sort((x, y) => y.level - x.level).map((z) => `<div class="zone-bar"><div class="zone-color" style="background:${esc(z.color)}"></div><div style="flex:1"><div class="zone-name">${esc(z.name)}</div><div class="zone-info">${z.assets} assets · ${z.online} online · ${z.conduits.length} conduit(s) ${z.conduits.map(esc).join(', ')} · ${z.findings} open finding(s)<br>${a.filter((x) => x.zone === z.id).map((x) => `${esc(x.id)} (${esc(x.ip)})`).join(' · ')}</div></div><span class="badge ${z.findings ? 'b-red' : 'b-green'}">${z.findings ? 'Findings' : 'Healthy'}</span></div>`).join('');
  };
  P.process = async () => {
    const live = await api('/api/process');
    const roles = { process: 'b-blue', setpoint: 'b-grey', config: 'b-amber', interlock: 'b-red', status: 'b-grey' };
    $('pg-process').innerHTML = `<div class="page-head"><h2>Process Integrity</h2><p>Read-only polling through the conduits: safe envelopes, golden configuration, interlocks, device identity</p></div>` + (Object.entries(live).map(([asset, v]) => `<div class="card"><div class="card-title"><span>${esc(asset)} ${statusBadge(v.online ? 'online' : 'offline')} <span class="mono" style="color:var(--text3)">${esc(v.host || '')} · ${v.identity ? esc(`${v.identity.VendorName} ${v.identity.ModelName} rev ${v.identity.MajorMinorRevision} app ${v.identity.UserApplicationName}`) : 'identity not read'}</span></span><span class="ev-time">${ago(v.ts)}</span></div>${v.online ? table(['Tag', 'Value', 'Role', 'Envelope / golden', 'Status', 'Description'], Object.entries(v.tags).map(([n, t]) => {
      let env = '—';
      if (t.role === 'process' && (t.min != null || t.max != null)) { const lo = t.min ?? 0, hi = t.max ?? lo + 1; const pct = Math.max(0, Math.min(100, (t.value - lo) / (hi - lo) * 100)); env = `<span class="tagbar ${t.status}"><i style="left:${pct}%"></i></span><span class="mono">${num(t.min)} … ${num(t.max)}</span>`; }
      else if (t.role === 'config') env = `<span class="mono">golden ${num(t.golden)}</span>`;
      return [`<b class="mono">${esc(n)}</b>`, `<span class="mono">${num(t.value)} ${esc(t.unit)}</span>`, `<span class="badge ${roles[t.role] || 'b-grey'}">${esc(t.role)}</span>`, env, `<span class="badge ${t.status === 'ok' ? 'b-green' : 'b-red'}">${esc(t.status)}</span>`, esc(t.desc || '')];
    })) : `<div class="empty">${esc(v.error || 'offline')}</div>`}</div>`).join('') || '<div class="empty">No integrity targets configured.</div>');
  };
  P.events = async () => {
    const e = await api(`/api/events?limit=300&severity=${filters.severity}&category=${filters.category}`);
    $('pg-events').innerHTML = `<div class="page-head"><h2>Event Log</h2><p>Every decision and observation from the conduits, discovery, integrity monitor and console</p></div><div class="filters"><select data-filter="severity"><option value="">All severities</option>${['critical', 'warning', 'info'].map((s) => `<option ${filters.severity === s ? 'selected' : ''}>${s}</option>`).join('')}</select><select data-filter="category"><option value="">All categories</option>${['SECURITY', 'PROCESS', 'CONFIG', 'NETWORK', 'AVAILABILITY', 'OPERATIONS', 'AUTH', 'SYSTEM'].map((s) => `<option ${filters.category === s ? 'selected' : ''}>${s}</option>`).join('')}</select></div><div class="card">${table(['Time', 'Severity', 'Rule', 'Event', 'Asset', 'Source', 'Sensor', 'Detail'], e.map((x) => [`<span class="mono">${when(x.ts)}</span>`, sevBadge(x.severity), `<span class="mono">${esc(x.rule || x.category)}</span>`, esc(x.title), esc(x.asset), `<span class="mono">${esc(x.source_ip)}</span>`, esc(x.sensor || 'console'), `<span class="mono" style="color:var(--text3)">${esc(x.detail && x.detail.reason ? x.detail.reason : JSON.stringify(x.detail || {}).slice(0, 140))}</span>`]))}</div>`;
  };
  P.changes = async () => {
    const c = await api('/api/changes?limit=300');
    const can = me && ['admin', 'engineer'].includes(me.role);
    $('pg-changes').innerHTML = `<div class="page-head"><h2>Change Tracking</h2><p>Setpoint and configuration changes observed on the PLCs. Approving a configuration change makes it the new golden value.</p></div><div class="card">${table(['Time', 'Asset', 'Tag', 'Kind', 'Old', 'New', 'Status', 'Ticket', 'Reviewed by', ''], c.map((x) => [`<span class="mono">${when(x.ts)}</span>`, esc(x.asset), `<b class="mono">${esc(x.tag)}</b>`, `<span class="badge ${x.kind === 'config' ? 'b-amber' : 'b-grey'}">${esc(x.kind)}</span>`, num(x.old_value), num(x.new_value), `<span class="badge ${x.status === 'approved' || x.status === 'logged' ? 'b-green' : x.status === 'rejected' ? 'b-red' : 'b-amber'}">${esc(x.status)}</span>`, esc(x.ticket || '—'), esc(x.reviewed_by || '—'), can && x.status === 'unreviewed' ? `<button class="act" data-act="/api/changes/${x.id}/approve" data-ticket data-msg="Change approved">Approve</button><button class="act" data-act="/api/changes/${x.id}/reject" data-ticket data-msg="Change rejected">Reject</button>` : '']))}</div>`;
    $('n-changes').textContent = c.filter((x) => x.status === 'unreviewed').length || '';
  };
  P.alerts = async () => {
    const [open, all] = await Promise.all([api('/api/alerts?status=open'), api('/api/alerts?limit=100')]);
    const can = (p) => me && ((p === 'ack' && ['admin', 'engineer', 'soc'].includes(me.role)) || (p === 'resolve' && ['admin', 'soc'].includes(me.role)));
    const row = (x) => [sevBadge(x.severity), `<span class="mono">${esc(x.rule || '')}</span>`, esc(x.asset), esc(x.title), `<span class="mono">${esc(x.source_ip || '')}</span>`, x.count, `<span class="mono">${when(x.last_ts)}</span>`, `<span class="badge ${x.status === 'active' ? 'b-red' : x.status === 'acknowledged' ? 'b-amber' : 'b-green'}">${esc(x.status)}</span>${x.ack_by ? ` <span class="mono" style="color:var(--text3)">${esc(x.ack_by)}</span>` : ''}`,
      (x.status === 'active' && can('ack') ? `<button class="act" data-act="/api/alerts/${x.id}/ack" data-msg="Acknowledged">Ack</button>` : '') + (x.status !== 'resolved' && can('resolve') ? `<button class="act" data-act="/api/alerts/${x.id}/resolve" data-msg="Resolved">Resolve</button>` : '')];
    const cnt = (s) => open.filter((x) => x.severity === s).length;
    $('pg-alerts').innerHTML = `<div class="page-head"><h2>Alerts</h2><p>Correlated findings; repeated events update the same alert instead of flooding the queue</p></div><div class="metrics"><div class="metric"><div class="metric-val" style="color:var(--red)">${cnt('critical')}</div><div class="metric-label">Critical</div></div><div class="metric"><div class="metric-val" style="color:var(--amber)">${cnt('warning')}</div><div class="metric-label">Warning</div></div><div class="metric"><div class="metric-val" style="color:var(--blue)">${cnt('info')}</div><div class="metric-label">Info</div></div><div class="metric"><div class="metric-val" style="color:var(--green)">${all.filter((x) => x.status === 'resolved').length}</div><div class="metric-label">Resolved (recent)</div></div></div><div class="card"><div class="card-title">Open alerts</div>${table(['Severity', 'Rule', 'Asset', 'Alert', 'Source', 'Count', 'Last seen', 'Status', ''], open.map(row), 'No open alerts.')}</div><div class="card"><div class="card-title">Recently resolved</div>${table(['Severity', 'Rule', 'Asset', 'Alert', 'Source', 'Count', 'Last seen', 'Status', ''], all.filter((x) => x.status === 'resolved').slice(0, 30).map(row), 'Nothing resolved yet.')}</div>`;
  };
  P.policy = async () => {
    const p = await api('/api/policy');
    $('pg-policy').innerHTML = `<div class="page-head"><h2>Conduit Policy</h2><p>Allowlists enforced by the sensors: default deny, per-source functions and write ranges, rate limits</p></div>` + p.map((c) => `<div class="card"><div class="card-title"><span><span class="mono">${esc(c.id)}</span> protects <b>${esc(c.asset)}</b></span><span class="mono" style="color:var(--text3)">${esc(c.listen)} → ${esc(c.upstream)} · default ${esc(c.default)} · ${c.max_rps} req/s</span></div>${table(['Source', 'Address', 'Allowed', 'Write ranges', 'Extra functions', 'Rate limit'], c.rules.map((r) => [`<b>${esc(r.asset || '—')}</b>`, `<span class="mono">${esc(r.source)}</span>`, r.allow.map((a) => `<span class="badge ${a === 'write' ? 'b-amber' : 'b-green'}">${esc(a)}</span>`).join(' ') || '<span class="badge b-grey">none</span>', `<span class="mono">${esc(Object.entries(r.writes).map(([t, v]) => `${t}: ${v.join(', ')}`).join(' · ')) || '—'}</span>`, `<span class="mono">${esc(r.functions.join(', ') || '—')}</span>`, r.max_rps ? `${r.max_rps} req/s` : 'conduit default']))}</div>`).join('');
  };
  P.sensors = async () => {
    const [s, st] = await Promise.all([api('/api/sensors'), api('/api/settings')]);
    $('pg-sensors').innerHTML = `<div class="page-head"><h2>Sensors &amp; Discovery</h2><p>Sensors run the conduits next to the PLCs and report to this console over an authenticated channel</p></div><div class="card"><div class="card-title">Sensors</div>${table(['Sensor', 'First seen', 'Last heartbeat', 'Events received', 'Configured conduits'], s.map((x) => [`<b>${esc(x.name)}</b>`, when(x.first_seen), `${ago(x.last_seen)} ${Date.now() / 1000 - x.last_seen > 30 ? '<span class="badge b-red">stale</span>' : '<span class="badge b-green">alive</span>'}`, x.events, esc(((st.sensors.find((y) => y.name === x.name) || {}).conduits || []).join(', '))]), 'No sensor has reported yet.')}</div><div class="card"><div class="card-title">Discovery scopes</div>${table(['Runner', 'Zone', 'Targets', 'Ports', 'Interval'], st.discovery.map((d) => [esc(d.runner), esc(d.zone), `<span class="mono">${esc(d.targets.join(', '))}</span>`, `<span class="mono">${esc(d.ports.join(', '))}</span>`, `${d.interval_s}s`]))}</div>`;
  };
  P.audit = async () => {
    const a = await api('/api/audit?limit=300');
    $('pg-audit').innerHTML = `<div class="page-head"><h2>Audit Log</h2><p>${a.total} entries — logins, lockouts, alert handling, change approvals, denied actions</p></div><div class="card">${table(['Time', 'User', 'Action', 'Target', 'Result', 'Client IP', 'Detail'], a.entries.map((x) => [`<span class="mono">${when(x.ts)}</span>`, `<b>${esc(x.user)}</b>`, `<span class="mono">${esc(x.action)}</span>`, esc(x.target), `<span class="badge ${String(x.result).startsWith('ok') ? 'b-green' : 'b-red'}">${esc(x.result)}</span>`, `<span class="mono">${esc(x.ip)}</span>`, `<span class="mono" style="color:var(--text3)">${esc(JSON.stringify(x.detail || {}))}</span>`]))}</div>`;
  };
  P.settings = async () => {
    const s = await api('/api/settings');
    $('pg-settings').innerHTML = `<div class="page-head"><h2>Settings</h2><p>Configuration is file-based (${esc(s.config_path)}); the console is read-only by design</p></div><div class="grid2"><div class="card"><div class="card-title">Console</div>${table(['Setting', 'Value'], [['Site', esc(s.site)], ['Version', esc(s.version)], ['Database', `<span class="mono">${esc(s.database)}</span>`], ['Event retention', `${s.retention_days} days`], ['TLS', s.tls ? '<span class="badge b-green">enabled</span>' : '<span class="badge b-amber">disabled — configure console.tls for production</span>'], ['Session lifetime', `${s.session_hours} h`], ['Syslog (CEF)', s.outputs.syslog ? `<span class="mono">${esc(s.outputs.syslog)}</span> · ${s.outputs.sent_syslog} sent` : '<span class="badge b-grey">off</span>'], ['Webhook', s.outputs.webhook ? `<span class="mono">${esc(s.outputs.webhook)}</span> · ${s.outputs.sent_webhook} sent` : '<span class="badge b-grey">off</span>'], ['Forwarding threshold', esc(s.outputs.min_severity)]])}</div><div class="card"><div class="card-title">Users &amp; roles</div>${table(['User', 'Role'], s.users.map((u) => [`<b>${esc(u.username)}</b>`, `<span class="badge b-blue">${esc(u.role)}</span>`]))}<p class="empty">admin: everything · engineer: approve changes and assets · soc: acknowledge and resolve alerts · viewer: read-only</p></div></div><div class="card"><div class="card-title">Integrity targets</div>${table(['Asset', 'Via conduit', 'Tags monitored'], s.integrity.targets.map((t) => [`<b>${esc(t.asset)}</b>`, `<span class="mono">${esc(t.host)}:${t.port}</span>`, t.tags]))}</div><div class="card"><div class="card-title">Detection rules</div>${table(['Rule', 'Title', 'Severity', 'Category', 'Description', 'Reference'], s.rules.map((r) => [`<span class="mono">${esc(r.id)}</span>`, `<b>${esc(r.title)}</b>`, sevBadge(r.severity), esc(r.category), esc(r.description), `<span class="mono" style="color:var(--text3)">${esc(r.reference)}</span>`]))}</div>`;
  };

  async function refresh() {
    try {
      await P[page]();
      if (page !== 'dashboard') { const s = await api('/api/summary'); $('n-alerts').textContent = s.alerts.critical + s.alerts.warning + s.alerts.info || ''; $('n-alerts').className = 'cnt' + (s.alerts.critical ? ' red' : ''); $('n-assets').textContent = s.assets.total; }
      $('liveBadge').textContent = '● live'; $('liveBadge').className = 'badge-live';
    } catch (e) { if (e.message !== 'unauthenticated') { $('liveBadge').textContent = '● ' + e.message; $('liveBadge').className = 'badge-live down'; } }
  }
  function start() { clearInterval(timer); refresh(); timer = setInterval(refresh, 4000); }
  (async () => { try { const m = await api('/api/me'); if (m.authenticated) { me = m; $('loginOverlay').classList.add('off'); $('appShell').classList.add('on'); $('logoutBtn').textContent = `${m.username} (${m.role}) ⏻`; start(); } } catch (e) { /* show login */ } })();
})();
