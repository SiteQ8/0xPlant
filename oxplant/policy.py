"""Conduit policy: who may do what to a protected asset."""
from __future__ import annotations

import ipaddress
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from modbuslite import codec
from modbuslite.codec import Request

Range = Tuple[int, int]


def parse_ranges(spec) -> List[Range]:
    """'100-119' | 100 | ['100-119', '200'] -> [(100, 119), (200, 200)]"""
    if spec is None:
        return []
    if not isinstance(spec, (list, tuple)):
        spec = [spec]
    out: List[Range] = []
    for item in spec:
        s = str(item).strip()
        if "-" in s:
            lo, hi = s.split("-", 1)
            out.append((int(lo), int(hi)))
        else:
            out.append((int(s), int(s)))
    for lo, hi in out:
        if lo < 0 or hi < lo or hi > 65535:
            raise ValueError(f"invalid range {lo}-{hi}")
    return out


def in_ranges(lo: int, hi: int, ranges: List[Range]) -> bool:
    return any(rlo <= lo and hi <= rhi for rlo, rhi in ranges)


@dataclass
class SourceRule:
    source: str                          # IP or CIDR
    asset: str = ""                      # friendly name of the source asset
    allow: List[str] = field(default_factory=list)   # read | write | identification
    functions: List[int] = field(default_factory=list)  # extra explicit function codes
    writes: Dict[str, List[Range]] = field(default_factory=dict)  # table -> permitted write ranges
    max_rps: Optional[int] = None

    def __post_init__(self):
        self._net = ipaddress.ip_network(self.source, strict=False)

    def matches(self, ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip) in self._net
        except ValueError:
            return False


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    rule: Optional[str] = None          # OXP rule id when denied
    exception_code: int = codec.EXC_ILLEGAL_FUNCTION
    source_asset: str = ""


class RateLimiter:
    def __init__(self, window: float = 1.0):
        self.window = window
        self._hits: Dict[str, Deque[float]] = {}

    def hit(self, key: str, limit: int) -> Tuple[bool, int]:
        """Record a request; returns (within limit, current rate)."""
        now = time.monotonic()
        dq = self._hits.setdefault(key, deque())
        dq.append(now)
        while dq and now - dq[0] > self.window:
            dq.popleft()
        return len(dq) <= limit, len(dq)


class ConduitPolicy:
    def __init__(self, conduit_id: str, asset: str, rules: List[SourceRule], default: str = "deny",
                 max_rps: int = 100):
        self.conduit_id = conduit_id
        self.asset = asset
        self.rules = rules
        self.default = default
        self.max_rps = max_rps
        self.limiter = RateLimiter()

    def find(self, ip: str) -> Optional[SourceRule]:
        for r in self.rules:
            if r.matches(ip):
                return r
        return None

    def evaluate(self, ip: str, req: Request) -> Decision:
        rule = self.find(ip)
        if rule is None:
            if self.default == "allow":
                return Decision(True, "default allow", source_asset="unlisted")
            return Decision(False, f"source {ip} is not part of conduit {self.conduit_id}", "OXP-003")
        name = rule.asset or rule.source
        ok, rate = self.limiter.hit(ip, rule.max_rps or self.max_rps)
        if not ok:
            return Decision(False, f"{name} exceeded {rule.max_rps or self.max_rps} req/s ({rate})", "OXP-004",
                            codec.EXC_DEVICE_BUSY, name)
        fc = req.function
        undecoded_write = fc in (codec.FC_MASK_WRITE, codec.FC_READ_WRITE_REGISTERS)
        if fc in rule.functions and not req.is_write and not undecoded_write:
            return Decision(True, "explicit function", source_asset=name)
        if undecoded_write:
            # the codec does not decode addresses for these, so only a full write grant may use them
            if "write" in rule.allow and fc in rule.functions:
                return Decision(True, "explicit write function", source_asset=name)
            return Decision(False, f"{name} may not use {req.name}", "OXP-001", source_asset=name)
        if req.is_read:
            if "read" in rule.allow:
                return Decision(True, "read", source_asset=name)
            return Decision(False, f"{name} may not use {req.name}", "OXP-001", source_asset=name)
        if req.is_write:
            if "write" in rule.allow:
                return Decision(True, "write", source_asset=name)
            ranges = rule.writes.get(req.table or "", [])
            if not ranges:
                return Decision(False, f"{name} may not use {req.name}", "OXP-001", source_asset=name)
            if in_ranges(req.address, req.end_address, ranges):
                return Decision(True, "write within permitted range", source_asset=name)
            return Decision(False,
                            f"{name} wrote {req.table}[{req.address}..{req.end_address}] outside permitted ranges "
                            + ", ".join(f"{lo}-{hi}" for lo, hi in ranges),
                            "OXP-002", codec.EXC_ILLEGAL_ADDRESS, name)
        if fc == codec.FC_ENCAP and req.mei_type == codec.MEI_DEVICE_ID:
            if "identification" in rule.allow:
                return Decision(True, "identification", source_asset=name)
            return Decision(False, f"{name} may not read device identification", "OXP-001", source_asset=name)
        return Decision(False, f"{name} used unsupported {req.name}", "OXP-001", source_asset=name)

    def describe(self) -> dict:
        return {
            "id": self.conduit_id, "asset": self.asset, "default": self.default, "max_rps": self.max_rps,
            "rules": [{
                "source": r.source, "asset": r.asset, "allow": r.allow, "functions": r.functions,
                "writes": {t: [f"{lo}-{hi}" for lo, hi in rs] for t, rs in r.writes.items()},
                "max_rps": r.max_rps,
            } for r in self.rules],
        }
