"""Command line entry point for the plant: PLCs, HMI and the engineering tool."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import config as plant_config


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=None, help="plant configuration file (default config/plant.local.yaml)")
    common.add_argument("-v", "--verbose", action="store_true")
    parser = argparse.ArgumentParser(prog="python -m plant", description="0xPlant Water Works simulator", parents=[common])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plc = sub.add_parser("plc", help="run one soft PLC", parents=[common])
    p_plc.add_argument("--name", required=True, help="PLC name from the configuration, e.g. PLC-001")
    sub.add_parser("hmi", help="run the operator HMI", parents=[common])
    sub.add_parser("broker", help="run the IoT MQTT broker", parents=[common])
    sub.add_parser("iot", help="run the virtual IoT sensor fleet", parents=[common])
    p_ews = sub.add_parser("ews", help="engineering workstation tool", parents=[common])
    p_ews.add_argument("--plc", required=True)
    p_ews.add_argument("--source-ip", default=None, help="bind outgoing connection to this address")
    p_ews.add_argument("action", choices=["read", "write", "identify", "reset"])
    p_ews.add_argument("args", nargs="*", help="tag names to read, or TAG=VALUE pairs to write")
    sub.add_parser("tags", help="print the register map of every program", parents=[common])
    args = parser.parse_args(argv)
    args.config = args.config or "config/plant.local.yaml"

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "tags":
        from .programs import PROGRAMS
        from .registers import status_tags
        for name, cls in PROGRAMS.items():
            print(f"== {name}: {cls.description}")
            for t in list(cls.tags) + status_tags():
                print(f"  {t.name:18s} {t.table:8s} {t.address:4d} x{t.scale:<5g} {t.unit:5s} [{t.access:8s}] {t.desc}")
        return 0

    cfg = plant_config.load(args.config)
    if args.cmd == "plc":
        from .plc import SoftPLC
        plc = SoftPLC(cfg.plc(args.name), time_scale=cfg.time_scale, scan_ms=cfg.scan_ms)
        return _run(plc.run(), plc.stop)
    if args.cmd == "hmi":
        from .hmi import HMI
        hmi = HMI(cfg.hmi)
        return _run(hmi.run(), hmi.stop.set)
    if args.cmd == "broker":
        from .mqtt import MQTTBroker
        broker = MQTTBroker(cfg.mqtt.listen, cfg.mqtt.port)

        async def run_broker():
            await broker.start()
            stop = asyncio.Event()
            broker.stop_event = stop
            await stop.wait()
            await broker.stop()
        return _run(run_broker(), lambda: broker.stop_event.set())
    if args.cmd == "iot":
        from .iot import IoTFleet
        import os
        broker_host = os.environ.get("OXPLANT_BROKER_HOST") or (cfg.mqtt.listen if cfg.mqtt.listen != "0.0.0.0" else "127.0.0.1")
        fleet = IoTFleet(broker_host, cfg.mqtt.port,
                         {p.name: (p.host, p.port) for p in cfg.mqtt.plcs}, cfg.mqtt.interval_s, cfg.mqtt.iot_source_ip)
        return _run(fleet.run(), fleet.stop.set)
    if args.cmd == "ews":
        from .ews import run as ews_run
        return asyncio.run(ews_run(cfg, args.plc, args.action, args.args, args.source_ip))
    return 2


def _run(coro, stop) -> int:
    async def runner():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop)
            except NotImplementedError:
                pass
        await coro
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
