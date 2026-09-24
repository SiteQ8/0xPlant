"""Soft-PLC runtime: scan cycle, Modbus/TCP server and PLC-to-PLC polling."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

from modbuslite import DataStore, DeviceIdentity, ModbusClient, ModbusServer, codec

from .config import PLCConfig
from .faults import FaultBoard, start_fault_server
from .programs import PROGRAMS, Program
from .registers import ENGINEER_HR, OPERATOR_HR, STATUS_HR

log = logging.getLogger("plant.plc")


class SoftPLC:
    def __init__(self, cfg: PLCConfig, time_scale: float = 60.0, scan_ms: int = 250):
        self.cfg = cfg
        self.time_scale = time_scale
        self.scan_s = scan_ms / 1000.0
        self.store = DataStore(coils=32, discrete=32, holding=256, input=64)
        self.store.write_guard = self._write_guard
        self.store.on_write = self._on_write
        program_cls = PROGRAMS[cfg.program]
        self.program: Program = program_cls(self.store, seed=cfg.seed)
        identity = DeviceIdentity(
            product_code=f"vPLC-{cfg.program.upper()}",
            model_name=cfg.identity.get("model_name", "vPLC-1200"),
            revision=cfg.identity.get("revision", "1.3.2"),
            application_name=cfg.identity.get("application_name", f"{cfg.program.upper()}_v1"),
            vendor_name=cfg.identity.get("vendor_name", "0xPlant Labs"),
            product_name=cfg.identity.get("product_name", "0xPlant Virtual PLC"),
        )
        self.server = ModbusServer(self.store, identity, host=cfg.listen, port=cfg.port,
                                   unit_id=cfg.unit, on_request=self._on_request)
        self.remote_clients: Dict[str, ModbusClient] = {}
        self._stop = asyncio.Event()
        self.writes = 0
        self.faults = FaultBoard()
        self.program.faults = self.faults
        self._sim_httpd = None
        self._base_revision = identity.revision

    # --- Modbus hooks ---
    def _write_guard(self, table: str, address: int, values) -> Optional[int]:
        """Reject writes into the read-only status region. Returns an exception code or None."""
        if table == "holding":
            end = address + len(values) - 1
            if address <= STATUS_HR[1]:
                return codec.EXC_ILLEGAL_ADDRESS
            ok_ranges = (OPERATOR_HR, ENGINEER_HR)
            if not any(lo <= address and end <= hi for lo, hi in ok_ranges):
                return codec.EXC_ILLEGAL_ADDRESS
        if table == "coils" and address + len(values) - 1 > 23:
            return codec.EXC_ILLEGAL_ADDRESS
        return None

    def _on_write(self, table: str, address: int, values, old) -> None:
        self.writes += 1
        if values != old:
            log.info("%s write %s[%d] %s -> %s", self.cfg.name, table, address, old, values)

    def _set_identity(self, revision: str) -> None:
        self.server.identity.revision = revision or self._base_revision
        log.warning("%s firmware revision now reports %s", self.cfg.name, self.server.identity.revision)

    async def _blackout_watch(self) -> None:
        """While a blackout fault is active the PLC answers nobody: connections are dropped and the port closed."""
        dark = False
        while not self._stop.is_set():
            f = self.faults.get("blackout")
            if f and not dark:
                dark = True
                log.warning("%s BLACKOUT: Modbus server stopped", self.cfg.name)
                await self.server.stop()
            elif not f and dark:
                dark = False
                await self.server.start()
                log.warning("%s blackout over: Modbus server back", self.cfg.name)
            if self.faults.get("identity") is None and self.server.identity.revision != self._base_revision:
                self._set_identity(self._base_revision)     # identity fault expired: report the true firmware again
            await asyncio.sleep(0.5)

    def _on_request(self, peer: str, unit: int, req, response: bytes) -> None:
        if req.is_write:
            log.debug("%s %s from %s: %s addr=%d n=%d", self.cfg.name, req.name, peer, "ok" if not response[0] & 0x80 else "rejected",
                      req.address, req.quantity)

    # --- remote polling ---
    async def _poll_remote(self, name: str, host: str, port: int, unit: int) -> None:
        needed = self.program.remote_tags.get(name, [])
        if not needed:
            return
        kwargs = {"local_addr": (self.cfg.source_ip, 0)} if self.cfg.source_ip else {}
        client = ModbusClient(host, port, unit=unit, timeout=1.5, **kwargs)
        self.remote_clients[name] = client
        failures = 0
        while not self._stop.is_set():
            if self.faults.get("link_loss", name):
                if self.program.remote_ok.get(name):
                    log.warning("%s link to %s cut by fault injection", self.cfg.name, name)
                self.program.remote_ok[name] = False
                self.program.remote.pop(name, None)
                await client.close()
                await asyncio.sleep(1.0)
                continue
            try:
                values: Dict[str, float] = {}
                for tag, table, address, scale in needed:
                    if table == "input":
                        raw = (await client.read_input_registers(address, 1))[0]
                    elif table == "holding":
                        raw = (await client.read_holding_registers(address, 1))[0]
                    else:
                        raw = (await client.read_discrete_inputs(address, 1))[0]
                    values[tag] = raw / scale
                self.program.remote[name] = values
                if not self.program.remote_ok.get(name):
                    log.info("%s link to %s established", self.cfg.name, name)
                self.program.remote_ok[name] = True
                failures = 0
            except Exception as exc:  # noqa: BLE001 - keep polling regardless
                failures += 1
                if failures == 3:
                    log.warning("%s link to %s lost: %s", self.cfg.name, name, exc)
                    self.program.remote_ok[name] = False
                    self.program.remote.pop(name, None)   # never keep integrating a stale remote value
                await client.close()
                await asyncio.sleep(min(5.0, 0.5 * failures))
            await asyncio.sleep(1.0)
        await client.close()

    # --- scan loop ---
    async def run(self) -> None:
        await self.server.start()
        log.info("%s running program '%s' (%s)", self.cfg.name, self.program.name, self.program.description)
        if self.cfg.sim_port:
            self._sim_httpd = start_fault_server(self.faults, self.cfg.sim_host, self.cfg.sim_port, self.cfg.name, on_identity=self._set_identity)
        pollers = [asyncio.create_task(self._poll_remote(r.name, r.host, r.port, r.unit)) for r in self.cfg.remotes]
        pollers.append(asyncio.create_task(self._blackout_watch()))
        dt_h = self.scan_s * self.time_scale / 3600.0
        next_scan = time.monotonic()
        try:
            while not self._stop.is_set():
                self.program.execute(dt_h)
                next_scan += self.scan_s
                delay = next_scan - time.monotonic()
                if delay < 0:
                    next_scan = time.monotonic()
                    delay = 0
                await asyncio.sleep(delay)
        finally:
            for p in pollers:
                p.cancel()
            await asyncio.gather(*pollers, return_exceptions=True)
            await self.server.stop()
            if self._sim_httpd:
                self._sim_httpd.shutdown()

    def stop(self) -> None:
        self._stop.set()
