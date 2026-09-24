"""Event and alert model, plus the sensor-to-console forwarder."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .rules import RULES

log = logging.getLogger("oxplant.events")

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Event:
    title: str
    severity: str = "info"
    category: str = "SYSTEM"
    rule: Optional[str] = None
    source_ip: str = ""
    dest_ip: str = ""
    asset: str = ""
    protocol: str = ""
    sensor: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    alert_key: Optional[str] = None      # when set, the console raises or updates an alert
    resolve_key: Optional[str] = None    # when set, the console resolves that alert
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_rule(cls, rule_id: str, title: str = "", **kw) -> "Event":
        r = RULES[rule_id]
        kw.setdefault("severity", r.severity)
        kw.setdefault("category", r.category)
        return cls(title=title or r.title, rule=rule_id, **kw)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        allowed = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**allowed)


class EventBus:
    """Synchronous fan-out of events to subscribers (store, forwarder, outputs)."""

    def __init__(self) -> None:
        self._subs: List[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subs.append(fn)

    def publish(self, event: Event) -> None:
        for fn in self._subs:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not lose the event for others
                log.exception("event subscriber failed")


class Forwarder:
    """Batches events, flow records and discovered assets and POSTs them to the console."""

    def __init__(self, console_url: str, token: str, sensor: str, interval: float = 2.0, max_queue: int = 20000):
        self.url = console_url.rstrip("/") + "/api/ingest"
        self.token = token
        self.sensor = sensor
        self.interval = interval
        self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="oxplant-forwarder", daemon=True)
        self.delivered = 0
        self.failed = 0
        self.last_error = ""

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _put(self, kind: str, payload: dict) -> None:
        try:
            self.q.put_nowait((kind, payload))
        except queue.Full:
            self.failed += 1

    def event(self, ev: Event) -> None:
        d = ev.to_dict()
        d.setdefault("sensor", self.sensor)
        if not d["sensor"]:
            d["sensor"] = self.sensor
        self._put("events", d)

    def flow(self, record: dict) -> None:
        self._put("flows", record)

    def asset(self, record: dict) -> None:
        self._put("assets", record)

    def _drain(self) -> Dict[str, list]:
        batch: Dict[str, list] = {"events": [], "flows": [], "assets": []}
        while len(batch["events"]) + len(batch["flows"]) + len(batch["assets"]) < 500:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break
            batch[kind].append(payload)
        return batch

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._drain()
            if any(batch.values()):
                self._send(batch)
            else:
                self._send({"events": [], "flows": [], "assets": [], "heartbeat": True})
            self._stop.wait(self.interval)

    def _send(self, batch: dict) -> None:
        body = json.dumps(dict(batch, sensor=self.sensor, ts=time.time())).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}",
            "X-Requested-With": "oxplant-sensor"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            self.delivered += len(batch.get("events", []))
            self.last_error = ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failed += 1
            if self.last_error != str(exc):
                log.warning("console %s unreachable: %s (buffering)", self.url, exc)
            self.last_error = str(exc)
            # requeue so nothing is lost while the console is down
            for kind in ("events", "flows", "assets"):
                for payload in batch.get(kind, []):
                    self._put(kind, payload)
