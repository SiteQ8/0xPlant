"""Plant configuration loader (YAML)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml


@dataclass
class RemoteConfig:
    name: str
    host: str
    port: int = 502
    unit: int = 1


@dataclass
class PLCConfig:
    name: str
    program: str
    listen: str = "0.0.0.0"
    port: int = 502
    unit: int = 1
    identity: Dict[str, str] = field(default_factory=dict)
    remotes: List[RemoteConfig] = field(default_factory=list)
    source_ip: Optional[str] = None      # bind address for outgoing PLC-to-PLC polls
    seed: Optional[int] = None


@dataclass
class HMIPLC:
    name: str
    program: str
    host: str
    port: int = 502
    unit: int = 1


@dataclass
class HMIConfig:
    listen: str = "0.0.0.0"
    port: int = 8080
    source_ip: Optional[str] = None
    poll_ms: int = 500
    plcs: List[HMIPLC] = field(default_factory=list)


@dataclass
class PlantConfig:
    time_scale: float = 60.0
    scan_ms: int = 250
    plcs: List[PLCConfig] = field(default_factory=list)
    hmi: HMIConfig = field(default_factory=HMIConfig)
    ews_source_ip: Optional[str] = None

    def plc(self, name: str) -> PLCConfig:
        for p in self.plcs:
            if p.name == name:
                return p
        raise KeyError(f"no PLC named {name} in configuration")


def load(path: str) -> PlantConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = PlantConfig(time_scale=float(raw.get("time_scale", 60)), scan_ms=int(raw.get("scan_ms", 250)),
                      ews_source_ip=raw.get("ews_source_ip"))
    for p in raw.get("plcs", []):
        cfg.plcs.append(PLCConfig(
            name=p["name"], program=p["program"], listen=p.get("listen", "0.0.0.0"),
            port=int(p.get("port", 502)), unit=int(p.get("unit", 1)),
            identity=dict(p.get("identity", {})), source_ip=p.get("source_ip"), seed=p.get("seed"),
            remotes=[RemoteConfig(r["name"], r["host"], int(r.get("port", 502)), int(r.get("unit", 1)))
                     for r in p.get("remotes", [])],
        ))
    h = raw.get("hmi", {})
    cfg.hmi = HMIConfig(listen=h.get("listen", "0.0.0.0"), port=int(h.get("port", 8080)),
                        source_ip=h.get("source_ip"), poll_ms=int(h.get("poll_ms", 500)),
                        plcs=[HMIPLC(x["name"], x["program"], x["host"], int(x.get("port", 502)), int(x.get("unit", 1)))
                              for x in h.get("plcs", [])])
    return cfg
