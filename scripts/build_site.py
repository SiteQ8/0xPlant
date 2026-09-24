#!/usr/bin/env python3
"""Build the GitHub Pages demo (docs/) from the real console and HMI code.

Runs the local lab, generates a few realistic findings, captures the console
API and HMI state over time, and writes a self-contained static site that
replays that data in the browser (no backend needed).
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
    call(HMI, "/api/write", {"plc": "PLC-003", "tag": "PRESSURE_SP", "value": 4.2})
    call(HMI, "/api/write", {"plc": "PLC-002", "tag": "CL2_SP", "value": 1.6})
    sys.path.insert(0, ROOT)
    from modbuslite import ModbusClient, ModbusException

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
    process, states = [], []
    t_end = time.time() + seconds
    while time.time() < t_end:
        process.append(call(CONSOLE, "/api/process"))
        states.append(call(HMI, "/api/state"))
        time.sleep(1.0)
    data = {"session": None, "summary": call(CONSOLE, "/api/summary"), "assets": call(CONSOLE, "/api/assets"),
            "events": call(CONSOLE, "/api/events?limit=400"), "alerts": call(CONSOLE, "/api/alerts?limit=200"),
            "changes": call(CONSOLE, "/api/changes?limit=200"), "flows": call(CONSOLE, "/api/flows"), "protocols": call(CONSOLE, "/api/protocols"),
            "policy": call(CONSOLE, "/api/policy"), "process": process, "audit": call(CONSOLE, "/api/audit?limit=200"),
            "sensors": call(CONSOLE, "/api/sensors"), "settings": call(CONSOLE, "/api/settings"), "rules": call(CONSOLE, "/api/rules")}
    call(CONSOLE, "/api/logout", {})
    return data, {"states": states}


def assemble(data: dict, hmi: dict) -> None:
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "demo-data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.OXP_DEMO = " + json.dumps(data, separators=(",", ":")) + ";\n")
    with open(os.path.join(DOCS, "hmi-demo-data.js"), "w", encoding="utf-8") as fh:
        fh.write("window.HMI_DEMO = " + json.dumps(hmi, separators=(",", ":")) + ";\n")
    shutil.copy(os.path.join(ROOT, "oxplant", "ui.js"), os.path.join(DOCS, "app.js"))
    html = open(os.path.join(ROOT, "oxplant", "ui.html"), encoding="utf-8").read()
    html = html.replace('<script src="/app.js"></script>', '<script src="demo-data.js"></script>\n<script src="app.js"></script>')
    html = html.replace("<title>0xPlant — ICS/OT Security Console</title>", "<title>0xPlant — ICS/OT Security Console (live demo)</title>")
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(html)
    hmi_html = open(os.path.join(ROOT, "plant", "hmi.html"), encoding="utf-8").read()
    hmi_html = hmi_html.replace("<script>\nconst fmt=", '<script src="hmi-demo-data.js"></script>\n<script>\nconst fmt=')
    hmi_html = hmi_html.replace("<footer>0xPlant Water Works ·", '<footer><a href="index.html" style="color:#38BDF8">← 0xPlant console demo</a> · 0xPlant Water Works ·')
    open(os.path.join(DOCS, "hmi.html"), "w", encoding="utf-8").write(hmi_html)
    open(os.path.join(DOCS, ".nojekyll"), "w").close()
    print(f"site written to {DOCS}: {len(data['events'])} events, {len(data['alerts'])} alerts, {len(data['process'])} process snapshots, {len(hmi['states'])} HMI frames")


def main() -> int:
    if "--no-capture" in sys.argv:
        data = json.loads(open(os.path.join(DOCS, "demo-data.js")).read().split("=", 1)[1].rstrip(";\n"))
        hmi = json.loads(open(os.path.join(DOCS, "hmi-demo-data.js")).read().split("=", 1)[1].rstrip(";\n"))
        assemble(data, hmi)
        return 0
    for f in ("oxplant-local.db", "oxplant-local.db-wal", "oxplant-local.db-shm"):
        try:
            os.remove(os.path.join(ROOT, "data", f))
        except FileNotFoundError:
            pass
    lab = subprocess.Popen([sys.executable, "scripts/run_local.py"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(24)
        generate_findings()
        time.sleep(8)
        data, hmi = capture()
    finally:
        lab.send_signal(signal.SIGTERM)
        lab.wait(timeout=20)
    assemble(data, hmi)
    return 0


if __name__ == "__main__":
    sys.exit(main())
