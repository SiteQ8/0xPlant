"""Sensor: runs protective conduits and discovery close to the process, reports to the console."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List

from .conduit import Conduit
from .config import Config
from .discovery import Discovery
from .events import Event, EventBus, Forwarder

log = logging.getLogger("oxplant.sensor")


class Sensor:
    def __init__(self, cfg: Config, name: str):
        self.cfg = cfg
        self.name = name
        scfg = cfg.sensor(name)
        self.bus = EventBus()
        self.forwarder = Forwarder(scfg.console_url or cfg.console.url, cfg.console.sensor_token, name)
        self.bus.subscribe(self.forwarder.event)
        self.bus.subscribe(lambda e: log.log(logging.WARNING if e.severity != "info" else logging.INFO,
                                             "[%s] %s %s", e.severity.upper(), e.rule or "-", e.title))
        wanted = [c for c in cfg.conduits if not scfg.conduits or c.id in scfg.conduits]
        self.conduits: List[Conduit] = [
            Conduit(c.policy, (c.listen_host, c.listen_port), (c.upstream_host, c.upstream_port), self.bus, name)
            for c in wanted]
        self.discovery = Discovery(cfg.discovery, cfg.assets, self.bus.publish, self.forwarder.asset,
                                   runner=name, zone_for_ip=cfg.zone_for_ip)
        self.stop = asyncio.Event()
        self.started = time.time()

    async def _report_flows(self) -> None:
        while not self.stop.is_set():
            for c in self.conduits:
                for rec in c.flow_records():
                    self.forwarder.flow(rec)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        self.forwarder.start()
        for c in self.conduits:
            await c.start()
        self.bus.publish(Event(f"Sensor {self.name} started with {len(self.conduits)} conduit(s)", "info", "SYSTEM",
                               sensor=self.name, detail={"conduits": [c.id for c in self.conduits]}))
        tasks = [asyncio.create_task(self._report_flows()), asyncio.create_task(self.discovery.run(self.stop))]
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for c in self.conduits:
                await c.stop()
            self.forwarder.stop()
