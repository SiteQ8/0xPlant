"""Process integrity monitor: safe envelopes, configuration drift, interlocks, identity, availability.

The monitor is strictly read-only. It polls each protected PLC through its
conduit (as the console's own source address) and never writes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional

from modbuslite import ModbusClient

from .config import IntegrityConfig, IntegrityTarget, TagConfig
from .events import Event
from .invariants import Invariant

log = logging.getLogger("oxplant.integrity")

ENVELOPE_DEBOUNCE = 3       # consecutive polls outside the envelope before alerting
OFFLINE_DEBOUNCE = 3


class TargetMonitor:
    def __init__(self, target: IntegrityTarget, cfg: IntegrityConfig, monitor: "IntegrityMonitor"):
        self.t = target
        self.cfg = cfg
        self.m = monitor
        kwargs = {"local_addr": (cfg.source_ip, 0)} if cfg.source_ip else {}
        self.client = ModbusClient(target.host, target.port, unit=target.unit, timeout=1.5, **kwargs)
        self.values: Dict[str, float] = {}
        self.status: Dict[str, str] = {}
        self.out_count: Dict[str, int] = {}
        self.in_count: Dict[str, int] = {}
        self.alerting: Dict[str, bool] = {}
        self.drift: Dict[str, bool] = {}
        self.interlock: Dict[str, bool] = {}
        self.failures = 0
        self.online = False
        self.last_identity_check = 0.0
        self.identity: Optional[dict] = None
        self.plan = self._plan()
        self.invariants = [Invariant(i.name, i.expr, i.debounce, i.desc) for i in target.invariants]
        self.previous: Dict[str, float] = {}
        self.prev_ts = 0.0
        self.inv_status: Dict[str, str] = {i.name: "pending" for i in self.invariants}

    def _plan(self):
        plan = []
        for table in ("input", "holding", "coils", "discrete"):
            addrs = sorted({t.address for t in self.t.tags if t.table == table})
            if not addrs:
                continue
            start = prev = addrs[0]
            for a in addrs[1:] + [None]:
                if a is None or a - prev > 8:
                    plan.append((table, start, prev - start + 1))
                    if a is not None:
                        start = a
                if a is not None:
                    prev = a
        return plan

    def golden(self, tag: TagConfig) -> Optional[float]:
        override = self.m.get_golden(self.t.asset, tag.name)
        return override if override is not None else tag.golden

    async def poll(self) -> None:
        raw: Dict[str, Dict[int, int]] = {"input": {}, "holding": {}, "coils": {}, "discrete": {}}
        for table, start, count in self.plan:
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
        now = time.time()
        previous, prev_ts = dict(self.values), self.prev_ts
        for tag in self.t.tags:
            if tag.address not in raw[tag.table]:
                continue
            r = raw[tag.table][tag.address]
            value = r / tag.scale if tag.table in ("input", "holding") else float(r)
            prev = self.values.get(tag.name)
            self.values[tag.name] = value
            if tag.role == "process":
                self._envelope(tag, value)
            elif tag.role == "config":
                self._config(tag, value)
            elif tag.role == "setpoint":
                self._setpoint(tag, prev, value)
            elif tag.role == "interlock":
                self._interlock(tag, value)
            else:
                self.status[tag.name] = "ok"
        self._invariants(previous, now - prev_ts if prev_ts else 0.0)
        self.previous, self.prev_ts = previous, self.prev_ts
        self.prev_ts = now
        if not self.online:
            self.online = True
            if self.failures >= OFFLINE_DEBOUNCE:
                self.m.emit(Event(f"{self.t.asset} answering again", "info", "AVAILABILITY", asset=self.t.asset,
                                  resolve_key=f"OXP-007:{self.t.asset}"))
            self.m.on_asset_status(self.t.asset, "online")
        self.failures = 0
        if now - self.last_identity_check >= self.cfg.identity_s:
            self.last_identity_check = now
            await self._identity()
        self.m.live[self.t.asset] = {
            "online": True, "ts": now, "host": self.t.host, "identity": self.identity,
            "tags": {tag.name: {"value": self.values.get(tag.name), "unit": tag.unit, "role": tag.role, "status": self.status.get(tag.name, "ok"),
                                "min": tag.min, "max": tag.max, "golden": self.golden(tag), "desc": tag.desc} for tag in self.t.tags},
            "invariants": {i.name: {"status": self.inv_status.get(i.name, "pending"), "expr": i.expr, "desc": i.desc} for i in self.invariants},
        }

    def _invariants(self, previous: Dict[str, float], dt: float) -> None:
        for inv in self.invariants:
            holds = inv.evaluate(self.values, previous, dt)
            change = inv.update(holds)
            self.inv_status[inv.name] = "pending" if holds is None else ("violated" if inv.alerting else "ok")
            if change == "violated":
                self.m.emit(Event.from_rule("OXP-018", f"{self.t.asset} invariant '{inv.name}' violated: reported values are physically inconsistent",
                                            asset=self.t.asset, dest_ip=self.t.host,
                                            detail={"invariant": inv.name, "expr": inv.expr, "desc": inv.desc,
                                                    "values": {k: self.values[k] for k in self.values if k in inv.expr}},
                                            alert_key=f"OXP-018:{self.t.asset}:{inv.name}"))
            elif change == "restored":
                self.m.emit(Event(f"{self.t.asset} invariant '{inv.name}' holds again", "info", "PROCESS", asset=self.t.asset,
                                  detail={"invariant": inv.name}, resolve_key=f"OXP-018:{self.t.asset}:{inv.name}"))

    def _envelope(self, tag: TagConfig, value: float) -> None:
        low = tag.min is not None and value < tag.min
        high = tag.max is not None and value > tag.max
        if low or high:
            self.out_count[tag.name] = self.out_count.get(tag.name, 0) + 1
            self.in_count[tag.name] = 0
            self.status[tag.name] = "low" if low else "high"
            if self.out_count[tag.name] == ENVELOPE_DEBOUNCE and not self.alerting.get(tag.name):
                self.alerting[tag.name] = True
                bound = tag.min if low else tag.max
                self.m.emit(Event.from_rule("OXP-005", f"{self.t.asset} {tag.name} = {value:.2f} {tag.unit} is {'below' if low else 'above'} safe limit {bound} {tag.unit}",
                                            asset=self.t.asset, dest_ip=self.t.host,
                                            detail={"tag": tag.name, "value": value, "min": tag.min, "max": tag.max, "desc": tag.desc},
                                            alert_key=f"OXP-005:{self.t.asset}:{tag.name}"))
        else:
            self.in_count[tag.name] = self.in_count.get(tag.name, 0) + 1
            self.out_count[tag.name] = 0
            self.status[tag.name] = "ok"
            if self.alerting.get(tag.name) and self.in_count[tag.name] >= ENVELOPE_DEBOUNCE:
                self.alerting[tag.name] = False
                self.m.emit(Event(f"{self.t.asset} {tag.name} back inside safe envelope ({value:.2f} {tag.unit})", "info", "PROCESS",
                                  asset=self.t.asset, detail={"tag": tag.name, "value": value},
                                  resolve_key=f"OXP-005:{self.t.asset}:{tag.name}"))

    def _config(self, tag: TagConfig, value: float) -> None:
        golden = self.golden(tag)
        if golden is None:
            self.status[tag.name] = "ok"
            return
        drifted = abs(value - golden) > 1e-9
        self.status[tag.name] = "drift" if drifted else "ok"
        if drifted and not self.drift.get(tag.name):
            self.drift[tag.name] = True
            self.m.emit(Event.from_rule("OXP-006", f"{self.t.asset} {tag.name} changed from golden {golden} to {value} {tag.unit}",
                                        asset=self.t.asset, dest_ip=self.t.host,
                                        detail={"tag": tag.name, "golden": golden, "value": value, "unit": tag.unit},
                                        alert_key=f"OXP-006:{self.t.asset}:{tag.name}"))
            self.m.on_change(self.t.asset, tag.name, "config", golden, value, "unreviewed",
                             note=f"Engineering register differs from golden baseline ({tag.desc or tag.name})")
        elif not drifted and self.drift.get(tag.name):
            self.drift[tag.name] = False
            self.m.emit(Event(f"{self.t.asset} {tag.name} matches golden baseline again ({value} {tag.unit})", "info", "CONFIG",
                              asset=self.t.asset, detail={"tag": tag.name, "value": value},
                              resolve_key=f"OXP-006:{self.t.asset}:{tag.name}"))

    def _setpoint(self, tag: TagConfig, prev: Optional[float], value: float) -> None:
        self.status[tag.name] = "ok"
        if prev is not None and abs(prev - value) > 1e-9:
            self.m.emit(Event.from_rule("OXP-013", f"{self.t.asset} {tag.name} setpoint changed {prev} -> {value} {tag.unit}",
                                        asset=self.t.asset, dest_ip=self.t.host,
                                        detail={"tag": tag.name, "old": prev, "new": value, "unit": tag.unit}))
            self.m.on_change(self.t.asset, tag.name, "setpoint", prev, value, "logged", note="Operator setpoint region")

    def _interlock(self, tag: TagConfig, value: float) -> None:
        tripped = value != 0
        self.status[tag.name] = "trip" if tripped else "ok"
        if tripped and not self.interlock.get(tag.name):
            self.interlock[tag.name] = True
            self.m.emit(Event.from_rule("OXP-012", f"{self.t.asset} safety interlock tripped ({tag.name} = {int(value)})",
                                        asset=self.t.asset, dest_ip=self.t.host, detail={"tag": tag.name, "value": value},
                                        alert_key=f"OXP-012:{self.t.asset}:{tag.name}"))
        elif not tripped and self.interlock.get(tag.name):
            self.interlock[tag.name] = False
            self.m.emit(Event(f"{self.t.asset} interlock reset ({tag.name})", "info", "PROCESS", asset=self.t.asset,
                              detail={"tag": tag.name}, resolve_key=f"OXP-012:{self.t.asset}:{tag.name}"))

    async def _identity(self) -> None:
        try:
            ident = await self.client.read_device_identification(0x02)
        except Exception:  # noqa: BLE001 - identification may be blocked or unsupported
            return
        self.identity = ident
        known = self.m.get_identity(self.t.asset)
        if known is None:
            self.m.set_identity(self.t.asset, ident)
        elif known != ident:
            diff = {k: (known.get(k), ident.get(k)) for k in set(known) | set(ident) if known.get(k) != ident.get(k)}
            self.m.emit(Event.from_rule("OXP-008", f"{self.t.asset} device identity changed: " + ", ".join(f"{k} {a!r}->{b!r}" for k, (a, b) in diff.items()),
                                        asset=self.t.asset, dest_ip=self.t.host, detail={"diff": diff, "identity": ident},
                                        alert_key=f"OXP-008:{self.t.asset}"))
            self.m.set_identity(self.t.asset, ident)
        self.m.on_asset_identity(self.t.asset, ident)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll()
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                try:
                    await self.client.close()
                    if self.failures == OFFLINE_DEBOUNCE:
                        self.online = False
                        self.m.live[self.t.asset] = dict(self.m.live.get(self.t.asset, {}), online=False, error=str(exc), ts=time.time())
                        self.m.emit(Event.from_rule("OXP-007", f"{self.t.asset} stopped answering integrity polls via {self.t.host}: {exc}",
                                                    asset=self.t.asset, dest_ip=self.t.host, detail={"error": str(exc)},
                                                    alert_key=f"OXP-007:{self.t.asset}"))
                        self.m.on_asset_status(self.t.asset, "offline")
                except Exception:  # noqa: BLE001 - reporting must never stop the monitor
                    log.exception("integrity monitor could not report a failure for %s", self.t.asset)
                await asyncio.sleep(min(5.0, 0.5 * self.failures))
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.poll_s)
            except asyncio.TimeoutError:
                pass
        await self.client.close()


class IntegrityMonitor:
    def __init__(self, cfg: IntegrityConfig, emit: Callable[[Event], None],
                 on_change: Callable[..., None], get_golden: Callable[[str, str], Optional[float]],
                 get_identity: Callable[[str], Optional[dict]], set_identity: Callable[[str, dict], None],
                 on_asset_status: Callable[[str, str], None], on_asset_identity: Callable[[str, dict], None]):
        self.cfg = cfg
        self.emit = emit
        self.on_change = on_change
        self.get_golden = get_golden
        self.get_identity = get_identity
        self.set_identity = set_identity
        self.on_asset_status = on_asset_status
        self.on_asset_identity = on_asset_identity
        self.live: Dict[str, dict] = {}
        self.targets: List[TargetMonitor] = [TargetMonitor(t, cfg, self) for t in cfg.targets]

    async def run(self, stop: asyncio.Event) -> None:
        if not self.targets:
            return
        await asyncio.gather(*(t.run(stop) for t in self.targets))
