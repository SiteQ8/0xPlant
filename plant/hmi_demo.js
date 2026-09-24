/* 0xPlant Water Works: in-browser HMI backend for the GitHub Pages demo.
   Implements the same HTTP API as plant/hmi.py (state, alarms, history, login, roles, writes)
   on top of the browser port of the plant (plant/plantsim.js), so the demo HMI drives a real
   control system instead of replaying frames. Also runs under Node for the tests. */
(function (root) {
  'use strict';
  const PlantSim = root.PlantSim;
  const ROLE_RANK = { viewer: 0, operator: 1, supervisor: 2 };
  const now = () => Date.now() / 1000;

  class AlarmManager {
    constructor() { this.alarms = new Map(); this.history = []; }
    static definitions(tagDefs) { return (tagDefs || []).filter((t) => t.table === 'discrete' && /^(Interlock|Alarm):/.test(t.desc || '')).map((t) => [t.name, t.desc.split(':').slice(1).join(':').trim(), t.desc.startsWith('Interlock') ? 'trip' : 'alarm']); }
    _log(plc, tag, text, cls, event) { this.history.unshift({ ts: now(), plc, tag, text, class: cls, event }); if (this.history.length > 500) this.history.pop(); }
    _transition(plc, tag, text, cls, active) {
      const key = plc + '|' + tag, a = this.alarms.get(key);
      if (active && !a) { this.alarms.set(key, { plc, tag, text, class: cls, active: true, acked: false, first_ts: now(), last_ts: now(), count: 1, ack_by: null }); this._log(plc, tag, text, cls, 'ACTIVE'); }
      else if (active && !a.active) { Object.assign(a, { active: true, acked: false, last_ts: now(), count: a.count + 1 }); this._log(plc, tag, text, cls, 'ACTIVE'); }
      else if (!active && a && a.active) { a.active = false; this._log(plc, tag, text, cls, 'CLEARED'); if (a.acked) this.alarms.delete(key); }
    }
    update(plc, defs, tags, online, error) {
      this._transition(plc, 'COMM', `Communication lost with ${plc}` + (error ? ` (${error})` : ''), 'trip', !online);
      if (online) for (const [tag, text, cls] of defs) this._transition(plc, tag, text, cls, !!tags[tag]);
    }
    ack(plc, tag, user) {
      let n = 0;
      for (const [key, a] of Array.from(this.alarms.entries())) {
        if ((plc && a.plc !== plc) || (tag && a.tag !== tag) || a.acked) continue;
        a.acked = true; a.ack_by = user; this._log(a.plc, a.tag, a.text, a.class, `ACK by ${user}`); n++;
        if (!a.active) this.alarms.delete(key);
      }
      return n;
    }
    snapshot() {
      const active = Array.from(this.alarms.values()).sort((x, y) => (x.acked - y.acked) || (y.last_ts - x.last_ts));
      return { alarms: active.map((a) => Object.assign({}, a)), history: this.history.slice(0, 200), unacked: active.filter((a) => !a.acked).length, active: active.filter((a) => a.active).length };
    }
  }

  class Historian {
    constructor(seconds = 1800) { this.seconds = seconds; this.series = {}; this.last = {}; }
    record(plc, tags, ts) {
      ts = ts == null ? now() : ts;
      if (ts - (this.last[plc] || 0) < 1.0) return;
      this.last[plc] = ts;
      const s = this.series[plc] = this.series[plc] || {};
      for (const [k, v] of Object.entries(tags)) { const arr = s[k] = s[k] || []; arr.push([Math.round(ts * 10) / 10, v]); if (arr.length > this.seconds) arr.shift(); }
    }
    query(plc, tags, seconds) { const cutoff = now() - seconds, s = this.series[plc] || {}; const out = {}; for (const t of tags) out[t] = (s[t] || []).filter((p) => p[0] >= cutoff); return out; }
  }

  class DemoBackend {
    /* opts: { tagDefs: {plc: [...]}, users: [{username, password, role}], timeScale, scanS, noise, seed, autostart } */
    constructor(opts = {}) {
      this.tagDefs = opts.tagDefs || {};
      this.users = opts.users || [{ username: 'operator', password: 'Operator@2025', role: 'operator' }, { username: 'supervisor', password: 'Supervisor@2025', role: 'supervisor' }];
      this.scanS = opts.scanS || 0.25;
      this.sim = new PlantSim({ timeScale: opts.timeScale || 60, scanS: this.scanS, noise: opts.noise, seed: opts.seed });
      this.alarms = new AlarmManager();
      this.historian = new Historian();
      this.defs = {}; for (const plc of Object.keys(this.sim.plcs)) this.defs[plc] = AlarmManager.definitions(this.tagDefs[plc]);
      this.session = null; this.started = now(); this.log = []; this.lastTick = null; this.timer = null;
      this.scanned = 0;
      if (opts.autostart !== false && typeof setInterval === 'function') this.start();
    }
    start() { if (this.timer) return; this.lastTick = now(); this.timer = setInterval(() => this.tick(), this.scanS * 1000); if (this.timer.unref) this.timer.unref(); }
    stop() { if (this.timer) clearInterval(this.timer); this.timer = null; }
    setTimeScale(x) { this.sim.timeScale = Math.max(1, Math.min(3600, +x || 60)); return this.sim.timeScale; }
    /* advance the plant by the wall-clock time elapsed (catching up after a background tab), then update alarms and history */
    tick(scans) {
      const t = now();
      let n = scans != null ? scans : Math.min(40, Math.round((t - (this.lastTick == null ? t : this.lastTick)) / this.scanS));
      this.lastTick = t;
      if (n <= 0) n = 1;
      this.sim.step(n); this.scanned += n;
      const st = this.sim.state(this.tagDefs);
      for (const [plc, s] of Object.entries(st.plcs)) { this.alarms.update(plc, this.defs[plc], s.tags, s.online, s.error); if (s.online) this.historian.record(plc, s.tags, t); }
      return st;
    }
    state() { const st = this.sim.state(this.tagDefs); const al = this.alarms.snapshot(); return Object.assign(st, { alarm_summary: { unacked: al.unacked, active: al.active } }); }
    fault(plc, spec) { if (!this.sim.plcs[plc]) return { ok: false, error: `unknown PLC ${plc}` }; this.sim.fault(plc, spec); this.log.push({ ts: now(), plc, spec }); return { ok: true }; }
    _audit(user, action, detail) { this.log.push({ ts: now(), user, action, detail }); }
    /* the HTTP API of plant/hmi.py, as (path, body) -> JSON. Throws on the errors the page treats as exceptions. */
    api(path, body) {
      const [route, qs] = String(path).split('?'); const q = {}; for (const [k, v] of new URLSearchParams(qs || '')) q[k] = v;
      const s = this.session;
      if (route === '/api/health') return { ok: true, uptime: now() - this.started };
      if (route === '/api/me') return { authenticated: !!s, username: s && s.username, role: s && s.role, login_required: true };
      if (route === '/api/login') {
        const u = this.users.find((x) => x.username === String((body || {}).username || '') && x.password === String((body || {}).password || ''));
        if (!u) return { ok: false, error: 'invalid credentials (demo: operator / Operator@2025, supervisor / Supervisor@2025)' };
        this.session = { username: u.username, role: u.role, since: now() }; this._audit(u.username, 'login');
        return { ok: true, username: u.username, role: u.role };
      }
      if (!s) { if (typeof showLogin === 'function') showLogin(); throw new Error('login required'); }
      if (route === '/api/logout') { this._audit(s.username, 'logout'); this.session = null; return { ok: true }; }
      if (route === '/api/state') return Object.assign(this.state(), { user: s.username, role: s.role });
      if (route === '/api/alarms') return this.alarms.snapshot();
      if (route === '/api/history') { const tags = (q.tags || '').split(',').filter(Boolean).slice(0, 12); const seconds = Math.max(10, Math.min(1800, +(q.seconds || 600) || 600)); return { plc: q.plc || '', seconds, series: this.historian.query(q.plc || '', tags, seconds) }; }
      if (route === '/api/write') {
        const b = body || {}; const value = +b.value; if (!b.plc || !b.tag || isNaN(value)) return { ok: false, error: 'bad request: plc, tag and numeric value required' };
        const r = this.sim.write(String(b.plc), String(b.tag), value, s.role); this._audit(s.username, 'write', { plc: b.plc, tag: b.tag, value, ok: r.ok, error: r.error }); return r;
      }
      if (route === '/api/alarms/ack') { if (ROLE_RANK[s.role] < 1) return { ok: false, error: 'operator role required' }; const b = body || {}; return { ok: true, acknowledged: this.alarms.ack(b.plc || null, b.tag || null, s.username) }; }
      return { ok: false, error: 'not found' };
    }
  }
  root.HMIDemoBackend = DemoBackend; root.HMIAlarmManager = AlarmManager; root.HMIHistorian = Historian;
  if (root.HMI_TAGDEFS && !root.HMI_DEMO) root.HMI_DEMO = new DemoBackend(Object.assign({ tagDefs: root.HMI_TAGDEFS }, root.HMI_DEMO_CONFIG || {}));
})(typeof window !== 'undefined' ? window : globalThis);
