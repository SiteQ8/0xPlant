"""Testbed instrumentation: fault injection for research experiments.

This is NOT part of the plant under protection. It is the lab's "physics
control panel": a small HTTP API on each soft PLC that lets an experiment
inject sensor faults, actuator failures, link losses and communication
blackouts, so detection latency and coverage can be measured reproducibly.
Bind it to a management interface only.

Fault types (POST /fault as JSON):
  {"type": "sensor_stuck",  "tag": "LT-101", "duration_s": 60}
  {"type": "sensor_offset", "tag": "LT-101", "value": 25.0, "duration_s": 60}
  {"type": "sensor_noise",  "tag": "AT-201", "value": 0.3, "duration_s": 60}
  {"type": "actuator_fail", "tag": "P-201",  "duration_s": 60}
  {"type": "link_loss",     "tag": "PLC-002","duration_s": 60}
  {"type": "blackout",      "duration_s": 20}          # PLC stops answering Modbus
  {"type": "identity",      "value": "9.9.9", "duration_s": 0}   # firmware string change
  {"type": "clear"}
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

log = logging.getLogger("plant.faults")
SENSOR_TYPES = ("sensor_stuck", "sensor_offset", "sensor_noise")
ALL_TYPES = SENSOR_TYPES + ("actuator_fail", "link_loss", "blackout", "identity")


class Fault:
    def __init__(self, ftype: str, tag: str = "", value: float = 0.0, duration_s: float = 0.0):
        self.type = ftype
        self.tag = tag
        self.value = value
        self.started = time.time()
        self.expires = self.started + duration_s if duration_s > 0 else None
        self.held: Optional[float] = None       # for sensor_stuck: the frozen value

    @property
    def active(self) -> bool:
        return self.expires is None or time.time() < self.expires

    def to_dict(self) -> dict:
        return {"type": self.type, "tag": self.tag, "value": self.value, "started": self.started, "expires": self.expires, "active": self.active}


class FaultBoard:
    """Holds active faults for one PLC and applies them to the program each scan."""

    def __init__(self, rng=None):
        self.faults: List[Fault] = []
        self.lock = threading.Lock()
        self.rng = rng
        self.injected = 0

    def inject(self, spec: Dict[str, Any]) -> Fault:
        ftype = str(spec.get("type", ""))
        if ftype == "clear":
            with self.lock:
                self.faults.clear()
            return Fault("clear")
        if ftype not in ALL_TYPES:
            raise ValueError(f"unknown fault type {ftype!r}; known: {', '.join(ALL_TYPES)}")
        raw = spec.get("value", 0.0)
        try:
            value = float(raw or 0.0)
        except (TypeError, ValueError):
            value = 0.0                      # non-numeric values (e.g. a firmware string) are used verbatim by the caller
        try:
            duration = float(spec.get("duration_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            raise ValueError("duration_s must be a number")
        f = Fault(ftype, str(spec.get("tag", "")), value, duration)
        with self.lock:
            self.faults = [x for x in self.faults if not (x.type == f.type and x.tag == f.tag)]
            self.faults.append(f)
        self.injected += 1
        log.warning("FAULT injected: %s %s value=%s duration=%s", f.type, f.tag, f.value, spec.get("duration_s"))
        return f

    def prune(self) -> None:
        with self.lock:
            self.faults = [f for f in self.faults if f.active]

    def get(self, ftype: str, tag: str = "") -> Optional[Fault]:
        with self.lock:
            for f in self.faults:
                if f.type == ftype and (not tag or f.tag == tag) and f.active:
                    return f
        return None

    def actuator_failed(self, name: str) -> bool:
        return self.get("actuator_fail", name) is not None

    def apply_sensors(self, program) -> None:
        """Called after the program published its values: corrupt the sensor tags in the Modbus tables."""
        self.prune()
        with self.lock:
            faults = list(self.faults)
        for f in faults:
            if f.type not in SENSOR_TYPES or f.tag not in program.map.by_name:
                continue
            current = program.get(f.tag)
            if f.type == "sensor_stuck":
                if f.held is None:
                    f.held = current
                program.set(f.tag, f.held)
            elif f.type == "sensor_offset":
                program.set(f.tag, current + f.value)
            elif f.type == "sensor_noise":
                program.set(f.tag, current + program.noise(abs(f.value)))

    def snapshot(self) -> List[dict]:
        self.prune()
        with self.lock:
            return [f.to_dict() for f in self.faults]


def start_fault_server(board: FaultBoard, host: str, port: int, plc_name: str, on_identity=None) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "0xPlant-SimControl/2.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        def _json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/faults"):
                return self._json(HTTPStatus.OK, {"plc": plc_name, "faults": board.snapshot(), "injected": board.injected})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            try:
                length = max(0, min(int(self.headers.get("Content-Length", "0") or 0), 65536))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            if self.path != "/fault":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                spec = json.loads(raw or b"{}")
                if not isinstance(spec, dict):
                    raise ValueError("object expected")
                f = board.inject(spec)
                if f.type == "identity" and on_identity:
                    on_identity(str(spec.get("value", "")))
                return self._json(HTTPStatus.OK, {"ok": True, "fault": f.to_dict()})
            except (ValueError, TypeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name=f"sim-control-{plc_name}").start()
    log.info("%s simulation control (fault injection) on http://%s:%d — testbed instrumentation, not part of the plant", plc_name, host, port)
    return httpd
