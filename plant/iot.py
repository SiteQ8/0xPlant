"""Virtual IoT sensors: condition-monitoring devices that publish plant telemetry over MQTT.

They are the Level 0/1 IoT layer of the water works: vibration and bearing
temperature on each pump, ambient conditions, and a flow totaliser. Payloads
are JSON on topics plant/<area>/<sensor>. They read the PLCs' running bits
over Modbus so vibration follows real pump state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from typing import Dict, List, Optional

from modbuslite import ModbusClient

from .mqtt import MQTTClient

log = logging.getLogger("plant.iot")

SENSORS = [
    # id, area, kind, plc, running discrete input
    ("VIB-101", "intake", "vibration", "PLC-001", 0),
    ("TT-101", "intake", "bearing_temp", "PLC-001", 0),
    ("VIB-201", "treatment", "vibration", "PLC-002", 7),
    ("TT-201", "treatment", "bearing_temp", "PLC-002", 7),
    ("VIB-301", "distribution", "vibration", "PLC-003", 0),
    ("TT-301", "distribution", "bearing_temp", "PLC-003", 0),
    ("ENV-001", "site", "ambient", "", -1),
    ("ENV-002", "chlorine-room", "gas", "", -1),
]


class IoTFleet:
    def __init__(self, broker_host: str, broker_port: int, plcs: Dict[str, tuple], interval_s: float = 2.0,
                 source_ip: Optional[str] = None, seed: int = 7):
        self.broker = (broker_host, broker_port)
        self.plcs = plcs                                   # name -> (host, port)
        self.interval = interval_s
        self.source_ip = source_ip
        self.rng = random.Random(seed)
        self.state: Dict[str, float] = {}
        self.running: Dict[str, bool] = {}
        self.stop = asyncio.Event()
        self.published = 0

    async def _poll_plcs(self) -> None:
        clients = {}
        for name, (host, port) in self.plcs.items():
            kwargs = {"local_addr": (self.source_ip, 0)} if self.source_ip else {}
            clients[name] = ModbusClient(host, port, timeout=1.5, **kwargs)
        while not self.stop.is_set():
            for name, c in clients.items():
                try:
                    bits = await c.read_discrete_inputs(0, 8)
                    for sid, _area, _kind, plc, di in SENSORS:
                        if plc == name and di >= 0:
                            self.running[sid] = bool(bits[di])
                except Exception:  # noqa: BLE001
                    await c.close()
            await asyncio.sleep(1.0)
        for c in clients.values():
            await c.close()

    def _sample(self, sid: str, kind: str, t: float) -> dict:
        s = self.state
        run = self.running.get(sid, False)
        if kind == "vibration":
            target = (2.8 + self.rng.random() * 0.4) if run else 0.15
            s[sid] = s.get(sid, target) + (target - s.get(sid, target)) * 0.3 + self.rng.gauss(0, 0.03)
            return {"vibration_mm_s": round(max(0.0, s[sid]), 3), "running": run}
        if kind == "bearing_temp":
            target = 58.0 + 6 * math.sin(t / 900) if run else 24.0
            s[sid] = s.get(sid, target) + (target - s.get(sid, target)) * 0.05 + self.rng.gauss(0, 0.05)
            return {"temperature_c": round(s[sid], 2), "running": run}
        if kind == "ambient":
            return {"temperature_c": round(28 + 6 * math.sin(t / 3600) + self.rng.gauss(0, 0.1), 2), "humidity_pct": round(45 + 10 * math.cos(t / 5000), 1)}
        return {"cl2_ppm": round(max(0.0, 0.2 + self.rng.gauss(0, 0.03)), 3), "alarm": False}

    async def run(self) -> None:
        poller = asyncio.create_task(self._poll_plcs())
        client = MQTTClient(self.broker[0], self.broker[1], client_id="iot-fleet-001")
        backoff = 1.0
        try:
            while not self.stop.is_set():
                try:
                    if not client.connected:
                        await client.connect()
                        log.info("IoT fleet connected to broker %s:%d", *self.broker)
                        backoff = 1.0
                    t = time.time()
                    for sid, area, kind, _plc, _di in SENSORS:
                        payload = dict(self._sample(sid, kind, t), sensor=sid, ts=round(t, 3), fw="iot-1.4.2")
                        await client.publish(f"plant/{area}/{sid}", json.dumps(payload).encode())
                        self.published += 1
                    await asyncio.sleep(self.interval)
                except Exception as exc:  # noqa: BLE001
                    log.warning("IoT fleet: broker unreachable (%s); retrying in %.0fs", exc, backoff)
                    await client.close()
                    await asyncio.sleep(backoff)
                    backoff = min(15.0, backoff * 2)
        finally:
            poller.cancel()
            await client.close()
