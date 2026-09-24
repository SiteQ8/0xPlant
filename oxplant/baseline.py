"""Behavioural baselining for conduits and a labelled traffic recorder.

Baseline: during a learning window every permitted request pattern
(function, address, quantity) and the per-second request rate of each source
are recorded. Afterwards a pattern never seen from that source, or a rate far
above what was learned, is reported as OXP-017. Nothing is blocked: this is
detection on top of the allowlist, meant for research on anomaly detection.

Recorder: appends one JSON line per request with the policy decision, so a
lab run produces a labelled dataset (see docs/research/dataset.md).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, Optional, Set, Tuple

Pattern = Tuple[int, int, int]   # function, address, quantity


class SourceProfile:
    def __init__(self) -> None:
        self.patterns: Set[Pattern] = set()
        self.reported: Set[Pattern] = set()
        self.max_rps = 0
        self._sec = 0
        self._count = 0
        self.requests = 0
        self.last_rate_report = 0.0

    def tick(self, now: float) -> int:
        sec = int(now)
        if sec != self._sec:
            self._sec, self._count = sec, 0
        self._count += 1
        self.requests += 1
        return self._count


class Baseline:
    def __init__(self, learning_s: float, rate_factor: float = 2.0, min_rate: int = 5):
        self.learning_s = learning_s
        self.rate_factor = rate_factor
        self.min_rate = min_rate
        self.started = time.time()
        self.profiles: Dict[str, SourceProfile] = {}

    @property
    def enabled(self) -> bool:
        return self.learning_s > 0

    @property
    def learning(self) -> bool:
        return time.time() - self.started < self.learning_s

    @property
    def state(self) -> str:
        return "off" if not self.enabled else ("learning" if self.learning else "enforcing")

    def observe(self, source: str, function: int, address: int, quantity: int, now: Optional[float] = None) -> Optional[dict]:
        """Record a permitted request. Returns an anomaly description after learning, else None."""
        if not self.enabled:
            return None
        now = now or time.time()
        p = self.profiles.setdefault(source, SourceProfile())
        rate = p.tick(now)
        pattern = (function, address, quantity)
        if self.learning:
            p.patterns.add(pattern)
            p.max_rps = max(p.max_rps, rate)
            return None
        if pattern not in p.patterns and pattern not in p.reported:
            p.reported.add(pattern)
            return {"kind": "new_pattern", "function": function, "address": address, "quantity": quantity,
                    "learned_patterns": len(p.patterns)}
        limit = max(self.min_rate, int(p.max_rps * self.rate_factor))
        if rate > limit and now - p.last_rate_report > 30:
            p.last_rate_report = now
            return {"kind": "rate", "rate": rate, "learned_max": p.max_rps, "limit": limit}
        return None

    def describe(self) -> dict:
        return {"state": self.state, "learning_s": self.learning_s,
                "sources": {s: {"patterns": len(p.patterns), "max_rps": p.max_rps, "requests": p.requests} for s, p in self.profiles.items()}}


class Recorder:
    """Thread-safe JSONL appender with periodic flushing."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - long-lived handle
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.records = 0

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            self._fh.write(line + "\n")
            self.records += 1
            if time.time() - self._last_flush > 1.0:
                self._fh.flush()
                self._last_flush = time.time()

    def close(self) -> None:
        with self._lock:
            self._fh.flush()
            self._fh.close()
