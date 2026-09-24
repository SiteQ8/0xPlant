/* 0xPlant console demo: live plant + process integrity monitor running in the browser.
   Drives the demo's /api/process from the browser port of the plant (plant/plantsim.js) and
   re-implements the integrity monitor rules (OXP-005/006/007/012/013/018) so faults injected on
   the demo page are detected the same way the console detects them on the real PLCs.
   Also runs under Node for the tests. */
(function (root) {
  'use strict';
  let now = () => Date.now() / 1000;      // replaceable clock (tests advance it deterministically)
  const ENVELOPE_DEBOUNCE = 3;

  /* Translate the invariant expression language (a Python subset: v(), prev(), dt, abs/min/max/round,
     not/and/or, chained comparisons) into a JavaScript function. Expressions come from the bundled config. */
  function compile(expr) {
    let js = String(expr);
    js = js.replace(/\bnot\s+/g, '!').replace(/\band\b/g, '&&').replace(/\bor\b/g, '||');
    js = js.replace(/\babs\(/g, 'Math.abs(').replace(/\bmin\(/g, 'Math.min(').replace(/\bmax\(/g, 'Math.max(').replace(/\bround\(/g, 'Math.round(');
    const term = "(-?\\d+(?:\\.\\d+)?|(?:v|prev)\\('[^']+'\\)|dt)";
    js = js.replace(new RegExp(`${term}\\s*(<=|<|>=|>)\\s*${term}\\s*(<=|<|>=|>)\\s*${term}`, 'g'), '($1 $2 $3 && $3 $4 $5)');
    return new Function('v', 'prev', 'dt', 'return (' + js + ');'); // eslint-disable-line no-new-func
  }

  class LiveMonitor {
    /* D: the OXP_DEMO dataset (events, alerts, changes, assets, rules, process snapshots, invariants). */
    constructor(D, opts = {}) {
      this.D = D;
      const PlantSim = root.PlantSim;
      this.sim = new PlantSim({ timeScale: 60, scanS: 0.25, seed: opts.seed || 7, noise: opts.noise });
      this.scanS = 0.25; this.pollS = opts.pollS || 1.0;
      this.rules = {}; (D.rules || []).forEach((r) => { this.rules[r.id] = r; });
      const first = (D.process && D.process[0]) || {};
      this.targets = {};
      for (const [asset, snap] of Object.entries(first)) {
        const tags = {}; for (const [n, t] of Object.entries(snap.tags || {})) tags[n] = { unit: t.unit || '', role: t.role || 'process', min: t.min, max: t.max, golden: t.golden, desc: t.desc || '' };
        const invs = ((D.invariants || {})[asset] || Object.entries(snap.invariants || {}).map(([name, i]) => ({ name, expr: i.expr, desc: i.desc, debounce: 3 }))).map((i) => Object.assign({}, i, { fn: compile(i.expr), bad: 0, good: 0, status: 'pending', alerting: false }));
        this.targets[asset] = { asset, host: snap.host || '', identity: snap.identity || null, tags, invariants: invs, values: {}, previous: {}, status: {}, outCount: {}, inCount: {}, alerting: {}, interlock: {}, drift: {}, setpoints: {}, online: null, lastPoll: null };
      }
      this.nextId = Math.max(0, ...(D.events || []).map((e) => e.id || 0), ...(D.alerts || []).map((a) => a.id || 0), ...(D.changes || []).map((c) => c.id || 0)) + 1;
      this.lastTick = null; this.lastEval = 0; this.timer = null; this.snapshot = null;
      if (opts.autostart !== false && typeof setInterval === 'function') this.start();
    }
    start() { if (this.timer) return; this.lastTick = now(); this.timer = setInterval(() => this.tick(), this.scanS * 1000); if (this.timer.unref) this.timer.unref(); }
    stop() { if (this.timer) clearInterval(this.timer); this.timer = null; }
    /* advance the plant by the wall-clock time elapsed and run one integrity poll per pollS seconds */
    tick(scans) {
      const t = now();
      let n = scans != null ? scans : Math.min(40, Math.round((t - (this.lastTick == null ? t : this.lastTick)) / this.scanS));
      this.lastTick = t; if (n <= 0) n = 1;
      this.sim.step(n);
      if (t - this.lastEval >= this.pollS - 1e-6 || scans != null) { this.poll(t); this.lastEval = t; }
      return this.snapshot;
    }
    /* ---- the console side: events, alerts, changes ---- */
    emit(ruleOrSev, title, extra = {}) {
      const rule = this.rules[ruleOrSev];
      const ev = Object.assign({ id: this.nextId++, ts: now(), severity: rule ? rule.severity : ruleOrSev, category: rule ? rule.category : (extra.category || 'PROCESS'), rule: rule ? rule.id : '', title, source_ip: '', dest_ip: '', asset: '', protocol: '', sensor: '', detail: {} }, extra);
      this.D.events.unshift(ev); if (this.D.events.length > 2000) this.D.events.pop();
      if (extra.alert_key) this.raise(extra.alert_key, ev);
      if (extra.resolve_key) this.resolve(extra.resolve_key);
      delete ev.alert_key; delete ev.resolve_key;
      return ev;
    }
    raise(key, ev) {
      const open = this.D.alerts.find((a) => a.key === key && a.status !== 'resolved');
      if (open) { open.count++; open.last_ts = ev.ts; open.title = ev.title; open.detail = ev.detail; return open; }
      const a = { id: this.nextId++, key, first_ts: ev.ts, last_ts: ev.ts, count: 1, severity: ev.severity, rule: ev.rule, asset: ev.asset, source_ip: ev.source_ip, title: ev.title, status: 'active', ack_by: null, ack_ts: null, resolved_ts: null, detail: ev.detail };
      this.D.alerts.unshift(a); return a;
    }
    resolve(key) { this.D.alerts.filter((a) => a.key === key && a.status !== 'resolved').forEach((a) => { a.status = 'resolved'; a.resolved_ts = now(); a.ack_by = a.ack_by || 'system'; }); }
    change(asset, tag, kind, oldValue, newValue, status, note) { const c = { id: this.nextId++, ts: now(), asset, tag, kind, old_value: oldValue, new_value: newValue, source_ip: '', source_asset: '', status, ticket: '', reviewed_by: null, reviewed_ts: null, note }; this.D.changes.unshift(c); return c; }
    setGolden(asset, tag, value) { const t = this.targets[asset]; if (t && t.tags[tag]) { t.tags[tag].golden = value; t.drift[tag] = false; t.status[tag] = 'ok'; } }
    assetStatus(asset, status) { const a = (this.D.assets || []).find((x) => x.id === asset); if (a) { a.status = status; if (status === 'online') a.last_seen = now(); } }
    /* ---- one integrity poll of every PLC, mirroring oxplant/integrity.py ---- */
    poll(t) {
      const snap = {};
      for (const T of Object.values(this.targets)) {
        const plc = this.sim.plcs[T.asset]; const offline = !plc || !!this.sim.offline[T.asset];
        if (offline) {
          if (T.online !== false) { T.online = false; this.emit('OXP-007', `${T.asset} stopped answering integrity polls via ${T.host}: blackout`, { asset: T.asset, dest_ip: T.host, detail: { error: 'blackout' }, alert_key: `OXP-007:${T.asset}` }); this.assetStatus(T.asset, 'offline'); }
          snap[T.asset] = { online: false, ts: t, host: T.host, identity: T.identity, tags: {}, invariants: {} }; continue;
        }
        if (T.online === false) { this.emit('info', `${T.asset} answering again`, { category: 'AVAILABILITY', asset: T.asset, resolve_key: `OXP-007:${T.asset}` }); this.assetStatus(T.asset, 'online'); }
        T.online = true;
        const values = {}; for (const n of Object.keys(T.tags)) if (n in plc.t) values[n] = +plc.t[n];
        const dt = T.lastPoll == null ? 0 : t - T.lastPoll;
        for (const [n, tag] of Object.entries(T.tags)) {
          if (!(n in values)) continue; const value = values[n];
          if (tag.role === 'process') this._envelope(T, n, tag, value);
          else if (tag.role === 'config') this._config(T, n, tag, value);
          else if (tag.role === 'setpoint') this._setpoint(T, n, tag, value);
          else if (tag.role === 'interlock') this._interlock(T, n, tag, value);
          else T.status[n] = 'ok';
        }
        for (const inv of T.invariants) this._invariant(T, inv, values, T.values, dt);   // T.values still holds the previous poll here
        T.previous = T.values; T.values = values; T.lastPoll = t;
        const tags = {}; for (const [n, tag] of Object.entries(T.tags)) tags[n] = { value: n in values ? Math.round(values[n] * 1000) / 1000 : null, unit: tag.unit, role: tag.role, status: T.status[n] || 'ok', min: tag.min, max: tag.max, golden: tag.golden, desc: tag.desc };
        const invariants = {}; for (const inv of T.invariants) invariants[inv.name] = { status: inv.status, expr: inv.expr, desc: inv.desc };
        snap[T.asset] = { online: true, ts: t, host: T.host, identity: T.identity, tags, invariants };
      }
      this.snapshot = snap; return snap;
    }
    _envelope(T, n, tag, value) {
      const low = tag.min != null && value < tag.min, high = tag.max != null && value > tag.max;
      if (low || high) {
        T.outCount[n] = (T.outCount[n] || 0) + 1; T.inCount[n] = 0; T.status[n] = low ? 'low' : 'high';
        if (T.outCount[n] === ENVELOPE_DEBOUNCE && !T.alerting[n]) { T.alerting[n] = true; const bound = low ? tag.min : tag.max; this.emit('OXP-005', `${T.asset} ${n} = ${value.toFixed(2)} ${tag.unit} is ${low ? 'below' : 'above'} safe limit ${bound} ${tag.unit}`, { asset: T.asset, dest_ip: T.host, detail: { tag: n, value, min: tag.min, max: tag.max, desc: tag.desc }, alert_key: `OXP-005:${T.asset}:${n}` }); }
      } else {
        T.inCount[n] = (T.inCount[n] || 0) + 1; T.outCount[n] = 0; T.status[n] = 'ok';
        if (T.alerting[n] && T.inCount[n] >= ENVELOPE_DEBOUNCE) { T.alerting[n] = false; this.emit('info', `${T.asset} ${n} back inside safe envelope (${value.toFixed(2)} ${tag.unit})`, { category: 'PROCESS', asset: T.asset, detail: { tag: n, value }, resolve_key: `OXP-005:${T.asset}:${n}` }); }
      }
    }
    _config(T, n, tag, value) {
      if (tag.golden == null) { T.status[n] = 'ok'; return; }
      const drift = Math.abs(value - tag.golden) > 1e-9;
      T.status[n] = drift ? 'drift' : 'ok';
      if (drift && !T.drift[n]) { T.drift[n] = true; this.emit('OXP-006', `${T.asset} ${n} changed from golden ${tag.golden} to ${value} ${tag.unit}`, { asset: T.asset, dest_ip: T.host, detail: { tag: n, golden: tag.golden, value, unit: tag.unit }, alert_key: `OXP-006:${T.asset}:${n}` }); this.change(T.asset, n, 'config', tag.golden, value, 'unreviewed', 'Engineering configuration region: approve to make this the golden value'); }
      else if (!drift && T.drift[n]) { T.drift[n] = false; this.emit('info', `${T.asset} ${n} matches golden baseline again (${value} ${tag.unit})`, { category: 'CONFIG', asset: T.asset, detail: { tag: n, value }, resolve_key: `OXP-006:${T.asset}:${n}` }); }
    }
    _setpoint(T, n, tag, value) {
      T.status[n] = 'ok';
      const prev = T.setpoints[n];
      if (prev != null && Math.abs(prev - value) > 1e-9) { this.emit('OXP-013', `${T.asset} ${n} setpoint changed ${prev} -> ${value} ${tag.unit}`, { asset: T.asset, dest_ip: T.host, detail: { tag: n, old: prev, new: value, unit: tag.unit } }); this.change(T.asset, n, 'setpoint', prev, value, 'logged', 'Operator setpoint region'); }
      T.setpoints[n] = value;
    }
    _interlock(T, n, tag, value) {
      const tripped = value !== 0; T.status[n] = tripped ? 'trip' : 'ok';
      if (tripped && !T.interlock[n]) { T.interlock[n] = true; this.emit('OXP-012', `${T.asset} safety interlock tripped (${n} = ${Math.trunc(value)})`, { asset: T.asset, dest_ip: T.host, detail: { tag: n, value }, alert_key: `OXP-012:${T.asset}:${n}` }); }
      else if (!tripped && T.interlock[n]) { T.interlock[n] = false; this.emit('info', `${T.asset} interlock reset (${n})`, { category: 'PROCESS', asset: T.asset, detail: { tag: n }, resolve_key: `OXP-012:${T.asset}:${n}` }); }
    }
    _invariant(T, inv, values, previous, dt) {
      let holds = null;
      if (dt > 0 && Object.keys(previous).length) {
        let missing = false; const v = (k) => { if (!(k in values)) { missing = true; return 0; } return values[k]; }; const prev = (k) => { if (!(k in previous)) { missing = true; return 0; } return previous[k]; };
        try { holds = !!inv.fn(v, prev, dt); } catch (e) { holds = null; } if (missing) holds = null;
      }
      if (holds === null) return;
      if (holds) { inv.good++; inv.bad = 0; if (inv.status !== 'ok' && (inv.status === 'pending' || inv.good >= inv.debounce)) { const was = inv.status; inv.status = 'ok'; if (was === 'violated') { inv.alerting = false; this.emit('info', `${T.asset} invariant '${inv.name}' holds again`, { category: 'PROCESS', asset: T.asset, detail: { invariant: inv.name }, resolve_key: `OXP-018:${T.asset}:${inv.name}` }); } } }
      else { inv.bad++; inv.good = 0; if (inv.bad >= inv.debounce && inv.status !== 'violated') { inv.status = 'violated'; inv.alerting = true; const used = {}; for (const k of Object.keys(values)) if (inv.expr.includes(`'${k}'`)) used[k] = values[k]; this.emit('OXP-018', `${T.asset} invariant '${inv.name}' violated: reported values are physically inconsistent`, { asset: T.asset, dest_ip: T.host, detail: { invariant: inv.name, expr: inv.expr, desc: inv.desc, values: used }, alert_key: `OXP-018:${T.asset}:${inv.name}` }); } }
    }
    /* ---- research controls used by the demo page ---- */
    process() { return this.snapshot || this.poll(now()); }
    fault(plc, spec) { if (!this.sim.plcs[plc]) return { ok: false, error: `unknown PLC ${plc}` }; this.sim.fault(plc, spec); return { ok: true }; }
    engineeringWrite(plc, tag, value) { const p = this.sim.plcs[plc]; if (!p) return { ok: false, error: `unknown PLC ${plc}` }; p.set(tag, +value); return { ok: true }; }
    operatorWrite(plc, tag, value) { return this.sim.write(plc, tag, value, 'supervisor'); }
    summary() { const out = {}; for (const [asset, s] of Object.entries(this.process())) out[asset] = { online: s.online, excursions: Object.values(s.tags).filter((t) => t.status !== 'ok').length }; return out; }
  }
  LiveMonitor.compile = compile;
  LiveMonitor.setClock = (fn) => { now = fn; };
  root.OXPLiveMonitor = LiveMonitor;
  if (root.OXP_DEMO && root.PlantSim && !root.OXP_LIVE) root.OXP_LIVE = new LiveMonitor(root.OXP_DEMO);
})(typeof window !== 'undefined' ? window : globalThis);
