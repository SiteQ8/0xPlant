#!/usr/bin/env python3
"""Build the GitHub Pages demo (docs/) from the real console and HMI code.

Runs the local lab, generates a few realistic findings and captures the console
API (inventory, events, alerts, changes, flows, policy, audit...). The site then
runs the plant itself in the browser: the operator HMI is backed by the browser
port of the PLC programs (plant/plantsim.js + plant/hmi_demo.js) and the console's
process page and dashboard are fed by the same plant through a browser port of the
integrity monitor (oxplant/ui_live.js), so faults injected on the page are detected
live by the same rules the console applies to the real PLCs.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
CONSOLE, HMI = "http://127.0.0.1:8000", "http://127.0.0.1:8080"
DEMO_USERS = [{"username": "operator", "password": "Operator@2025", "role": "operator"},
              {"username": "supervisor", "password": "Supervisor@2025", "role": "supervisor"}]


def client():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(base, path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"})
        with opener.open(req, timeout=10) as r:
            return json.loads(r.read())
    return call


def generate_findings():
    """Legitimate engineering and operator actions plus a few refused requests, so the demo has something to show."""
    subprocess.run([sys.executable, "-m", "plant", "-c", "config/plant.local.yaml", "ews", "--plc", "PLC-002", "write", "AAHH_LIMIT=3.5"], cwd=ROOT, check=False, capture_output=True)
    subprocess.run([sys.executable, "-m", "plant", "-c", "config/plant.local.yaml", "ews", "--plc", "PLC-001", "write", "PUMP_MAX_SPEED=90"], cwd=ROOT, check=False, capture_output=True)
    call = client()
    call(HMI, "/api/login", {"username": "operator", "password": "Operator@2025"})
    call(HMI, "/api/write", {"plc": "PLC-003", "tag": "PRESSURE_SP", "value": 4.2})
    call(HMI, "/api/write", {"plc": "PLC-002", "tag": "CL2_SP", "value": 1.6})
    sys.path.insert(0, ROOT)
    from modbuslite import ModbusClient, ModbusException
    from plant.mqtt import MQTTClient

    # research detections: a frozen level transmitter while the intake pump fails (OXP-018 invariant),
    # an unknown IoT publisher (OXP-019) and a permitted host reading a block it never read before (OXP-017)
    for spec in ({"type": "actuator_fail", "tag": "P-101", "duration_s": 45}, {"type": "sensor_stuck", "tag": "LT-101", "duration_s": 45}):
        req = urllib.request.Request("http://127.0.0.1:9001/fault", data=json.dumps(spec).encode(), method="POST", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()

    async def iot_and_pattern():
        c = MQTTClient("127.0.3.50", 1883, client_id="contractor-laptop")
        await c.connect()
        await c.publish("plant/intake/VIB-999", json.dumps({"sensor": "VIB-999", "vibration_mm_s": 9.9, "fw": "unknown"}).encode())
        await c.close()
        try:
            async with ModbusClient("127.0.3.11", 5020, local_addr=("127.0.3.30", 0)) as m:
                await m.read_holding_registers(150, 8)
        except ModbusException:
            pass
    asyncio.run(iot_and_pattern())

    async def refused():
        for src, host, fn in (("127.0.3.77", "127.0.3.11", lambda c: c.read_holding_registers(0, 1)),
                              ("127.0.3.30", "127.0.3.11", lambda c: c.write_register(200, 500)),
                              ("127.0.3.30", "127.0.3.10", lambda c: c.read_device_identification())):
            try:
                async with ModbusClient(host, 5020, local_addr=(src, 0)) as c:
                    await fn(c)
            except ModbusException:
                pass
    asyncio.run(refused())


def capture(seconds: float = 40.0) -> tuple[dict, dict]:
    call = client()
    call(CONSOLE, "/api/login", {"username": "admin", "password": "Plant@2025"})
    call(HMI, "/api/login", {"username": "operator", "password": "Operator@2025"})
    process, states = [], []
    t_end = time.time() + seconds
    while time.time() < t_end:
        process.append(call(CONSOLE, "/api/process"))
        states.append(call(HMI, "/api/state"))
        time.sleep(1.0)
    sys.path.insert(0, ROOT)
    from oxplant.config import load as load_oxplant
    cfg = load_oxplant(os.path.join(ROOT, "config", "oxplant.local.yaml"))
    invariants = {t.asset: [{"name": i.name, "expr": i.expr, "desc": i.desc, "debounce": i.debounce} for i in t.invariants] for t in cfg.integrity.targets}
    data = {"session": None, "summary": call(CONSOLE, "/api/summary"), "assets": call(CONSOLE, "/api/assets"),
            "events": call(CONSOLE, "/api/events?limit=400"), "alerts": call(CONSOLE, "/api/alerts?limit=200"),
            "changes": call(CONSOLE, "/api/changes?limit=200"), "flows": call(CONSOLE, "/api/flows"), "protocols": call(CONSOLE, "/api/protocols"),
            "policy": call(CONSOLE, "/api/policy"), "process": process, "audit": call(CONSOLE, "/api/audit?limit=200"),
            "sensors": call(CONSOLE, "/api/sensors"), "settings": call(CONSOLE, "/api/settings"), "rules": call(CONSOLE, "/api/rules"),
            "invariants": invariants}
    call(CONSOLE, "/api/logout", {})
    return data, {"states": states}


def tag_defs() -> dict:
    """Tag descriptions per PLC program (units, access levels, alarm texts) for the browser HMI backend."""
    sys.path.insert(0, ROOT)
    from plant.config import load as load_plant
    from plant.programs import PROGRAMS
    from plant.registers import TagMap, status_tags
    cfg = load_plant(os.path.join(ROOT, "config", "plant.local.yaml"))
    return {p.name: TagMap(list(PROGRAMS[p.program].tags) + status_tags()).describe() for p in cfg.hmi.plcs}


def assemble(data: dict, hmi: dict) -> None:
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "demo-data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.OXP_DEMO = " + json.dumps(data, separators=(",", ":")) + ";\n")
    with open(os.path.join(DOCS, "hmi-demo-data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.HMI_TAGDEFS = " + json.dumps(hmi["tag_defs"], separators=(",", ":")) + ";\n")
        fh.write("window.HMI_DEMO_CONFIG = " + json.dumps({"timeScale": 60, "scanS": 0.25, "users": hmi["users"]}, separators=(",", ":")) + ";\n")
    shutil.copy(os.path.join(ROOT, "oxplant", "ui.js"), os.path.join(DOCS, "app.js"))
    shutil.copy(os.path.join(ROOT, "oxplant", "ui_live.js"), os.path.join(DOCS, "app-live.js"))
    shutil.copy(os.path.join(ROOT, "plant", "plantsim.js"), os.path.join(DOCS, "plantsim.js"))
    shutil.copy(os.path.join(ROOT, "plant", "hmi_demo.js"), os.path.join(DOCS, "hmi-demo.js"))
    html = open(os.path.join(ROOT, "oxplant", "ui.html"), encoding="utf-8").read()
    html = html.replace('<script src="/app.js"></script>', '<script src="demo-data.js"></script>\n<script src="plantsim.js"></script>\n<script src="app-live.js"></script>\n<script src="app.js"></script>')
    html = html.replace("<title>0xPlant — ICS/OT Security Console</title>", "<title>0xPlant — ICS/OT Security Console (live demo)</title>")
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(html)
    hmi_html = open(os.path.join(ROOT, "plant", "hmi.html"), encoding="utf-8").read()
    hmi_html = hmi_html.replace("<script>\nconst fmt=", '<script src="plantsim.js"></script>\n<script src="hmi-demo-data.js"></script>\n<script src="hmi-demo.js"></script>\n<script>\nconst fmt=')
    hmi_html = hmi_html.replace("<footer>0xPlant Water Works ·", '<footer><a href="index.html" style="color:#38BDF8">← 0xPlant console demo</a> · 0xPlant Water Works ·')
    open(os.path.join(DOCS, "hmi.html"), "w", encoding="utf-8").write(hmi_html)
    open(os.path.join(DOCS, ".nojekyll"), "w").close()
    print(f"site written to {DOCS}: {len(data['events'])} events, {len(data['alerts'])} alerts, {len(data['process'])} process snapshots, {len(hmi['states'])} HMI frames captured, browser plant with {len(hmi['tag_defs'])} PLC programs")


def main() -> int:
    if "--no-capture" in sys.argv:
        data = json.loads(open(os.path.join(DOCS, "demo-data.js")).read().split("=", 1)[1].rstrip(";\n"))
        assemble(data, {"states": [], "tag_defs": tag_defs(), "users": DEMO_USERS})
        return 0
    for f in ("oxplant-local.db", "oxplant-local.db-wal", "oxplant-local.db-shm"):
        try:
            os.remove(os.path.join(ROOT, "data", f))
        except FileNotFoundError:
            pass
    lab = subprocess.Popen([sys.executable, "scripts/run_local.py"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(52)                 # start-up plus the 45 s behavioural learning windows
        generate_findings()
        time.sleep(10)
        data, hmi = capture()
        hmi.update(tag_defs=tag_defs(), users=DEMO_USERS)
    finally:
        lab.send_signal(signal.SIGTERM)
        lab.wait(timeout=20)
    assemble(data, hmi)
    return 0


if __name__ == "__main__":
    sys.exit(main())
