"""Operator HMI: polls the PLCs over Modbus/TCP and serves an operator web screen."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from modbuslite import ModbusClient, ModbusException

from .config import HMIConfig, HMIPLC
from .programs import PROGRAMS
from .registers import TagMap, status_tags

log = logging.getLogger("plant.hmi")
HTML_PATH = os.path.join(os.path.dirname(__file__), "hmi.html")


class PLCPoller:
    def __init__(self, cfg: HMIPLC, source_ip: Optional[str], poll_s: float):
        self.cfg = cfg
        self.map = TagMap(list(PROGRAMS[cfg.program].tags) + status_tags())
        kwargs = {"local_addr": (source_ip, 0)} if source_ip else {}
        self.client = ModbusClient(cfg.host, cfg.port, unit=cfg.unit, timeout=1.5, **kwargs)
        self.poll_s = poll_s
        self.state: Dict[str, Any] = {"name": cfg.name, "program": cfg.program, "online": False,
                                      "tags": {}, "error": "", "ts": 0.0}
        self.ranges = self._plan_reads()

    def _plan_reads(self):
        plan = []
        for table in ("input", "holding", "coils", "discrete"):
            addrs = sorted(t.address for t in self.map if t.table == table)
            if not addrs:
                continue
            # group into contiguous-ish blocks (gap > 8 starts a new block)
            start, prev = addrs[0], addrs[0]
            for a in addrs[1:] + [None]:
                if a is None or a - prev > 8:
                    plan.append((table, start, prev - start + 1))
                    if a is not None:
                        start = a
                if a is not None:
                    prev = a
        return plan

    async def poll_once(self) -> None:
        raw: Dict[str, Dict[int, int]] = {"input": {}, "holding": {}, "coils": {}, "discrete": {}}
        for table, start, count in self.ranges:
            if table == "input":
                vals = await self.client.read_input_registers(start, count)
            elif table == "holding":
                vals = await self.client.read_holding_registers(start, count)
            elif table == "coils":
                vals = await self.client.read_coils(start, count)
            else:
                vals = await self.client.read_discrete_inputs(start, count)
            for i, v in enumerate(vals):
                raw[table][start + i] = v
        tags = {}
        for t in self.map:
            if t.address in raw[t.table]:
                tags[t.name] = self.map.decode(t.name, raw[t.table][t.address])
        self.state.update({"online": True, "tags": tags, "error": "", "ts": time.time()})

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                await self.poll_once()
                failures = 0
            except Exception as exc:  # noqa: BLE001
                failures += 1
                self.state.update({"online": False, "error": f"{type(exc).__name__}: {exc}"})
                await self.client.close()
                if failures in (1, 10):
                    log.warning("%s poll failed: %s", self.cfg.name, exc)
                await asyncio.sleep(min(5.0, failures * 0.5))
            await asyncio.sleep(self.poll_s)
        await self.client.close()

    async def write(self, tag_name: str, value: float) -> None:
        tag = self.map.by_name[tag_name]
        if tag.access != "operator":
            raise PermissionError(f"{tag_name} is not an operator-writable tag")
        if tag.table == "holding":
            await self.client.write_register(tag.address, self.map.encode(tag_name, value))
        elif tag.table == "coils":
            await self.client.write_coil(tag.address, bool(value))
        else:
            raise PermissionError(f"{tag_name} is read-only")


class HMI:
    def __init__(self, cfg: HMIConfig):
        self.cfg = cfg
        self.pollers = {p.name: PLCPoller(p, cfg.source_ip, cfg.poll_ms / 1000.0) for p in cfg.plcs}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop = asyncio.Event()
        self.started = time.time()

    def snapshot(self) -> dict:
        return {
            "ts": time.time(),
            "plcs": {name: dict(p.state, tag_defs=p.map.describe()) for name, p in self.pollers.items()},
        }

    def write(self, plc: str, tag: str, value: float) -> dict:
        if plc not in self.pollers:
            return {"ok": False, "error": f"unknown PLC {plc}"}
        assert self.loop
        fut = asyncio.run_coroutine_threadsafe(self.pollers[plc].write(tag, value), self.loop)
        try:
            fut.result(timeout=3.0)
            log.info("operator write %s %s=%s ok", plc, tag, value)
            return {"ok": True}
        except ModbusException as exc:
            log.warning("operator write %s %s=%s rejected: %s", plc, tag, value, exc)
            return {"ok": False, "error": f"Rejected by device/conduit: {exc}"}
        except Exception as exc:  # noqa: BLE001
            log.warning("operator write %s %s=%s failed: %s", plc, tag, value, exc)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        httpd = ThreadingHTTPServer((self.cfg.listen, self.cfg.port), make_handler(self))
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        log.info("HMI listening on http://%s:%d", self.cfg.listen, self.cfg.port)
        try:
            await asyncio.gather(*(p.run(self.stop) for p in self.pollers.values()))
        finally:
            httpd.shutdown()


def make_handler(hmi: HMI):
    class Handler(BaseHTTPRequestHandler):
        server_version = "0xPlant-HMI/2.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter access log
            log.debug("%s " + fmt, self.address_string(), *args)

        def _json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as fh:
                    body = fh.read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/state"):
                self._json(HTTPStatus.OK, hmi.snapshot())
            elif self.path.startswith("/api/health"):
                self._json(HTTPStatus.OK, {"ok": True, "uptime": time.time() - hmi.started})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/write":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length) or b"{}")
                result = hmi.write(str(data["plc"]), str(data["tag"]), float(data["value"]))
            except (KeyError, ValueError, TypeError) as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad request: {exc}"})
            self._json(HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT, result)

    return Handler
