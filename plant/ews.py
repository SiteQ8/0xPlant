"""Engineering workstation tool: read, write and identify PLC tags by name from the command line."""
from __future__ import annotations

import asyncio
import sys
from typing import List, Optional

from modbuslite import ModbusClient, ModbusException

from .config import PlantConfig
from .programs import PROGRAMS
from .registers import TagMap, status_tags


def _target(cfg: PlantConfig, plc_name: str):
    for p in cfg.hmi.plcs:
        if p.name == plc_name:
            return p
    raise SystemExit(f"unknown PLC {plc_name}; known: {[p.name for p in cfg.hmi.plcs]}")


async def run(cfg: PlantConfig, plc_name: str, action: str, args: List[str], source_ip: Optional[str] = None) -> int:
    target = _target(cfg, plc_name)
    tag_map = TagMap(list(PROGRAMS[target.program].tags) + status_tags())
    src = source_ip or cfg.ews_source_ip
    kwargs = {"local_addr": (src, 0)} if src else {}
    client = ModbusClient(target.host, target.port, unit=target.unit, timeout=2.0, **kwargs)
    try:
        await client.connect()
        if action == "identify":
            for k, v in (await client.read_device_identification(0x02)).items():
                print(f"{k:22s} {v}")
        elif action == "read":
            wanted = set(args)
            for t in tag_map:
                if wanted and t.name not in wanted:
                    continue
                if t.table == "input":
                    raw = (await client.read_input_registers(t.address, 1))[0]
                elif t.table == "holding":
                    raw = (await client.read_holding_registers(t.address, 1))[0]
                elif t.table == "coils":
                    raw = (await client.read_coils(t.address, 1))[0]
                else:
                    raw = (await client.read_discrete_inputs(t.address, 1))[0]
                print(f"{t.name:18s} {t.table:8s} {t.address:4d}  {tag_map.decode(t.name, raw):>10.2f} {t.unit:5s} [{t.access}] {t.desc}")
        elif action == "write":
            for item in args:
                name, _, value = item.partition("=")
                if name not in tag_map.by_name:
                    print(f"unknown tag {name}", file=sys.stderr)
                    return 2
                t = tag_map.by_name[name]
                try:
                    if t.table == "holding":
                        await client.write_register(t.address, tag_map.encode(name, float(value)))
                    elif t.table == "coils":
                        await client.write_coil(t.address, value.lower() in ("1", "true", "on"))
                    else:
                        print(f"{name} is read-only", file=sys.stderr)
                        return 2
                    print(f"{name} <- {value}  ok")
                except ModbusException as exc:
                    print(f"{name} <- {value}  REJECTED: {exc}")
                    return 1
        elif action == "reset":
            await client.write_coil(tag_map.by_name["ALARM_RESET"].address, True)
            print("alarm reset issued")
        else:
            print(f"unknown action {action}", file=sys.stderr)
            return 2
    finally:
        await client.close()
    return 0
