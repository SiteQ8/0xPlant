#!/usr/bin/env python3
"""Reproducible detection experiments against the local 0xPlant lab.

Each scenario performs one controlled action on the plant or its network
(an engineering change, a refused request, an injected fault, a new IoT
publisher, ...) and measures how long 0xPlant takes to raise the expected
rule. Results are written as JSON and a Markdown summary so runs can be
compared across versions, configurations and repetitions.

    python research/run_experiments.py --runs 3
    python research/run_experiments.py --attach --scenarios config-drift,stuck-sensor

The harness starts the lab itself unless --attach is given. It waits for the
behavioural learning windows before scenarios that depend on them.
"""
from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from modbuslite import DataStore, DeviceIdentity, ModbusClient, ModbusException, ModbusServer  # noqa: E402
from plant.mqtt import MQTTClient  # noqa: E402

CONSOLE = "http://127.0.0.1:8000"
SIM = {"PLC-001": "http://127.0.0.1:9001", "PLC-002": "http://127.0.0.1:9002", "PLC-003": "http://127.0.0.1:9003"}
CONDUIT = {"PLC-001": ("127.0.3.10", 5020), "PLC-002": ("127.0.3.11", 5020), "PLC-003": ("127.0.3.12", 5020)}
HMI_IP, EWS_IP, UNLISTED_IP = "127.0.3.30", "127.0.4.40", "127.0.3.77"


class Console:
    def __init__(self, base: str = CONSOLE):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path: str, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"})
        with self.opener.open(req, timeout=10) as r:
            return json.loads(r.read())

    def login(self):
        self.call("/api/login", {"username": "admin", "password": "Plant@2025"})

    def events_since(self, t0: float, rule: str = "") -> list:
        return self.call(f"/api/events?limit=500&since={t0}&rule={rule}")

    def wait_for_rule(self, rule: str, t0: float, timeout: float) -> Optional[float]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ev = self.events_since(t0, rule)
            if ev:
                return min(e["ts"] for e in ev) - t0
            time.sleep(0.25)
        return None

    def resolve_all(self):
        for a in self.call("/api/alerts?status=open"):
            self.call(f"/api/alerts/{a['id']}/resolve", {})


def fault(plc: str, spec: dict) -> None:
    req = urllib.request.Request(SIM[plc] + "/fault", data=json.dumps(spec).encode(), method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def modbus(plc: str, source: str, fn: Callable) -> None:
    async def go():
        try:
            async with ModbusClient(*CONDUIT[plc], local_addr=(source, 0)) as c:
                await fn(c)
        except ModbusException:
            pass
    asyncio.run(go())


def ews_write(plc: str, tag_value: str) -> None:
    subprocess.run([sys.executable, "-m", "plant", "-c", "config/plant.local.yaml", "ews", "--plc", plc, "write", tag_value], cwd=ROOT, check=False, capture_output=True)


@dataclass
class Scenario:
    key: str
    title: str
    rule: str
    action: Callable[[], None]
    cleanup: Callable[[], None] = lambda: None
    timeout: float = 30.0
    needs_baseline: bool = False
    category: str = "security"
    notes: str = ""


def scenarios() -> List[Scenario]:
    rogue_server = {}

    def start_rogue():
        async def go():
            srv = ModbusServer(DataStore(), DeviceIdentity(vendor_name="Unknown", model_name="Field-Gateway-X"), "127.0.1.25", 5020)
            await srv.start()
            rogue_server["stop"] = asyncio.Event()
            await rogue_server["stop"].wait()
            await srv.stop()
        import threading
        loop = asyncio.new_event_loop()
        rogue_server["loop"] = loop
        threading.Thread(target=lambda: loop.run_until_complete(go()), daemon=True).start()

    def stop_rogue():
        if "stop" in rogue_server:
            rogue_server["loop"].call_soon_threadsafe(rogue_server["stop"].set)

    def mqtt_new_topic():
        async def go():
            c = MQTTClient("127.0.3.50", 1883, client_id="exp-sensor")
            await c.connect()
            await c.publish("plant/intake/VIB-999", json.dumps({"sensor": "VIB-999", "vibration_mm_s": 9.9}).encode())
            await c.close()
        asyncio.run(go())

    return [
        Scenario("config-drift", "Engineering change to an interlock limit (EWS)", "OXP-006",
                 lambda: ews_write("PLC-002", "AAHH_LIMIT=3.5"), lambda: ews_write("PLC-002", "AAHH_LIMIT=4.0"), category="integrity",
                 notes="Legitimate but unreviewed change: golden configuration drift"),
        Scenario("setpoint-change", "Operator setpoint change (HMI)", "OXP-013",
                 lambda: modbus("PLC-003", HMI_IP, lambda c: c.write_register(100, 420)), lambda: modbus("PLC-003", HMI_IP, lambda c: c.write_register(100, 400)), category="operations"),
        Scenario("write-out-of-range", "HMI writes an engineering register", "OXP-002",
                 lambda: modbus("PLC-002", HMI_IP, lambda c: c.write_register(200, 500)), notes="Refused by the conduit: Illegal Data Address"),
        Scenario("unlisted-host", "Host outside the conduit policy reads a PLC", "OXP-003",
                 lambda: modbus("PLC-002", UNLISTED_IP, lambda c: c.read_holding_registers(0, 1))),
        Scenario("identification-denied", "HMI reads device identification", "OXP-001",
                 lambda: modbus("PLC-001", HMI_IP, lambda c: c.read_device_identification())),
        Scenario("new-pattern", "Permitted host reads a register block it never read before", "OXP-017",
                 lambda: modbus("PLC-002", HMI_IP, lambda c: c.read_holding_registers(150, 8)), needs_baseline=True, category="anomaly"),
        Scenario("mqtt-new-topic", "Unknown IoT publisher appears on the broker", "OXP-019", mqtt_new_topic, needs_baseline=True, category="iot"),
        Scenario("stuck-sensor", "Level transmitter stuck while the intake pump fails", "OXP-018",
                 lambda: (fault("PLC-001", {"type": "actuator_fail", "tag": "P-101", "duration_s": 60}), fault("PLC-001", {"type": "sensor_stuck", "tag": "LT-101", "duration_s": 60})),
                 lambda: fault("PLC-001", {"type": "clear"}), timeout=45, category="integrity", notes="Mass-balance invariant catches a frozen sensor that hides a draining tank"),
        Scenario("dosing-failure", "Chlorine dosing pump fails", "OXP-005",
                 lambda: fault("PLC-002", {"type": "actuator_fail", "tag": "P-201", "duration_s": 90}), lambda: fault("PLC-002", {"type": "clear"}), timeout=80, category="integrity",
                 notes="Residual decays below the safe envelope; detection time is dominated by process dynamics"),
        Scenario("plc-blackout", "PLC stops answering (power/network loss)", "OXP-016",
                 lambda: fault("PLC-003", {"type": "blackout", "duration_s": 20}), timeout=30, category="availability"),
        Scenario("plc-offline-integrity", "Integrity monitor notices the silent PLC", "OXP-007",
                 lambda: fault("PLC-003", {"type": "blackout", "duration_s": 20}), timeout=30, category="availability"),
        Scenario("firmware-change", "Device reports a different firmware revision", "OXP-008",
                 lambda: fault("PLC-002", {"type": "identity", "value": "9.9.9", "duration_s": 60}), lambda: fault("PLC-002", {"type": "clear"}), timeout=45, category="integrity"),
        Scenario("rogue-device", "Unknown Modbus device appears on Level 1", "OXP-009", start_rogue, stop_rogue, timeout=90, category="network",
                 notes="Bounded by the discovery interval (60 s locally)"),
    ]


def run(args) -> dict:
    console = Console()
    console.login()
    all_scenarios = {s.key: s for s in scenarios()}
    selected = [all_scenarios[k] for k in (args.scenarios.split(",") if args.scenarios else all_scenarios)]
    results: Dict[str, list] = {s.key: [] for s in selected}
    quiet_t0 = time.time()
    if any(s.needs_baseline for s in selected):
        wait = max(0.0, args.learning_s - (time.time() - args.lab_started))
        if wait:
            print(f"waiting {wait:.0f}s for the behavioural learning windows to close")
            time.sleep(wait)
    # OXP-010 (cleartext MQTT broker) is a design finding about the plant itself, not a false alert
    quiet_alerts = [e["rule"] for e in console.events_since(quiet_t0) if e["severity"] != "info" and e["rule"] not in ("OXP-010",)]
    print(f"false alerts during the quiet period: {quiet_alerts or 'none'}")
    for run_no in range(args.runs):
        for s in selected:
            console.resolve_all()
            t0 = time.time()
            latency, error = None, ""
            try:
                s.action()
                latency = console.wait_for_rule(s.rule, t0, s.timeout)
            except Exception as exc:  # noqa: BLE001 - one broken scenario must not abort the campaign
                error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    s.cleanup()
                except Exception:  # noqa: BLE001
                    pass
            results[s.key].append(latency)
            status = '%.2fs' % latency if latency is not None else ('ERROR ' + error if error else 'NOT DETECTED within %ss' % s.timeout)
            print(f"run {run_no + 1}/{args.runs}  {s.key:24s} {s.rule}  {status}", flush=True)
            time.sleep(args.settle)
    summary = []
    for s in selected:
        lat = [x for x in results[s.key] if x is not None]
        summary.append({"scenario": s.key, "title": s.title, "rule": s.rule, "category": s.category, "runs": len(results[s.key]),
                        "detected": len(lat), "detection_rate": len(lat) / max(1, len(results[s.key])),
                        "latency_mean_s": round(statistics.mean(lat), 2) if lat else None, "latency_median_s": round(statistics.median(lat), 2) if lat else None,
                        "latency_max_s": round(max(lat), 2) if lat else None, "timeout_s": s.timeout, "notes": s.notes, "raw": results[s.key]})
    return {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runs": args.runs, "learning_s": args.learning_s,
            "false_alerts_quiet_period": quiet_alerts, "scenarios": summary}


def write_report(report: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    stamp = report["generated"].replace(":", "").replace("-", "")
    jpath = os.path.join(out_dir, f"experiments-{stamp}.json")
    with open(jpath, "w") as fh:
        json.dump(report, fh, indent=2)
    lines = [f"# 0xPlant detection experiments — {report['generated']}", "",
             f"Runs per scenario: {report['runs']} · learning window: {report['learning_s']} s · false alerts in the quiet period: {report['false_alerts_quiet_period'] or 'none'}", "",
             "| Scenario | Rule | Category | Detected | Mean latency (s) | Median (s) | Max (s) | Notes |", "|---|---|---|---|---|---|---|---|"]
    for s in report["scenarios"]:
        lines.append(f"| {s['title']} | {s['rule']} | {s['category']} | {s['detected']}/{s['runs']} | {s['latency_mean_s'] if s['latency_mean_s'] is not None else '—'} | "
                     f"{s['latency_median_s'] if s['latency_median_s'] is not None else '—'} | {s['latency_max_s'] if s['latency_max_s'] is not None else '—'} | {s['notes']} |")
    mpath = os.path.join(out_dir, f"experiments-{stamp}.md")
    with open(mpath, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return mpath


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--scenarios", default="", help="comma separated scenario keys (default: all)")
    ap.add_argument("--attach", action="store_true", help="use an already running local lab instead of starting one")
    ap.add_argument("--learning-s", type=float, default=45.0, help="baseline learning window configured on the sensor")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds between scenarios")
    ap.add_argument("--out", default=os.path.join(ROOT, "research", "results"))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for s in scenarios():
            print(f"{s.key:24s} {s.rule}  {s.title}")
        return 0
    lab = None
    args.lab_started = time.time()
    if not args.attach:
        for f in ("oxplant-local.db", "oxplant-local.db-wal", "oxplant-local.db-shm"):
            try:
                os.remove(os.path.join(ROOT, "data", f))
            except FileNotFoundError:
                pass
        lab = subprocess.Popen([sys.executable, "scripts/run_local.py", "--no-hmi"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("lab starting…")
        time.sleep(25)
    try:
        report = run(args)
    finally:
        if lab:
            lab.send_signal(signal.SIGTERM)
            lab.wait(timeout=20)
    path = write_report(report, args.out)
    print(f"\nreport: {path}")
    print(open(path).read())
    return 0


if __name__ == "__main__":
    sys.exit(main())
