"""Tag and register-map definitions shared by the PLC programs, the HMI and the EWS tool.

Register layout convention used by every 0xPlant virtual PLC (zero-based Modbus addresses):

  Input registers  (FC04)  0-63    process values (read-only)
  Holding registers (FC03/06/16)
                           0-9     status words (read-only, PLC rejects writes)
                           100-119 operator setpoints (HMI may write)
                           200-219 engineering configuration (EWS only)
  Coils (FC01/05/15)       0-7     operator commands
                           16-23   engineering commands
  Discrete inputs (FC02)   0-15    running / alarm / interlock status bits
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List

STATUS_HR = (0, 9)
OPERATOR_HR = (100, 119)
ENGINEER_HR = (200, 219)
OPERATOR_COILS = (0, 7)
ENGINEER_COILS = (16, 23)

# status words
HR_MODE = 0          # 0 = stop, 1 = run
HR_SCAN = 1          # scan counter (wraps)
HR_INTERLOCK = 2     # interlock word (bit per interlock)
HR_ALARM = 3         # alarm word (bit per alarm)
HR_UPTIME = 4        # uptime in minutes (simulated clock)
HR_SIMHOUR = 5       # simulated hour of day x100
HR_REMOTE_OK = 6     # bit per remote PLC link


@dataclass(frozen=True)
class Tag:
    name: str
    table: str          # coils | discrete | holding | input
    address: int
    scale: float = 1.0  # engineering value = register / scale
    unit: str = ""
    desc: str = ""
    access: str = "ro"  # ro | operator | supervisor | engineer

    def to_dict(self) -> dict:
        return asdict(self)


class TagMap:
    """Name-addressed access to a data store using a list of Tag definitions."""

    def __init__(self, tags: List[Tag]):
        self.tags = tags
        self.by_name: Dict[str, Tag] = {t.name: t for t in tags}

    def __iter__(self):
        return iter(self.tags)

    def get(self, store, name: str) -> float:
        t = self.by_name[name]
        raw = store.tables[t.table][t.address]
        return raw / t.scale if t.table in ("holding", "input") else float(raw)

    def set(self, store, name: str, value: float) -> None:
        t = self.by_name[name]
        if t.table in ("holding", "input"):
            raw = int(round(value * t.scale))
            store.tables[t.table][t.address] = max(0, min(65535, raw))
        else:
            store.tables[t.table][t.address] = 1 if value else 0

    def encode(self, name: str, value: float) -> int:
        t = self.by_name[name]
        if t.table in ("holding", "input"):
            return max(0, min(65535, int(round(value * t.scale))))
        return 1 if value else 0

    def decode(self, name: str, raw: int) -> float:
        t = self.by_name[name]
        return raw / t.scale if t.table in ("holding", "input") else float(raw)

    def describe(self) -> List[dict]:
        return [t.to_dict() for t in self.tags]


def status_tags() -> List[Tag]:
    return [
        Tag("PLC_MODE", "holding", HR_MODE, 1, "", "PLC mode (0 stop, 1 run)"),
        Tag("SCAN_COUNT", "holding", HR_SCAN, 1, "", "Scan counter"),
        Tag("INTERLOCK_WORD", "holding", HR_INTERLOCK, 1, "", "Interlock word"),
        Tag("ALARM_WORD", "holding", HR_ALARM, 1, "", "Alarm word"),
        Tag("UPTIME_MIN", "holding", HR_UPTIME, 1, "min", "Uptime (simulated minutes)"),
        Tag("SIM_HOUR", "holding", HR_SIMHOUR, 100, "h", "Simulated hour of day"),
        Tag("REMOTE_OK", "holding", HR_REMOTE_OK, 1, "", "Remote PLC link word"),
        Tag("ALARM_RESET", "coils", 16, 1, "", "Reset latched interlocks/alarms (engineering)", "engineer"),
        Tag("RESET_CMD", "coils", 4, 1, "", "Reset latched interlocks/alarms (supervisor, from the HMI)", "supervisor"),
    ]
