"""The browser port of the plant (plant/plantsim.js), the in-browser HMI backend (plant/hmi_demo.js)
and the in-browser integrity monitor (oxplant/ui_live.js) must behave like their Python originals.

These tests run the JavaScript under Node and are skipped when Node is not installed.
"""
import json
import os
import shutil
import subprocess

import pytest

from modbuslite import DataStore
from plant.programs import Distribution, Intake, Treatment

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

TAGS = ["LT-101", "FT-101", "AT-101", "SC-101", "FT-201", "AT-201", "PDT-201", "LT-201", "SC-201", "SEQ_STEP",
        "PT-301", "LT-301", "FT-301", "SC-301", "SC-302", "FQ-301"]
CHECKPOINTS = (50, 100, 200, 400, 800, 1200)
DT = 50 * 600 / 3600 / 1000          # 50 ms scans at 600x, as in the Node run below
LINKS = {"PLC-001": ["PLC-002"], "PLC-002": ["PLC-001", "PLC-003"], "PLC-003": ["PLC-002"]}


def node(script: str, *args: str) -> dict:
    out = subprocess.run([NODE, "-e", script, "--", *args], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def python_run() -> dict:
    def make(cls):
        p = cls(DataStore(coils=32, discrete=32, holding=256, input=64), seed=1)
        p.noise = lambda sigma: 0.0
        return p
    plcs = {"PLC-001": make(Intake), "PLC-002": make(Treatment), "PLC-003": make(Distribution)}
    out = {}
    for scan in range(1, max(CHECKPOINTS) + 1):
        for name, p in plcs.items():
            for r in LINKS[name]:
                p.remote_ok[r] = True
                p.remote[r] = {t.name: plcs[r].get(t.name) for t in plcs[r].map}
            p.execute(DT)
        if scan in CHECKPOINTS:
            out[str(scan)] = {n: {t: p.get(t) for t in TAGS if t in p.map.by_name} for n, p in plcs.items()}
    return out


def test_javascript_plant_matches_python_programs():
    js = node("""
      require('./plant/plantsim.js');
      const sim = new globalThis.PlantSim({ noise: false, scanS: 0.05, timeScale: 600 });
      const TAGS = %s, out = {}; let done = 0;
      for (const cp of %s) { sim.step(cp - done); done = cp; out[cp] = {};
        for (const [n, p] of Object.entries(sim.plcs)) { out[cp][n] = {}; for (const t of TAGS) if (t in p.t) out[cp][n][t] = +p.t[t]; } }
      console.log(JSON.stringify(out));
    """ % (json.dumps(TAGS), json.dumps(list(CHECKPOINTS))))
    py = python_run()
    mismatches = []
    for cp, plcs in py.items():
        for plc, tags in plcs.items():
            for tag, v in tags.items():
                w = js[cp][plc].get(tag)
                if w is None or abs(v - w) > max(0.02 * abs(v), 0.3):   # Modbus scaling quantises the Python values
                    mismatches.append((cp, plc, tag, v, w))
    assert not mismatches, mismatches
    assert py["1200"]["PLC-002"]["SEQ_STEP"] == 3 and js["1200"]["PLC-002"]["FT-201"] > 100


def test_browser_hmi_backend_roles_alarms_and_sequence():
    r = node("""
      require('./plant/plantsim.js'); require('./plant/hmi_demo.js');
      const defs = { 'PLC-003': [{ name: 'PAHH_TRIP', table: 'discrete', desc: 'Interlock: Network pressure high-high interlock' }] };
      const B = new globalThis.HMIDemoBackend({ autostart: false, tagDefs: defs, noise: false });
      const out = { me: B.api('/api/me') };
      try { B.api('/api/state'); out.unauth = 'allowed'; } catch (e) { out.unauth = e.message; }
      out.badLogin = B.api('/api/login', { username: 'operator', password: 'wrong' }).ok;
      B.api('/api/login', { username: 'operator', password: 'Operator@2025' });
      for (let i = 0; i < 200; i++) B.tick(1);
      out.opStop = B.api('/api/write', { plc: 'PLC-002', tag: 'PLANT_STOP_CMD', value: 1 });
      out.opSp = B.api('/api/write', { plc: 'PLC-001', tag: 'PUMP_SPEED_SP', value: 60 });
      out.opCfg = B.api('/api/write', { plc: 'PLC-001', tag: 'LAHH_LIMIT', value: 99 });
      B.api('/api/logout', {}); B.api('/api/login', { username: 'supervisor', password: 'Supervisor@2025' });
      out.supStop = B.api('/api/write', { plc: 'PLC-002', tag: 'PLANT_STOP_CMD', value: 1 });
      for (let i = 0; i < 60; i++) B.tick(1);
      out.seqAfterStop = B.api('/api/state').plcs['PLC-002'].tags.SEQ_STEP;
      out.flowAfterStop = B.api('/api/state').plcs['PLC-002'].tags['FT-201'];
      B.api('/api/write', { plc: 'PLC-002', tag: 'PLANT_START_CMD', value: 1 });
      for (let i = 0; i < 120; i++) B.tick(1);
      out.seqAfterStart = B.api('/api/state').plcs['PLC-002'].tags.SEQ_STEP;
      B.fault('PLC-003', { type: 'sensor_offset', tag: 'PT-301', value: 2.5, duration_s: 600 });
      for (let i = 0; i < 20; i++) B.tick(1);
      const st = B.api('/api/state'); out.pahh = st.plcs['PLC-003'].tags.PAHH_TRIP; out.p301 = st.plcs['PLC-003'].tags.P301_RUNNING;
      const al = B.api('/api/alarms'); out.alarms = al.alarms.map((a) => [a.plc, a.tag, a.active, a.acked]); out.unacked = al.unacked;
      out.ack = B.api('/api/alarms/ack', { plc: 'PLC-003', tag: 'PAHH_TRIP' });
      out.unackedAfter = B.api('/api/alarms').unacked;
      out.history = Object.keys(B.api('/api/history?plc=PLC-003&tags=PT-301,LT-301&seconds=60').series);
      console.log(JSON.stringify(out));
    """)
    assert r["me"]["login_required"] and not r["me"]["authenticated"]
    assert r["unauth"] == "login required" and r["badLogin"] is False
    assert not r["opStop"]["ok"] and "supervisor" in r["opStop"]["error"]
    assert r["opSp"]["ok"] and not r["opCfg"]["ok"]
    assert r["supStop"]["ok"] and r["seqAfterStop"] == 0 and r["flowAfterStop"] < 1
    assert r["seqAfterStart"] == 3
    assert r["pahh"] == 1 and r["p301"] == 0                    # the PLC acts on the spoofed transmitter
    assert r["alarms"] == [["PLC-003", "PAHH_TRIP", True, False]] and r["unacked"] == 1
    assert r["ack"]["acknowledged"] == 1 and r["unackedAfter"] == 0
    assert r["history"] == ["PT-301", "LT-301"]


def test_browser_integrity_monitor_detects_injected_faults_without_false_alerts():
    with open(os.path.join(ROOT, "docs", "demo-data.js"), encoding="utf-8") as fh:
        head = fh.read(20)
    assert head.startswith("window.OXP_DEMO")
    r = node("""
      const fs = require('fs');
      require('./plant/plantsim.js'); require('./oxplant/ui_live.js');
      const D = JSON.parse(fs.readFileSync('docs/demo-data.js', 'utf8').split('=').slice(1).join('=').trim().replace(/;$/, ''));
      D.events = []; D.alerts = []; D.changes = [];
      let clock = 1000; globalThis.OXPLiveMonitor.setClock(() => clock);
      const L = new globalThis.OXPLiveMonitor(D, { autostart: false, noise: false });
      const run = (sec) => { for (let i = 0; i < sec; i++) { clock += 1; L.tick(4); } };   // 4 scans of 250 ms per second at 60x
      const keys = () => D.alerts.filter((a) => a.status !== 'resolved').map((a) => a.key);
      const out = {};
      run(240); out.quiet = keys(); out.invariants = Object.values(L.process()).flatMap((s) => Object.values(s.invariants).map((i) => i.status));
      for (let i = 0; i < 600 && L.process()['PLC-001'].tags['FT-201-R'].value < 100; i++) run(1);   // wait until treatment draws from T-101 (not in a hold or backwash)
      L.fault('PLC-001', { type: 'actuator_fail', tag: 'P-101', duration_s: 3600 }); L.fault('PLC-001', { type: 'sensor_stuck', tag: 'LT-101', duration_s: 3600 });
      run(30); out.afterStuck = keys();
      L.engineeringWrite('PLC-002', 'AAHH_LIMIT', 3.5); L.operatorWrite('PLC-003', 'PRESSURE_SP', 4.2); run(3);
      out.afterWrites = keys(); out.changes = D.changes.map((c) => [c.tag, c.kind, c.status]);
      L.setGolden('PLC-002', 'AAHH_LIMIT', 3.5); run(3); out.afterApprove = keys();
      L.fault('PLC-002', { type: 'actuator_fail', tag: 'P-201', duration_s: 3600 }); run(150); out.afterDosing = keys();
      out.eventRules = D.events.map((e) => e.rule).filter(Boolean);
      console.log(JSON.stringify(out));
    """)
    assert r["quiet"] == [] and set(r["invariants"]) == {"ok"}, r
    assert "OXP-018:PLC-001:intake-mass-balance" in r["afterStuck"]
    assert "OXP-006:PLC-002:AAHH_LIMIT" in r["afterWrites"]
    assert ["AAHH_LIMIT", "config", "unreviewed"] in r["changes"] and ["PRESSURE_SP", "setpoint", "logged"] in r["changes"]
    assert "OXP-013" in r["eventRules"]
    assert "OXP-005:PLC-002:AT-201" in r["afterDosing"]
