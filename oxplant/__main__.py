"""Command line entry point: python -m oxplant <command>."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys

from . import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m oxplant", description=f"0xPlant {__version__} - ICS/OT security")
    parser.add_argument("-c", "--config", default="config/oxplant.local.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("console", help="run the console (web UI, API, integrity monitor, alerting)")
    p_sensor = sub.add_parser("sensor", help="run a sensor (conduits + discovery)")
    p_sensor.add_argument("--name", required=True)
    p_scan = sub.add_parser("scan", help="one-off discovery scan")
    p_scan.add_argument("targets", nargs="*", help="IPs, CIDRs or ranges (default: all configured scopes)")
    p_scan.add_argument("--ports", default="", help="comma separated port list")
    sub.add_parser("policy-check", help="validate the configuration and print the conduit policies")
    sub.add_parser("hash-password", help="hash a password for the console user list")
    sub.add_parser("version")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "version":
        print(f"0xPlant {__version__}")
        return 0
    if args.cmd == "hash-password":
        from .auth import hash_password
        pw = getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        print(hash_password(pw))
        return 0

    from . import config as oxconfig
    cfg = oxconfig.load(args.config)

    if args.cmd == "policy-check":
        problems = oxconfig.validate(cfg)
        for c in cfg.conduits:
            print(f"{c.id}: {c.listen_host}:{c.listen_port} -> {c.upstream_host}:{c.upstream_port} protects {c.asset} (default {c.policy.default}, {c.policy.max_rps} req/s)")
            for r in c.policy.rules:
                writes = ", ".join(f"{t}:{','.join(f'{lo}-{hi}' for lo, hi in rs)}" for t, rs in r.writes.items()) or "-"
                print(f"    {r.asset or r.source:14s} {r.source:18s} allow={','.join(r.allow) or '-':30s} writes={writes}")
        for p in problems:
            print(f"WARNING: {p}")
        print(f"{len(cfg.assets)} assets, {len(cfg.conduits)} conduits, {len(cfg.integrity.targets)} integrity targets, {len(problems)} warning(s)")
        return 1 if problems else 0

    if args.cmd == "scan":
        from .discovery import expand_targets, identify_modbus, scan, PORT_PROTOCOLS, MODBUS_PORTS

        async def do_scan():
            if args.targets:
                ports = [int(p) for p in args.ports.split(",")] if args.ports else [502, 5020, 102, 4840, 44818, 20000, 1883, 47808, 2404, 23, 21, 80, 445]
                scopes = [(expand_targets(args.targets), ports)]
            else:
                scopes = [(expand_targets(s.targets), s.ports) for s in cfg.discovery]
            for targets, ports in scopes:
                found = await scan(targets, ports)
                for ip, open_ports in sorted(found.items()):
                    asset = cfg.asset_by_ip(ip)
                    label = f"{asset.id} ({asset.name})" if asset else "UNKNOWN - not in inventory"
                    services = ", ".join(f"{p}/{PORT_PROTOCOLS.get(p, 'tcp')}" for p in open_ports)
                    print(f"{ip:16s} {label:40s} {services}")
                    mb = next((p for p in open_ports if p in MODBUS_PORTS), None)
                    if mb:
                        ident = await identify_modbus(ip, mb)
                        if ident:
                            print(f"{'':16s}   {ident.get('VendorName', '')} {ident.get('ModelName', '')} rev {ident.get('MajorMinorRevision', '')} app {ident.get('UserApplicationName', '')}")
        asyncio.run(do_scan())
        return 0

    if args.cmd == "console":
        from .console import Console
        console = Console(cfg)
        return _run(console.run(), console.stop.set)
    if args.cmd == "sensor":
        from .sensor import Sensor
        sensor = Sensor(cfg, args.name)
        return _run(sensor.run(), sensor.stop.set)
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
