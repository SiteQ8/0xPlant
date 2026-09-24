"""Protective conduit: a Modbus/TCP gateway that enforces the conduit policy.

Every request from a Level 2/3 client is decoded, checked against the
policy for its source address and only then forwarded to the protected PLC.
Denied requests are answered with a Modbus exception so the client fails
safely, and every decision is logged as an event.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple

from modbuslite import codec
from modbuslite.codec import DecodeError, Request

from .events import Event, EventBus
from .policy import ConduitPolicy

log = logging.getLogger("oxplant.conduit")


@dataclass
class FlowStats:
    source_ip: str
    source_asset: str
    asset: str
    conduit: str
    first_seen: float
    last_seen: float
    requests: int = 0
    denied: int = 0
    functions: Dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source_ip": self.source_ip, "source_asset": self.source_asset, "asset": self.asset,
            "conduit": self.conduit, "first_seen": self.first_seen, "last_seen": self.last_seen,
            "requests": self.requests, "denied": self.denied,
            "functions": {codec.FUNCTION_NAMES.get(fc, f"0x{fc:02X}"): n for fc, n in self.functions.items()},
            "protocol": "Modbus/TCP",
        }


class Conduit:
    def __init__(self, policy: ConduitPolicy, listen: Tuple[str, int], upstream: Tuple[str, int],
                 bus: EventBus, sensor: str = "", upstream_timeout: float = 2.0):
        self.policy = policy
        self.listen = listen
        self.upstream = upstream
        self.bus = bus
        self.sensor = sensor
        self.upstream_timeout = upstream_timeout
        self._server: Optional[asyncio.AbstractServer] = None
        self.flows: Dict[str, FlowStats] = {}
        self._seen_sources: Set[str] = set()
        self._last_rate_event: Dict[str, float] = {}
        self._last_deny_event: Dict[Tuple[str, str], float] = {}
        self._upstream_down = False
        self.requests = 0
        self.denied = 0

    @property
    def id(self) -> str:
        return self.policy.conduit_id

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.listen[0], self.listen[1])
        log.info("%s protecting %s: %s:%d -> %s:%d", self.id, self.policy.asset,
                 self.listen[0], self.listen[1], self.upstream[0], self.upstream[1])

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # --- event helpers ---
    def _emit(self, ev: Event) -> None:
        ev.sensor = ev.sensor or self.sensor
        ev.asset = ev.asset or self.policy.asset
        ev.dest_ip = ev.dest_ip or self.upstream[0]
        ev.protocol = ev.protocol or "Modbus/TCP"
        ev.detail.setdefault("conduit", self.id)
        self.bus.publish(ev)

    def _flow(self, ip: str, source_asset: str) -> FlowStats:
        f = self.flows.get(ip)
        now = time.time()
        if f is None:
            f = FlowStats(ip, source_asset, self.policy.asset, self.id, now, now)
            self.flows[ip] = f
        f.last_seen = now
        if source_asset and f.source_asset in ("", "unlisted"):
            f.source_asset = source_asset
        return f

    def _deny(self, ip: str, req: Request, decision) -> None:
        self.denied += 1
        key = (ip, decision.rule or "")
        now = time.time()
        # OXP-004 is noisy by nature: one event per source per 30 s
        if decision.rule == "OXP-004" and now - self._last_rate_event.get(ip, 0) < 30:
            return
        if decision.rule == "OXP-004":
            self._last_rate_event[ip] = now
        elif now - self._last_deny_event.get(key, 0) < 2 and decision.rule == "OXP-003":
            return  # unknown source hammering: aggregate
        self._last_deny_event[key] = now
        title = f"{decision.rule and codec.FUNCTION_NAMES.get(req.function, req.name)} from {decision.source_asset or ip} blocked on {self.policy.asset}"
        self._emit(Event.from_rule(decision.rule or "OXP-001", title, source_ip=ip, detail={
            "function": req.function, "function_name": req.name, "address": req.address,
            "quantity": req.quantity, "table": req.table, "reason": decision.reason,
            "values": req.values[:16], "exception": decision.exception_code,
        }, alert_key=f"{decision.rule}:{self.policy.asset}:{ip}"))

    # --- connection handling ---
    async def _open_upstream(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.upstream[0], self.upstream[1]), self.upstream_timeout)
        if self._upstream_down:
            self._upstream_down = False
            self._emit(Event(f"{self.policy.asset} reachable again behind {self.id}", "info", "AVAILABILITY",
                             resolve_key=f"OXP-016:{self.policy.asset}"))
        return reader, writer

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = str(peer[0])
        up_reader = up_writer = None
        rule = self.policy.find(ip)
        source_asset = (rule.asset if rule else "") or ("unlisted" if rule is None else ip)
        if ip not in self._seen_sources:
            self._seen_sources.add(ip)
            if rule is not None:
                self._emit(Event.from_rule("OXP-015", f"{source_asset} started talking to {self.policy.asset}",
                                           source_ip=ip, detail={"source_asset": source_asset}))
        flow = self._flow(ip, source_asset)
        try:
            while True:
                try:
                    transaction, unit, pdu = await codec.read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                except DecodeError as exc:
                    self._emit(Event.from_rule("OXP-011", f"Malformed frame from {source_asset or ip}", source_ip=ip,
                                               detail={"error": str(exc)}, alert_key=f"OXP-011:{self.policy.asset}:{ip}"))
                    break
                self.requests += 1
                flow.requests += 1
                try:
                    req = codec.parse_pdu(pdu)
                except DecodeError as exc:
                    flow.denied += 1
                    self._emit(Event.from_rule("OXP-011", f"Undecodable Modbus PDU from {source_asset or ip}",
                                               source_ip=ip, detail={"error": str(exc), "pdu": pdu[:16].hex()},
                                               alert_key=f"OXP-011:{self.policy.asset}:{ip}"))
                    writer.write(codec.build_mbap(transaction, unit, codec.build_exception(pdu[0] if pdu else 0, codec.EXC_ILLEGAL_VALUE)))
                    await writer.drain()
                    continue
                flow.functions[req.function] = flow.functions.get(req.function, 0) + 1
                decision = self.policy.evaluate(ip, req)
                if not decision.allowed:
                    flow.denied += 1
                    self._deny(ip, req, decision)
                    writer.write(codec.build_mbap(transaction, unit, codec.build_exception(req.function, decision.exception_code)))
                    await writer.drain()
                    continue
                # forward to the protected asset
                response = None
                for attempt in range(2):
                    try:
                        if up_writer is None or up_writer.is_closing():
                            up_reader, up_writer = await self._open_upstream()
                        up_writer.write(codec.build_mbap(transaction, unit, pdu))
                        await up_writer.drain()
                        while True:
                            rtid, _runit, rpdu = await asyncio.wait_for(codec.read_frame(up_reader), self.upstream_timeout)
                            if rtid == transaction:
                                break
                        response = rpdu
                        break
                    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, DecodeError) as exc:
                        if up_writer:
                            up_writer.close()
                        up_reader = up_writer = None
                        if attempt == 1:
                            if not self._upstream_down:
                                self._upstream_down = True
                                self._emit(Event.from_rule("OXP-016", f"{self.policy.asset} unreachable behind {self.id}",
                                                           detail={"error": str(exc)}, alert_key=f"OXP-016:{self.policy.asset}"))
                if response is None:
                    response = codec.build_exception(req.function, codec.EXC_GATEWAY_TARGET)
                if req.is_write and not (response[0] & 0x80):
                    self._emit(Event(f"{decision.source_asset or ip} wrote {req.table}[{req.address}"
                                     f"{'..' + str(req.end_address) if req.quantity > 1 else ''}] on {self.policy.asset}",
                                     "info", "OPERATIONS", source_ip=ip,
                                     detail={"function": req.function, "function_name": req.name, "address": req.address,
                                             "quantity": req.quantity, "table": req.table, "values": req.values[:16]}))
                writer.write(codec.build_mbap(transaction, unit, response))
                try:
                    await writer.drain()
                except ConnectionError:
                    break
        finally:
            writer.close()
            if up_writer:
                up_writer.close()

    def flow_records(self) -> list:
        return [f.to_dict() for f in self.flows.values()]
