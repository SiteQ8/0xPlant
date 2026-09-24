"""Shared helpers for the 0xPlant test-suite (plain asyncio, no pytest-asyncio needed)."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from plant.config import PLCConfig, RemoteConfig  # noqa: E402
from plant.plc import SoftPLC  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def can_bind(ip: str) -> bool:
    try:
        with socket.socket() as s:
            s.bind((ip, 0))
        return True
    except OSError:
        return False


async def start_plc(name: str, program: str, port: int, remotes=None, time_scale: float = 600, scan_ms: int = 50, seed: int = 1):
    cfg = PLCConfig(name, program, "127.0.0.1", port, seed=seed,
                    remotes=[RemoteConfig(n, "127.0.0.1", p) for n, p in (remotes or [])])
    plc = SoftPLC(cfg, time_scale=time_scale, scan_ms=scan_ms)
    task = asyncio.create_task(plc.run())
    await asyncio.sleep(0.2)
    return plc, task


async def stop_plc(plc, task):
    plc.stop()
    try:
        await asyncio.wait_for(task, 3)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture
def run():
    """Run a coroutine function to completion inside a fresh event loop."""
    def _run(coro):
        return asyncio.run(coro)
    return _run
