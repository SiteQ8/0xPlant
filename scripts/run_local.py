#!/usr/bin/env python3
"""Run the whole 0xPlant lab on loopback without Docker.

Starts three PLCs, the 0xPlant sensor (conduits), the 0xPlant console and the
operator HMI as child processes and stops them all on Ctrl-C.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", default="config/plant.local.yaml")
    ap.add_argument("--oxplant", default="config/oxplant.local.yaml")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = run until Ctrl-C)")
    ap.add_argument("--no-hmi", action="store_true")
    args = ap.parse_args()
    py = sys.executable
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONUNBUFFERED="1")
    procs = []

    def on_term(signum, frame):      # make SIGTERM behave like Ctrl-C so children are always stopped
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    def start(label, cmd):
        p = subprocess.Popen(cmd, cwd=ROOT, env=env)
        procs.append((label, p))
        print(f"[run_local] started {label} (pid {p.pid})")

    for plc in ("PLC-001", "PLC-002", "PLC-003"):
        start(plc, [py, "-m", "plant", "-c", args.plant, "plc", "--name", plc])
    time.sleep(1.0)
    start("console", [py, "-m", "oxplant", "-c", args.oxplant, "console"])
    time.sleep(1.0)
    start("sensor", [py, "-m", "oxplant", "-c", args.oxplant, "sensor", "--name", "SENSOR-001"])
    time.sleep(1.0)
    if not args.no_hmi:
        start("hmi", [py, "-m", "plant", "-c", args.plant, "hmi"])
    print("\n[run_local] 0xPlant console: http://127.0.0.1:8000  (admin / Plant@2025)")
    print("[run_local] Operator HMI:     http://127.0.0.1:8080")
    print("[run_local] Ctrl-C to stop\n")
    deadline = time.time() + args.duration if args.duration else None
    try:
        while True:
            for label, p in procs:
                if p.poll() is not None:
                    print(f"[run_local] {label} exited with {p.returncode}")
                    raise KeyboardInterrupt
            if deadline and time.time() > deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for label, p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for label, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[run_local] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
