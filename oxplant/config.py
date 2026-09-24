"""0xPlant configuration model and loader."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from .policy import ConduitPolicy, SourceRule, parse_ranges


@dataclass
class UserConfig:
    username: str
    role: str
    password_hash: str


@dataclass
class OutputConfig:
    syslog_host: str = ""
    syslog_port: int = 514
    webhook_url: str = ""
    min_severity: str = "warning"


@dataclass
class ConsoleConfig:
    listen: str = "0.0.0.0"
    port: int = 8000
    url: str = "http://127.0.0.1:8000"
    sensor_token: str = ""
    metrics_token: str = ""           # empty = /metrics is open (bind the console to a trusted network)
    session_hours: float = 8.0
    tls_cert: str = ""
    tls_key: str = ""
    database: str = "data/oxplant.db"
    users: List[UserConfig] = field(default_factory=list)
    outputs: OutputConfig = field(default_factory=OutputConfig)


@dataclass
class Zone:
    id: str
    name: str
    subnets: List[str] = field(default_factory=list)
    color: str = "#64748B"
    level: int = 0

    def contains(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in ipaddress.ip_network(s, strict=False) for s in self.subnets)


@dataclass
class AssetConfig:
    id: str
    name: str
    type: str
    ip: str
    zone: str = ""
    protocols: List[str] = field(default_factory=list)
    ports: List[int] = field(default_factory=list)
    criticality: str = "medium"
    vendor: str = ""
    model: str = ""
    description: str = ""


@dataclass
class ConduitConfig:
    id: str
    asset: str
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    policy: ConduitPolicy


@dataclass
class DiscoveryScope:
    runner: str                 # "console" or a sensor name
    targets: List[str]
    ports: List[int]
    interval_s: int = 120
    zone: str = ""


@dataclass
class TagConfig:
    name: str
    table: str
    address: int
    scale: float = 1.0
    unit: str = ""
    role: str = "process"       # process | setpoint | config | interlock | status
    min: Optional[float] = None
    max: Optional[float] = None
    golden: Optional[float] = None
    desc: str = ""


@dataclass
class InvariantConfig:
    name: str
    expr: str
    debounce: int = 3
    desc: str = ""


@dataclass
class IntegrityTarget:
    asset: str
    host: str
    port: int
    unit: int
    tags: List[TagConfig]
    invariants: List[InvariantConfig] = field(default_factory=list)


@dataclass
class IntegrityConfig:
    source_ip: Optional[str] = None
    poll_s: float = 2.0
    identity_s: float = 60.0
    targets: List[IntegrityTarget] = field(default_factory=list)


@dataclass
class SensorConfig:
    name: str = "SENSOR-001"
    console_url: str = ""
    conduits: List[str] = field(default_factory=list)   # conduit ids this sensor runs (empty = all)
    learning_s: float = 0.0                              # behavioural baseline learning window (0 = off)
    record: str = ""                                     # JSONL traffic recording path (research datasets)
    mqtt: Dict[str, Any] = field(default_factory=dict)   # optional MQTT monitor: {host, port, learning_s, envelopes}


@dataclass
class Config:
    site: str = "0xPlant"
    console: ConsoleConfig = field(default_factory=ConsoleConfig)
    zones: List[Zone] = field(default_factory=list)
    assets: List[AssetConfig] = field(default_factory=list)
    conduits: List[ConduitConfig] = field(default_factory=list)
    discovery: List[DiscoveryScope] = field(default_factory=list)
    integrity: IntegrityConfig = field(default_factory=IntegrityConfig)
    sensors: List[SensorConfig] = field(default_factory=list)
    path: str = ""

    def asset(self, asset_id: str) -> Optional[AssetConfig]:
        for a in self.assets:
            if a.id == asset_id:
                return a
        return None

    def asset_by_ip(self, ip: str) -> Optional[AssetConfig]:
        for a in self.assets:
            if a.ip == ip:
                return a
        return None

    def zone_for_ip(self, ip: str) -> str:
        for z in self.zones:
            if z.contains(ip):
                return z.id
        return ""

    def sensor(self, name: str) -> SensorConfig:
        for s in self.sensors:
            if s.name == name:
                return s
        raise KeyError(f"no sensor named {name} in configuration")


def _env(value: Any) -> Any:
    """Expand ${ENV_VAR} references in string values."""
    if isinstance(value, str) and "${" in value:
        return os.path.expandvars(value)
    return value


def load(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = Config(site=raw.get("site", "0xPlant"), path=path)

    c = raw.get("console", {})
    out = c.get("outputs", {})
    cfg.console = ConsoleConfig(
        listen=c.get("listen", "0.0.0.0"), port=int(c.get("port", 8000)),
        url=_env(c.get("url", "http://127.0.0.1:8000")), sensor_token=_env(c.get("sensor_token", "")), metrics_token=_env(c.get("metrics_token", "")),
        session_hours=float(c.get("session_hours", 8)), tls_cert=c.get("tls", {}).get("cert", ""),
        tls_key=c.get("tls", {}).get("key", ""), database=c.get("database", "data/oxplant.db"),
        users=[UserConfig(u["username"], u.get("role", "viewer"), _env(u["password_hash"])) for u in c.get("users", [])],
        outputs=OutputConfig(syslog_host=out.get("syslog", {}).get("host", ""), syslog_port=int(out.get("syslog", {}).get("port", 514)),
                             webhook_url=_env(out.get("webhook", {}).get("url", "")), min_severity=out.get("min_severity", "warning")),
    )
    cfg.zones = [Zone(z["id"], z.get("name", z["id"]), list(z.get("subnets", [])), z.get("color", "#64748B"), int(z.get("level", 0)))
                 for z in raw.get("zones", [])]
    cfg.assets = [AssetConfig(a["id"], a.get("name", a["id"]), a.get("type", "Unknown"), a["ip"], a.get("zone", cfg.zone_for_ip(a["ip"])),
                              list(a.get("protocols", [])), [int(p) for p in a.get("ports", [])], a.get("criticality", "medium"),
                              a.get("vendor", ""), a.get("model", ""), a.get("description", "")) for a in raw.get("assets", [])]
    for cd in raw.get("conduits", []):
        rules = []
        for r in cd.get("rules", []):
            src = r.get("source") or (cfg.asset(r["asset"]).ip if r.get("asset") and cfg.asset(r["asset"]) else None)
            if not src:
                raise ValueError(f"conduit {cd['id']}: rule without a resolvable source: {r}")
            rules.append(SourceRule(
                source=str(src), asset=r.get("asset", ""), allow=list(r.get("allow", [])),
                functions=[int(f) for f in r.get("functions", [])],
                writes={t: parse_ranges(v) for t, v in (r.get("writes") or {}).items()},
                max_rps=r.get("max_rps"),
            ))
        policy = ConduitPolicy(cd["id"], cd["asset"], rules, cd.get("default", "deny"), int(cd.get("max_rps", 100)))
        cfg.conduits.append(ConduitConfig(cd["id"], cd["asset"], cd["listen"]["host"], int(cd["listen"].get("port", 502)),
                                          cd["upstream"]["host"], int(cd["upstream"].get("port", 502)), policy))
    for d in raw.get("discovery", []):
        cfg.discovery.append(DiscoveryScope(d.get("runner", "console"), [str(t) for t in d.get("targets", [])],
                                            [int(p) for p in d.get("ports", [502, 102, 4840, 44818, 20000, 1883, 47808, 2404])],
                                            int(d.get("interval_s", 120)), d.get("zone", "")))
    i = raw.get("integrity", {})
    cfg.integrity = IntegrityConfig(source_ip=i.get("source_ip"), poll_s=float(i.get("poll_s", 2)),
                                    identity_s=float(i.get("identity_s", 60)))
    for t in i.get("targets", []):
        tags = [TagConfig(x["name"], x["table"], int(x["address"]), float(x.get("scale", 1)), x.get("unit", ""),
                          x.get("role", "process"), x.get("min"), x.get("max"), x.get("golden"), x.get("desc", ""))
                for x in t.get("tags", [])]
        invariants = [InvariantConfig(x["name"], x["expr"], int(x.get("debounce", 3)), x.get("desc", "")) for x in t.get("invariants", [])]
        cfg.integrity.targets.append(IntegrityTarget(t["asset"], t["host"], int(t.get("port", 502)), int(t.get("unit", 1)), tags, invariants))
    cfg.sensors = [SensorConfig(s["name"], _env(s.get("console_url", cfg.console.url)), list(s.get("conduits", [])),
                                float(s.get("learning_s", 0) or 0), str(s.get("record", "") or ""), dict(s.get("mqtt") or {}))
                   for s in raw.get("sensors", [])]
    validate(cfg)
    return cfg


def validate(cfg: Config) -> List[str]:
    """Return a list of problems (and raise on fatal ones)."""
    problems: List[str] = []
    ids = [a.id for a in cfg.assets]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate asset ids in configuration")
    for cd in cfg.conduits:
        if cfg.asset(cd.asset) is None:
            problems.append(f"conduit {cd.id} protects unknown asset {cd.asset}")
        if cd.policy.default == "allow":
            problems.append(f"conduit {cd.id} has default allow (not recommended)")
        for r in cd.policy.rules:
            if "write" in r.allow and r.writes:
                problems.append(f"conduit {cd.id}: rule {r.source} allows all writes, 'writes' ranges are ignored")
            if any(fc in (5, 6, 15, 16, 22, 23) for fc in r.functions) and "write" not in r.allow:
                problems.append(f"conduit {cd.id}: rule {r.source} lists a write function code in 'functions'; write ranges still apply and 22/23 need allow: [write]")
    for t in cfg.integrity.targets:
        for tag in t.tags:
            if not tag.scale:
                raise ValueError(f"tag {tag.name} on {t.asset} has scale 0")
        from .invariants import Invariant
        for inv in t.invariants:
            Invariant(inv.name, inv.expr, inv.debounce)   # raises on unsafe or invalid expressions
    for t in cfg.integrity.targets:
        if cfg.asset(t.asset) is None:
            problems.append(f"integrity target references unknown asset {t.asset}")
        for tag in t.tags:
            if tag.table not in ("coils", "discrete", "holding", "input"):
                raise ValueError(f"tag {tag.name} has invalid table {tag.table}")
    if not cfg.console.sensor_token:
        problems.append("console.sensor_token is empty: sensors cannot authenticate")
    if not cfg.console.users:
        problems.append("console has no users configured")
    return problems
