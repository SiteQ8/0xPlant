"""MQTT monitor: subscribes to the plant's IoT broker and baselines topics, publishers and payloads.

Runs inside a sensor. During the learning window it records every topic and
sensor id it sees and the numeric ranges of each payload field. Afterwards a
new topic or sensor id raises OXP-019 and an unparsable payload or a value far
outside the learned range raises OXP-020. Message counts are reported to the
console as MQTT flows so the Protocols page shows the IoT traffic profile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Set

from plant.mqtt import MQTTClient

from .events import Event, EventBus

log = logging.getLogger("oxplant.mqttmon")


class MQTTMonitor:
    def __init__(self, cfg: Dict[str, Any], bus: EventBus, sensor: str):
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.port = int(cfg.get("port", 1883))
        self.learning_s = float(cfg.get("learning_s", 60))
        self.margin = float(cfg.get("margin", 1.0))      # allowed excursion beyond the learned range, as a fraction of it
        self.consecutive = int(cfg.get("consecutive", 3)) # samples outside the range before reporting
        self._outside: Dict[str, int] = {}
        self.asset = str(cfg.get("asset", "IOT-BROKER"))
        self.bus = bus
        self.sensor = sensor
        self.started = time.time()
        self.topics: Set[str] = set()
        self.publishers: Set[str] = set()
        self.ranges: Dict[str, Dict[str, list]] = {}       # topic -> field -> [min, max]
        self.counts: Dict[str, int] = {}
        self.reported: Set[str] = set()
        self.messages = 0
        self.connected = False

    @property
    def learning(self) -> bool:
        return time.time() - self.started < self.learning_s

    def _emit(self, ev: Event) -> None:
        ev.sensor = self.sensor
        ev.asset = ev.asset or self.asset
        ev.protocol = "MQTT"
        ev.dest_ip = self.host
        self.bus.publish(ev)

    def observe(self, topic: str, payload: bytes) -> None:
        self.messages += 1
        self.counts[topic] = self.counts.get(topic, 0) + 1
        try:
            data = json.loads(payload.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("payload is not an object")
        except (ValueError, UnicodeDecodeError) as exc:
            if not self.learning and f"parse:{topic}" not in self.reported:
                self.reported.add(f"parse:{topic}")
                self._emit(Event.from_rule("OXP-020", f"Unparsable IoT payload on {topic}: {exc}", detail={"topic": topic, "payload": payload[:64].hex()},
                                           alert_key=f"OXP-020:{topic}:parse"))
            return
        sensor_id = str(data.get("sensor", ""))
        if self.learning:
            self.topics.add(topic)
            if sensor_id:
                self.publishers.add(sensor_id)
            rng = self.ranges.setdefault(topic, {})
            for k, v in data.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "ts":
                    lo, hi = rng.get(k, [v, v])
                    rng[k] = [min(lo, v), max(hi, v)]
            return
        if topic not in self.topics and f"topic:{topic}" not in self.reported:
            self.reported.add(f"topic:{topic}")
            self._emit(Event.from_rule("OXP-019", f"New MQTT topic {topic} (publisher {sensor_id or 'unknown'}) not seen during learning",
                                       detail={"topic": topic, "sensor": sensor_id, "fields": sorted(data)}, alert_key=f"OXP-019:{topic}"))
            return
        if sensor_id and sensor_id not in self.publishers and f"pub:{sensor_id}" not in self.reported:
            self.reported.add(f"pub:{sensor_id}")
            self._emit(Event.from_rule("OXP-019", f"New IoT publisher {sensor_id} on known topic {topic}", detail={"topic": topic, "sensor": sensor_id},
                                       alert_key=f"OXP-019:{sensor_id}"))
        for k, v in data.items():
            rng = self.ranges.get(topic, {}).get(k)
            if rng is None or not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            lo, hi = rng
            span = max(hi - lo, abs(hi) * 0.1, 0.5)
            key = f"range:{topic}:{k}"
            if lo - span * self.margin <= v <= hi + span * self.margin:
                self._outside[key] = 0
                rng[0], rng[1] = min(lo, v), max(hi, v)          # slow drift within the margin widens the range (adaptive baseline)
                continue
            self._outside[key] = self._outside.get(key, 0) + 1
            if self._outside[key] >= self.consecutive and key not in self.reported:
                self.reported.add(key)
                self._emit(Event.from_rule("OXP-020", f"IoT value {k}={v} on {topic} is outside the learned range {lo:.3g}…{hi:.3g}",
                                           detail={"topic": topic, "field": k, "value": v, "learned": [lo, hi]}, alert_key=f"OXP-020:{topic}:{k}"))

    def flow_records(self) -> list:
        now = time.time()
        return [{"source_ip": topic, "source_asset": topic.split("/")[-1], "asset": self.asset, "conduit": "MQTT-MONITOR",
                 "protocol": "MQTT", "first_seen": self.started, "last_seen": now, "requests": n, "denied": 0,
                 "functions": {"PUBLISH": n}, "baseline": "learning" if self.learning else "enforcing"} for topic, n in self.counts.items()]

    async def run(self, stop: asyncio.Event) -> None:
        client = MQTTClient(self.host, self.port, client_id=f"oxplant-{self.sensor}")
        backoff = 2.0
        while not stop.is_set():
            try:
                await client.connect()
                await client.subscribe("#")
                self.connected = True
                log.info("MQTT monitor subscribed to %s:%d (learning %.0fs)", self.host, self.port, self.learning_s)
                backoff = 2.0
                async for topic, payload, _retained in client.messages():
                    self.observe(topic, payload)
                    if stop.is_set():
                        break
            except Exception as exc:  # noqa: BLE001
                if self.connected:
                    log.warning("MQTT monitor lost the broker: %s", exc)
                self.connected = False
                await client.close()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(30.0, backoff * 1.5)
        await client.close()
