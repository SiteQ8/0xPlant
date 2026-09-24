/* 0xPlant Water Works: browser port of the plant physics and PLC programs (plant/programs.py).
   Used by the GitHub Pages demo so the operator HMI runs a real control system in the browser.
   Register/tag semantics follow plant/registers.py; values are engineering units (no Modbus scaling). */
(function (root) {
  'use strict';
  function Rng(seed) { let s = (seed >>> 0) || 1; this.next = () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; }; }
  function gauss(rng) { const u = Math.max(rng.next(), 1e-12), v = rng.next(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }

  class Program {
    constructor(name, seed, noise) { this.name = name; this.rng = new Rng(seed); this.noiseOn = noise !== false; this.t = {}; this.remote = {}; this.remoteOk = {}; this.simHours = 0; this.scan = 0; this.interlockWord = 0; this.alarmWord = 0; this.faults = []; this.defaults(); }
    noise(sigma) { return this.noiseOn ? gauss(this.rng) * sigma : 0; }
    get(n) { return this.t[n] || 0; } set(n, v) { this.t[n] = typeof v === 'boolean' ? (v ? 1 : 0) : v; }
    coil(n) { return !!this.t[n]; } consume(n) { const v = !!this.t[n]; if (v) this.t[n] = 0; return v; }
    clamp(n, lo, hi) { const v = this.get(n), c = Math.max(lo, Math.min(hi, v)); if (c !== v) this.set(n, c); return c; }
    remoteValue(plc, tag, d) { return (this.remoteOk[plc] && this.remote[plc] && this.remote[plc][tag] != null) ? this.remote[plc][tag] : d; }
    resetRequested() { const a = this.consume('ALARM_RESET'), b = this.consume('RESET_CMD'); return a || b; }
    actuatorFailed(name) { return this.faults.some((f) => f.type === 'actuator_fail' && f.tag === name && f.active()); }
    execute(dt) { this.scan++; this.simHours += dt; this.logic(dt); this.physics(dt); this.publish(); this.applySensorFaults(); this.set('PLC_MODE', 1); this.set('SCAN_COUNT', this.scan & 0xFFFF); this.set('INTERLOCK_WORD', this.interlockWord); this.set('ALARM_WORD', this.alarmWord); this.set('UPTIME_MIN', Math.floor(this.simHours * 60)); this.set('SIM_HOUR', this.simHours % 24); }
    applySensorFaults() { this.faults = this.faults.filter((f) => f.active()); for (const f of this.faults) { if (!(f.tag in this.t)) continue; const cur = this.get(f.tag); if (f.type === 'sensor_stuck') { if (f.held == null) f.held = cur; this.set(f.tag, f.held); } else if (f.type === 'sensor_offset') this.set(f.tag, cur + f.value); else if (f.type === 'sensor_noise') this.set(f.tag, cur + this.noise(Math.abs(f.value))); } }
    fault(spec) { if (spec.type === 'clear') { this.faults = []; return; } const exp = spec.duration_s ? Date.now() + spec.duration_s * 1000 : null; this.faults = this.faults.filter((f) => !(f.type === spec.type && f.tag === spec.tag)); this.faults.push({ type: spec.type, tag: spec.tag || '', value: +spec.value || 0, held: null, active: () => exp == null || Date.now() < exp }); }
  }

  class Intake extends Program {
    defaults() { this.TANK = 500; this.PUMP = 150; this.volume = 0.55 * this.TANK; this.level = 55; this.flow = 0; this.turbidity = 3.2; this.speedFb = 0; this.current = 0; this.pumpRun = false; this.valveOpen = false; this.lahh = false; this.draw = 0; this.river = 2; this.speedCmd = 0;
      Object.assign(this.t, { LEVEL_START_SP: 40, LEVEL_STOP_SP: 85, PUMP_SPEED_SP: 80, LAHH_LIMIT: 95, TURB_HIGH_LIMIT: 20, PUMP_MAX_SPEED: 100, LALL_LIMIT: 10, AUTO_MODE: 1 }); }
    logic(dt) { const lahhL = this.clamp('LAHH_LIMIT', 60, 99), maxS = this.clamp('PUMP_MAX_SPEED', 20, 100), stopSp = this.clamp('LEVEL_STOP_SP', 20, lahhL - 2), startSp = this.clamp('LEVEL_START_SP', 5, stopSp - 5), speedSp = this.clamp('PUMP_SPEED_SP', 0, maxS); this.clamp('LALL_LIMIT', 2, startSp - 2); this.clamp('TURB_HIGH_LIMIT', 1, 200);
      if (this.resetRequested() && this.level < lahhL - 2) this.lahh = false; if (this.level >= lahhL) this.lahh = true;
      const auto = this.coil('AUTO_MODE');
      if (auto) { this.consume('PUMP_START_CMD'); this.consume('PUMP_STOP_CMD'); if (this.level < startSp) this.pumpRun = true; else if (this.level > stopSp) this.pumpRun = false; this.valveOpen = this.pumpRun; }
      else { if (this.consume('PUMP_START_CMD')) this.pumpRun = true; if (this.consume('PUMP_STOP_CMD')) this.pumpRun = false; this.valveOpen = this.coil('VALVE_OPEN_CMD'); }
      if (this.lahh || this.actuatorFailed('P-101')) this.pumpRun = false; this.speedCmd = this.pumpRun ? speedSp : 0;
      this.interlockWord = this.lahh ? 1 : 0; this.alarmWord = (this.turbidity > this.get('TURB_HIGH_LIMIT') ? 2 : 0) | (this.level < this.get('LALL_LIMIT') ? 4 : 0); }
    physics(dt) { this.speedFb += (this.speedCmd - this.speedFb) * Math.min(1, dt * 60); if (this.speedFb < 0.5) this.speedFb = 0; this.flow = this.valveOpen ? this.PUMP * this.speedFb / 100 : 0;
      const draw = this.level > 2 ? this.remoteValue('PLC-002', 'FT-201', 0) : 0; this.volume = Math.max(0, Math.min(this.TANK, this.volume + (this.flow - draw) * dt)); this.level = this.volume / this.TANK * 100;
      this.river = 2 + 0.3 * Math.sin(this.simHours / 4) + this.noise(0.01); const tt = 3 + ((this.simHours % 31) > 2 && (this.simHours % 31) < 3.5 ? 12 : 0);
      this.turbidity = Math.max(0.1, this.turbidity + (tt - this.turbidity) * Math.min(1, dt * 4) + this.noise(0.05)); this.current = this.speedFb > 0 ? 12 + 30 * this.speedFb / 100 + this.noise(0.2) : 0; this.draw = draw; }
    publish() { this.set('LT-101', this.level); this.set('FT-101', this.flow); this.set('AT-101', this.turbidity); this.set('SC-101', this.speedFb); this.set('LT-100', this.river); this.set('IT-101', this.current); this.set('FT-201-R', this.draw); this.set('PUMP_RUNNING', this.speedFb > 0); this.set('VALVE_OPEN', this.valveOpen); this.set('LAHH_TRIP', this.lahh); this.set('TURB_HIGH', !!(this.alarmWord & 2)); this.set('LALL_ALARM', !!(this.alarmWord & 4)); this.set('AUTO_ACTIVE', this.coil('AUTO_MODE')); this.set('REMOTE_OK_002', !!this.remoteOk['PLC-002']); }
  }

  class Treatment extends Program {
    defaults() { this.flow = 0; this.residual = 1.4; this.ph = 7.2; this.dp = 12; this.contact = 55; this.stroke = 30; this.dosingRun = false; this.transferRun = false; this.bwLeft = 0; this.aahh = false; this.prevErr = 0; this.cwHold = false; this.upLevel = 0; this.cwLevel = 0; this.seq = 3; this.seqTimer = 0; this.flowCmd = 0;
      Object.assign(this.t, { CL2_SP: 1.5, FLOW_SP: 120, BACKWASH_DP_SP: 60, MANUAL_STROKE_SP: 40, AAHH_LIMIT: 4, AALL_LIMIT: 0.5, PI_KP: 2, PI_TI: 60, MAX_STROKE: 100, CLEARWELL_STOP: 88, CLEARWELL_RESTART: 70, AUTO_MODE: 1 }); }
    sequence(dt, start, stop, available) { const flowSp = this.get('FLOW_SP'); if (stop && this.seq !== 0 && this.seq !== 4) { this.seq = 4; this.seqTimer = 0; } else if (start && this.seq === 0) { this.seq = 1; this.seqTimer = 0; }
      if (this.seq === 1) { if (available && this.upLevel > 15) { this.seq = 2; this.seqTimer = 0; } } else if (this.seq === 2) { this.seqTimer += dt; if (this.flow > 0.8 * flowSp) { this.seq = 3; this.seqTimer = 0; } else if (this.seqTimer > 1) this.seq = 5; } else if (this.seq === 3) { if (!available) { this.seq = 5; this.seqTimer = 0; } } else if (this.seq === 5) { if (available && this.upLevel > 15) { this.seq = 2; this.seqTimer = 0; } } else if (this.seq === 4) { this.seqTimer += dt; if (this.flow < 1 || this.seqTimer > 0.2) this.seq = 0; } }
    logic(dt) { const aahh = this.clamp('AAHH_LIMIT', 2, 8), cl2Sp = this.clamp('CL2_SP', 0.2, aahh - 0.5), flowSp = this.clamp('FLOW_SP', 20, 200), bwSp = this.clamp('BACKWASH_DP_SP', 20, 100), maxStroke = this.clamp('MAX_STROKE', 10, 100), manStroke = this.clamp('MANUAL_STROKE_SP', 0, maxStroke); this.clamp('AALL_LIMIT', 0.1, cl2Sp); const kp = this.clamp('PI_KP', 0.1, 20), ti = this.clamp('PI_TI', 5, 600);
      if (this.resetRequested() && this.residual < aahh - 0.5) this.aahh = false; if (this.residual >= aahh) this.aahh = true;
      const upLevel = this.remoteValue('PLC-001', 'LT-101', 0); const available = upLevel > 3 && !!this.remoteOk['PLC-001']; const cwStop = this.clamp('CLEARWELL_STOP', 50, 97), cwRestart = this.clamp('CLEARWELL_RESTART', 20, cwStop - 5); const cw = this.remoteValue('PLC-003', 'LT-301', 50); if (cw >= cwStop) this.cwHold = true; else if (cw <= cwRestart) this.cwHold = false;
      const auto = this.coil('AUTO_MODE'); if (this.consume('BACKWASH_CMD') && this.bwLeft <= 0) this.bwLeft = 0.1; if (this.dp >= bwSp && this.bwLeft <= 0) this.bwLeft = 0.1; const bw = this.bwLeft > 0;
      const start = this.consume('PLANT_START_CMD'), stop = this.consume('PLANT_STOP_CMD');
      if (auto) { this.upLevel = upLevel; this.sequence(dt, start, stop, available); this.transferRun = (this.seq === 2 || this.seq === 3) && available && !bw && !this.cwHold; } else this.transferRun = this.coil('TRANSFER_RUN_CMD') && available && !bw;
      this.flowCmd = this.transferRun ? flowSp : 0; const wanted = this.flow > 5 && (!auto || this.seq === 3);
      if (auto) { this.dosingRun = wanted && !this.aahh; if (this.dosingRun) { const dts = dt * 3600, err = cl2Sp - this.residual; this.stroke += (kp * (err - this.prevErr) + kp / ti * err * dts) * 10; this.prevErr = err; this.stroke = Math.max(0, Math.min(maxStroke, this.stroke)); } }
      else { this.dosingRun = this.coil('DOSING_ENABLE_CMD') && wanted && !this.aahh; this.stroke = manStroke; }
      if (this.actuatorFailed('P-201')) this.dosingRun = false; if (this.actuatorFailed('P-202')) { this.transferRun = false; this.flowCmd = 0; } if (!this.dosingRun) this.prevErr = 0;
      this.interlockWord = this.aahh ? 1 : 0; const low = this.dosingRun && this.residual < this.get('AALL_LIMIT'); this.alarmWord = (low ? 2 : 0) | (this.dp > bwSp * 0.9 ? 4 : 0); }
    physics(dt) { if (this.bwLeft > 0) { this.bwLeft -= dt; this.dp = Math.max(8, this.dp - 600 * dt); } this.flow += (this.flowCmd - this.flow) * Math.min(1, dt * 30); if (this.flow < 0.5) this.flow = 0;
      const applied = (this.dosingRun && this.flow > 0) ? (this.stroke / 100) * 6 * (120 / Math.max(this.flow, 10)) : 0; const demand = 0.8 + this.noise(0.02); const eq = this.flow > 0 ? Math.max(0, applied - demand) : this.residual * 0.98;
      this.residual = Math.max(0, this.residual + (eq - this.residual) * Math.min(1, dt / 0.2) + this.noise(0.005)); this.ph = Math.max(6.5, Math.min(8.5, this.ph + (7.2 - this.ph) * dt + this.noise(0.005)));
      if (this.bwLeft <= 0) this.dp += this.flow * dt * 0.15 + this.noise(0.02); this.dp = Math.max(8, this.dp); const tl = 40 + 30 * this.flow / 120; this.contact += (tl - this.contact) * Math.min(1, dt * 6) + this.noise(0.05);
      this.upLevel = this.remoteValue('PLC-001', 'LT-101', 0); this.cwLevel = this.remoteValue('PLC-003', 'LT-301', 0); }
    publish() { this.set('FT-201', this.flow); this.set('AT-201', this.residual); this.set('AT-202', this.ph); this.set('PDT-201', this.dp); this.set('LT-201', this.contact); this.set('SC-201', this.dosingRun ? this.stroke : 0); this.set('LT-101-R', this.upLevel); this.set('LT-301-R', this.cwLevel); this.set('SEQ_STEP', this.seq); this.set('PLANT_RUNNING', this.seq === 3); this.set('DOSING_RUNNING', this.dosingRun); this.set('BACKWASH_ACTIVE', this.bwLeft > 0); this.set('AAHH_TRIP', this.aahh); this.set('AALL_ALARM', !!(this.alarmWord & 2)); this.set('DP_HIGH', !!(this.alarmWord & 4)); this.set('AUTO_ACTIVE', this.coil('AUTO_MODE')); this.set('REMOTE_OK_001', !!this.remoteOk['PLC-001']); this.set('TRANSFER_RUNNING', this.flow > 0.5); this.set('REMOTE_OK_003', !!this.remoteOk['PLC-003']); this.set('CLEARWELL_HOLD', this.cwHold); }
  }

  class Distribution extends Program {
    defaults() { this.TANK = 800; this.PUMP = 110; this.volume = 0.6 * this.TANK; this.level = 60; this.pressure = 3.8; this.flow = 0; this.demand = 60; this.speed = [0, 0]; this.run = [false, false]; this.speedCmd = 0; this.lagTimer = 0; this.lall = false; this.pahh = false; this.integral = 0; this.inflow = 0;
      Object.assign(this.t, { PRESSURE_SP: 4, LAG_START_DELTA: 0.5, LALL_LIMIT: 10, PAHH_LIMIT: 6, PALL_LIMIT: 2.5, MAX_SPEED: 100, AUTO_MODE: 1 }); }
    logic(dt) { const pahh = this.clamp('PAHH_LIMIT', 3, 8), sp = this.clamp('PRESSURE_SP', 2, pahh - 1), delta = this.clamp('LAG_START_DELTA', 0.1, 2), lall = this.clamp('LALL_LIMIT', 2, 40), maxS = this.clamp('MAX_SPEED', 30, 100); this.clamp('PALL_LIMIT', 0.5, sp - 0.5);
      if (this.resetRequested()) { if (this.level > lall + 5) this.lall = false; if (this.pressure < pahh - 0.5) this.pahh = false; } if (this.level <= lall) this.lall = true; if (this.pressure >= pahh) this.pahh = true; const tripped = this.lall || this.pahh;
      const auto = this.coil('AUTO_MODE');
      if (auto) { this.consume('STOP_ALL_CMD'); this.run[0] = !tripped; const err = sp - this.pressure, dts = dt * 3600; this.integral = Math.max(-50, Math.min(50, this.integral + err * dts * 0.05)); this.speedCmd = Math.max(0, Math.min(maxS, 70 + err * 25 + this.integral));
        if (this.pressure < sp - delta && this.run[0]) this.lagTimer++; else if (this.pressure >= sp - delta / 4 && this.demand < this.PUMP * 0.8) this.lagTimer--; this.lagTimer = Math.max(-40, Math.min(10, this.lagTimer)); if (this.lagTimer >= 10) this.run[1] = true; if (this.lagTimer <= -40) this.run[1] = false; this.run[1] = this.run[1] && !tripped; }
      else { if (this.consume('STOP_ALL_CMD')) { this.set('P301_RUN_CMD', 0); this.set('P302_RUN_CMD', 0); } this.run[0] = this.coil('P301_RUN_CMD') && !tripped; this.run[1] = this.coil('P302_RUN_CMD') && !tripped; this.speedCmd = maxS * 0.85; }
      if (tripped) this.run = [false, false]; if (this.actuatorFailed('P-301')) this.run[0] = false; if (this.actuatorFailed('P-302')) this.run[1] = false;
      this.interlockWord = (this.lall ? 1 : 0) | (this.pahh ? 2 : 0); this.alarmWord = (this.pressure < this.get('PALL_LIMIT') && (this.run[0] || this.run[1])) ? 4 : 0; }
    physics(dt) { const hour = this.simHours % 24; const base = 60 + 35 * Math.sin(2 * Math.PI * (hour - 8) / 24) + 20 * Math.sin(4 * Math.PI * (hour - 8) / 24); this.demand = Math.max(20, Math.min(160, base + this.noise(1)));
      for (let i = 0; i < 2; i++) { const target = this.run[i] ? this.speedCmd : 0; this.speed[i] += (target - this.speed[i]) * Math.min(1, dt * 60); if (this.speed[i] < 0.5) this.speed[i] = 0; }
      const cap = this.level > 0.5 ? this.speed.reduce((a, s) => a + this.PUMP * s / 100, 0) : 0; const head = Math.max(...this.speed.map((s) => 6.5 * (s / 100) ** 2), 0); this.flow = Math.min(cap, this.demand); const deficit = Math.max(0, this.demand - cap);
      const tp = Math.max(0, head - 0.00012 * this.flow ** 2 - deficit * 0.02); this.pressure += (tp - this.pressure) * Math.min(1, dt * 40) + this.noise(0.005); this.pressure = Math.max(0, this.pressure);
      const inflow = this.remoteValue('PLC-002', 'FT-201', 0); this.volume = Math.max(0, Math.min(this.TANK, this.volume + (inflow - this.flow) * dt)); this.level = this.volume / this.TANK * 100; this.inflow = inflow; }
    publish() { this.set('LT-301', this.level); this.set('PT-301', this.pressure); this.set('FT-301', this.flow); this.set('SC-301', this.speed[0]); this.set('SC-302', this.speed[1]); this.set('FQ-301', this.demand); this.set('FT-201-R', this.inflow); this.set('P301_RUNNING', this.speed[0] > 0); this.set('P302_RUNNING', this.speed[1] > 0); this.set('LALL_TRIP', this.lall); this.set('PAHH_TRIP', this.pahh); this.set('PALL_ALARM', !!(this.alarmWord & 4)); this.set('AUTO_ACTIVE', this.coil('AUTO_MODE')); this.set('REMOTE_OK_002', !!this.remoteOk['PLC-002']); }
  }

  const ACCESS = { operator: ['LEVEL_START_SP', 'LEVEL_STOP_SP', 'PUMP_SPEED_SP', 'AUTO_MODE', 'PUMP_START_CMD', 'PUMP_STOP_CMD', 'VALVE_OPEN_CMD', 'CL2_SP', 'FLOW_SP', 'BACKWASH_DP_SP', 'MANUAL_STROKE_SP', 'DOSING_ENABLE_CMD', 'BACKWASH_CMD', 'TRANSFER_RUN_CMD', 'PRESSURE_SP', 'LAG_START_DELTA', 'P301_RUN_CMD', 'P302_RUN_CMD', 'STOP_ALL_CMD'], supervisor: ['RESET_CMD', 'PLANT_START_CMD', 'PLANT_STOP_CMD'] };

  class PlantSim {
    constructor(opts = {}) { this.timeScale = opts.timeScale || 60; this.scanS = opts.scanS || 0.25; this.noise = opts.noise !== false; this.plcs = { 'PLC-001': new Intake('intake', opts.seed || 11, this.noise), 'PLC-002': new Treatment('treatment', (opts.seed || 11) * 2, this.noise), 'PLC-003': new Distribution('distribution', (opts.seed || 11) * 3, this.noise) }; this.links = { 'PLC-001': ['PLC-002'], 'PLC-002': ['PLC-001', 'PLC-003'], 'PLC-003': ['PLC-002'] }; this.offline = {}; }
    step(n = 1) { const dt = this.scanS * this.timeScale / 3600; for (let i = 0; i < n; i++) { for (const [name, p] of Object.entries(this.plcs)) { for (const r of this.links[name]) { const ok = !this.offline[r] && !p.faults.some((f) => f.type === 'link_loss' && f.tag === r && f.active()); p.remoteOk[r] = ok; if (ok) p.remote[r] = Object.assign({}, this.plcs[r].t); else delete p.remote[r]; } if (!this.offline[name]) p.execute(dt); } } return this; }
    state(tagDefs) { const plcs = {}; for (const [name, p] of Object.entries(this.plcs)) plcs[name] = { name, program: p.name, online: !this.offline[name], tags: this.offline[name] ? {} : Object.assign({}, p.t), error: this.offline[name] ? 'blackout' : '', ts: Date.now() / 1000, tag_defs: (tagDefs && tagDefs[name]) || [] }; return { ts: Date.now() / 1000, plcs }; }
    write(plc, tag, value, role = 'supervisor') { const p = this.plcs[plc]; if (!p) return { ok: false, error: `unknown PLC ${plc}` }; const need = ACCESS.supervisor.includes(tag) ? 'supervisor' : ACCESS.operator.includes(tag) ? 'operator' : null; if (!need) return { ok: false, error: `${tag} is not writable from the HMI` }; const rank = { viewer: 0, operator: 1, supervisor: 2 }; if (rank[role] < rank[need]) return { ok: false, error: `${tag} needs the ${need} role` }; p.set(tag, +value); return { ok: true }; }
    fault(plc, spec) { if (spec.type === 'blackout') { this.offline[plc] = true; setTimeout(() => { this.offline[plc] = false; }, (spec.duration_s || 10) * 1000); return; } this.plcs[plc].fault(spec); }
  }
  root.PlantSim = PlantSim; root.PlantSimPrograms = { Intake, Treatment, Distribution };
})(typeof window !== 'undefined' ? window : globalThis);
