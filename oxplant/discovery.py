"""Asset discovery: ICS service probing, Modbus device identification, rogue and insecure service detection."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from modbuslite import ModbusClient

from .config import AssetConfig, DiscoveryScope
from .events import Event

log = logging.getLogger("oxplant.discovery")

PORT_PROTOCOLS = {
    502: "Modbus/TCP", 5020: "Modbus/TCP", 102: "S7comm", 4840: "OPC UA", 44818: "EtherNet/IP",
    20000: "DNP3", 1883: "MQTT", 8883: "MQTT/TLS", 47808: "BACnet/IP", 2404: "IEC 60870-5-104",
    1502: "TriStation", 23: "Telnet", 21: "FTP", 80: "HTTP", 443: "HTTPS", 445: "SMB", 161: "SNMP",
    22: "SSH", 3389: "RDP", 5900: "VNC", 8000: "HTTP", 8080: "HTTP",
}
INSECURE_PORTS = {23: "Telnet (cleartext)", 21: "FTP (cleartext)", 80: "HTTP (cleartext)", 8080: "HTTP (cleartext)", 1883: "MQTT without TLS",
                  445: "SMB", 69: "TFTP", 5900: "VNC", 3389: "RDP exposed on OT network"}
MODBUS_PORTS = {502, 5020}


def expand_targets(targets: Iterable[str]) -> List[str]:
    """Accepts single IPs, CIDR blocks and 'a.b.c.d-a.b.c.e' ranges."""
    out: List[str] = []
    for t in targets:
        t = str(t).strip()
        if "/" in t:
            net = ipaddress.ip_network(t, strict=False)
            out.extend(str(h) for h in (net.hosts() if net.num_addresses > 2 else net))
        elif "-" in t:
            a, b = t.split("-", 1)
            start, end = int(ipaddress.ip_address(a.strip())), int(ipaddress.ip_address(b.strip()))
            if end < start or end - start > 65536:
                raise ValueError(f"bad range {t}")
            out.extend(str(ipaddress.ip_address(i)) for i in range(start, end + 1))
        else:
            out.append(str(ipaddress.ip_address(t)))
    return out


async def probe(ip: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        writer.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def scan(targets: List[str], ports: List[int], concurrency: int = 128, timeout: float = 0.5) -> Dict[str, List[int]]:
    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, List[int]] = {}

    async def one(ip: str, port: int) -> None:
        async with sem:
            if await probe(ip, port, timeout):
                results.setdefault(ip, []).append(port)

    await asyncio.gather(*(one(ip, p) for ip in targets for p in ports))
    return {ip: sorted(ps) for ip, ps in results.items()}


async def identify_modbus(ip: str, port: int, source_ip: Optional[str] = None) -> Optional[dict]:
    kwargs = {"local_addr": (source_ip, 0)} if source_ip else {}
    client = ModbusClient(ip, port, timeout=1.5, **kwargs)
    try:
        await client.connect()
        return await client.read_device_identification(0x02)
    except Exception:  # noqa: BLE001 - not every Modbus device supports FC43
        try:
            return await client.read_device_identification(0x01)
        except Exception:  # noqa: BLE001
            return None
    finally:
        await client.close()


class Discovery:
    """Runs discovery scopes on a schedule and reports assets and findings."""

    def __init__(self, scopes: List[DiscoveryScope], baseline: List[AssetConfig], on_event: Callable[[Event], None],
                 on_asset: Callable[[dict], None], runner: str, source_ip: Optional[str] = None,
                 zone_for_ip: Optional[Callable[[str], str]] = None):
        self.scopes = [s for s in scopes if s.runner == runner]
        self.baseline = {a.ip: a for a in baseline}
        self.on_event = on_event
        self.on_asset = on_asset
        self.runner = runner
        self.source_ip = source_ip
        self.zone_for_ip = zone_for_ip or (lambda ip: "")
        self._misses: Dict[str, int] = {}
        self._reported: Set[Tuple[str, int]] = set()
        self._rogues: Set[str] = set()
        self.last_run: Dict[str, float] = {}
        self.cycles = 0

    async def run_scope(self, scope: DiscoveryScope) -> Dict[str, List[int]]:
        targets = expand_targets(scope.targets)
        t0 = time.time()
        found = await scan(targets, scope.ports)
        self.last_run[scope.zone or ",".join(scope.targets)] = time.time()
        log.info("%s scanned %d hosts x %d ports in %.1fs: %d hosts with open services",
                 self.runner, len(targets), len(scope.ports), time.time() - t0, len(found))
        seen_ips = set(found)
        for ip, ports in found.items():
            asset = self.baseline.get(ip)
            protocols = sorted({PORT_PROTOCOLS.get(p, f"tcp/{p}") for p in ports})
            record = {"ip": ip, "ports": ports, "protocols": protocols, "status": "online", "last_seen": time.time(),
                      "discovered_by": self.runner, "zone": (asset.zone if asset else "") or scope.zone or self.zone_for_ip(ip)}
            identity = None
            mb_port = next((p for p in ports if p in MODBUS_PORTS), None)
            if mb_port and not (asset and asset.type.lower() == "conduit"):   # a conduit answers with the PLC's identity
                identity = await identify_modbus(ip, mb_port, self.source_ip)
                if identity:
                    record.update({"vendor": identity.get("VendorName", ""), "model": identity.get("ModelName") or identity.get("ProductCode", ""),
                                   "firmware": identity.get("MajorMinorRevision", ""), "application": identity.get("UserApplicationName", ""),
                                   "detail": {"identity": identity}})
            if asset:
                record.update({"id": asset.id, "name": asset.name, "type": asset.type, "criticality": asset.criticality, "approved": True})
                if self._misses.get(asset.id, 0) >= 2:
                    self.on_event(Event(f"{asset.name} ({ip}) is answering again", "info", "AVAILABILITY", dest_ip=ip,
                                        asset=asset.id, sensor=self.runner, resolve_key=f"OXP-007:{asset.id}"))
                self._misses[asset.id] = 0
            else:
                record.update({"id": f"UNK-{ip}", "name": f"Unknown device {ip}", "type": "Unknown", "criticality": "high"})
                if ip not in self._rogues:
                    record["approved"] = False   # only on first sight: an operator approval must survive later scans
                    self._rogues.add(ip)
                    self.on_event(Event.from_rule("OXP-009", f"Rogue device {ip} exposes {', '.join(protocols)} in zone {record['zone'] or '?'}",
                                                  source_ip=ip, asset=record["id"], sensor=self.runner,
                                                  detail={"ports": ports, "protocols": protocols, "identity": identity or {}},
                                                  alert_key=f"OXP-009:{ip}"))
            self.on_asset(record)
            for p in ports:
                if p in INSECURE_PORTS and (ip, p) not in self._reported:
                    self._reported.add((ip, p))
                    self.on_event(Event.from_rule("OXP-010", f"{INSECURE_PORTS[p]} exposed on {record['name']} ({ip}:{p})",
                                                  source_ip=ip, asset=record["id"], protocol=PORT_PROTOCOLS.get(p, str(p)), sensor=self.runner,
                                                  detail={"port": p, "service": INSECURE_PORTS[p]}, alert_key=f"OXP-010:{ip}:{p}"))
        # baseline assets in this scope that did not answer
        for ip in targets:
            asset = self.baseline.get(ip)
            if not asset or ip in seen_ips or not asset.ports:
                continue
            if not any(p in scope.ports for p in asset.ports):
                continue
            self._misses[asset.id] = self._misses.get(asset.id, 0) + 1
            if self._misses[asset.id] == 2:
                self.on_event(Event.from_rule("OXP-007", f"{asset.name} ({ip}) is not answering on {asset.ports}",
                                              dest_ip=ip, asset=asset.id, sensor=self.runner,
                                              detail={"ports": asset.ports}, alert_key=f"OXP-007:{asset.id}"))
                self.on_asset({"id": asset.id, "name": asset.name, "type": asset.type, "ip": ip, "zone": asset.zone,
                               "status": "offline", "discovered_by": self.runner})
            elif self._misses[asset.id] > 2:
                self.on_asset({"id": asset.id, "ip": ip, "status": "offline"})
        return found

    async def run(self, stop: asyncio.Event, initial_delay: float = 8.0) -> None:
        if not self.scopes:
            return
        # give neighbouring services a moment to start so the first cycle is representative
        next_run = {id(s): time.time() + initial_delay for s in self.scopes}
        while not stop.is_set():
            now = time.time()
            for s in self.scopes:
                if now >= next_run[id(s)]:
                    try:
                        await self.run_scope(s)
                    except Exception:  # noqa: BLE001
                        log.exception("discovery scope failed")
                    next_run[id(s)] = time.time() + s.interval_s
                    self.cycles += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
