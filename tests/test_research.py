"""Tests for the research instrumentation: faults, invariants, baselining, recorder, MQTT layer, metrics."""
import asyncio
import json
import os
import tempfile
import time

import pytest

from modbuslite import DataStore, ModbusClient
from oxplant.baseline import Baseline, Recorder
from oxplant.invariants import Invariant
from oxplant.mqttmon import MQTTMonitor
from oxplant.events import EventBus
from plant.faults import FaultBoard
from plant.mqtt import MQTTBroker, MQTTClient, topic_matches
from plant.programs import Intake
from tests.conftest import free_port, start_plc, stop_plc, wait_for


# ---------------- invariants ----------------
def test_invariant_rejects_unsafe_expressions():
    for bad in ("__import__('os')", "v.__class__", "open('x')", "lambda: 1", "[1 for i in range(3)]", "dt.real"):
        with pytest.raises(ValueError):
            Invariant("x", bad)


def test_invariant_evaluation_and_debounce():
    inv = Invariant("balance", "abs((v('L') - prev('L')) / dt - v('F') / 10) < 0.1", debounce=2)
    assert inv.evaluate({"L": 50.0, "F": 10.0}, {"L": 49.0}, 1.0) is True
    assert inv.evaluate({"L": 50.0, "F": 10.0}, {"L": 50.0}, 1.0) is False
    assert inv.evaluate({"L": 50.0}, {"L": 50.0}, 1.0) is None            # missing F
    assert inv.evaluate({"L": 50.0, "F": 1.0}, {"L": 49.0}, 0.0) is None   # no previous poll yet
    assert inv.update(False) is None and inv.update(False) == "violated" and inv.alerting
    assert inv.update(True) is None and inv.update(True) == "restored" and not inv.alerting


# ---------------- fault board ----------------
def test_fault_board_sensor_and_actuator_faults():
    p = Intake(DataStore(coils=32, discrete=32, holding=256, input=64), seed=1)
    board = FaultBoard()
    p.faults = board
    p.set("AUTO_MODE", 0)
    p.set("PUMP_START_CMD", 1)
    p.execute(0.01)
    assert p.pump_run
    board.inject({"type": "actuator_fail", "tag": "P-101", "duration_s": 5})
    p.execute(0.01)
    assert not p.pump_run
    board.inject({"type": "sensor_stuck", "tag": "LT-101"})
    p.execute(0.01)
    frozen = p.get("LT-101")
    p.volume *= 0.5
    p.execute(0.01)
    assert p.get("LT-101") == frozen and p.level < frozen           # reported value frozen, true level moved
    board.inject({"type": "sensor_offset", "tag": "AT-101", "value": 30})
    p.execute(0.01)
    assert p.get("AT-101") > 25
    board.inject({"type": "clear"})
    p.execute(0.01)
    assert abs(p.get("LT-101") - p.level) < 0.02
    with pytest.raises(ValueError):
        board.inject({"type": "nonsense"})
    f = board.inject({"type": "blackout", "duration_s": 0.01})
    time.sleep(0.02)
    assert not f.active and board.get("blackout") is None


def test_fault_api_and_blackout_on_a_running_plc(run):
    import urllib.request

    async def inner():
        port, sim = free_port(), free_port()
        from plant.config import PLCConfig
        from plant.plc import SoftPLC
        plc = SoftPLC(PLCConfig("PLC-T", "intake", "127.0.0.1", port, seed=1, sim_host="127.0.0.1", sim_port=sim), time_scale=600, scan_ms=50)
        task = asyncio.create_task(plc.run())
        await asyncio.sleep(0.4)
        loop = asyncio.get_running_loop()

        def post(spec):
            req = urllib.request.Request(f"http://127.0.0.1:{sim}/fault", data=json.dumps(spec).encode(), method="POST", headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=5).read())

        r = await loop.run_in_executor(None, post, {"type": "identity", "value": "9.9.9", "duration_s": 2})
        assert r["ok"]
        async with ModbusClient("127.0.0.1", port) as c:
            assert (await c.read_device_identification())["MajorMinorRevision"] == "9.9.9"
        await loop.run_in_executor(None, post, {"type": "blackout", "duration_s": 1.5})
        assert await wait_for(lambda: plc.server._server is None, 3)
        with pytest.raises(Exception):
            async with ModbusClient("127.0.0.1", port, timeout=1) as c:
                await c.read_input_registers(0, 1)
        assert await wait_for(lambda: plc.server._server is not None, 4)
        async with ModbusClient("127.0.0.1", port) as c:
            assert len(await c.read_input_registers(0, 1)) == 1
            assert await wait_for(lambda: plc.server.identity.revision == "1.3.2", 3)
        listing = await loop.run_in_executor(None, lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{sim}/faults", timeout=5).read()))
        assert listing["plc"] == "PLC-T" and listing["injected"] == 2
        await stop_plc(plc, task)
    run(inner())


# ---------------- baseline + recorder ----------------
def test_baseline_learns_then_reports_new_patterns_and_rates():
    b = Baseline(learning_s=0.2, rate_factor=2.0, min_rate=3)
    t = 1000.0
    for i in range(4):
        assert b.observe("10.0.0.5", 3, 0, 10, now=t + i * 0.02) is None
    assert b.state == "learning"
    b.started -= 1.0                          # learning window over
    assert b.state == "enforcing"
    assert b.observe("10.0.0.5", 3, 0, 10, now=t + 1) is None                 # known pattern
    a = b.observe("10.0.0.5", 3, 150, 8, now=t + 1)
    assert a and a["kind"] == "new_pattern" and a["address"] == 150
    assert b.observe("10.0.0.5", 3, 150, 8, now=t + 1) is None                 # reported once
    rate = [b.observe("10.0.0.5", 3, 0, 10, now=t + 2) for _ in range(10)]
    assert any(x and x["kind"] == "rate" for x in rate)
    assert b.observe("10.0.0.9", 3, 0, 10, now=t + 2)["kind"] == "new_pattern"  # unknown source after learning
    assert Baseline(0).observe("x", 3, 0, 1) is None and Baseline(0).state == "off"
    d = b.describe()
    assert d["sources"]["10.0.0.5"]["patterns"] == 1


def test_recorder_writes_jsonl():
    with tempfile.TemporaryDirectory() as d:
        r = Recorder(os.path.join(d, "sub", "traffic.jsonl"))
        r.write({"a": 1, "allowed": False})
        r.write({"b": 2})
        r.close()
        lines = open(os.path.join(d, "sub", "traffic.jsonl")).read().splitlines()
        assert len(lines) == 2 and json.loads(lines[0]) == {"a": 1, "allowed": False}


# ---------------- MQTT ----------------
def test_topic_matching():
    assert topic_matches("plant/#", "plant/intake/VIB-101") and topic_matches("#", "a")
    assert topic_matches("plant/+/VIB-101", "plant/intake/VIB-101") and not topic_matches("plant/+/VIB-101", "plant/intake/x/VIB-101")
    assert not topic_matches("plant/intake", "plant/intake/VIB-101") and topic_matches("plant/intake", "plant/intake")


def test_broker_pubsub_retained_and_monitor(run):
    async def inner():
        port = free_port()
        seen = []
        broker = MQTTBroker("127.0.0.1", port, on_publish=lambda cid, topic, payload, peer: seen.append((cid, topic)))
        await broker.start()
        sub = MQTTClient("127.0.0.1", port, client_id="sub")
        await sub.connect()
        await sub.subscribe("plant/#")
        pub = MQTTClient("127.0.0.1", port, client_id="pub")
        await pub.connect()
        await pub.publish("plant/intake/VIB-101", b'{"sensor":"VIB-101","vibration_mm_s":2.5}', qos=1)
        await pub.publish("plant/site/ENV-001", b'{"sensor":"ENV-001","temperature_c":28}', retain=True)
        got = []
        async def collect():
            async for topic, payload, retained in sub.messages():
                got.append((topic, json.loads(payload), retained))
                if len(got) == 2:
                    break
        await asyncio.wait_for(collect(), 5)
        assert got[0][0] == "plant/intake/VIB-101" and got[1][1]["sensor"] == "ENV-001" and seen[0] == ("pub", "plant/intake/VIB-101")
        late = MQTTClient("127.0.0.1", port, client_id="late")
        await late.connect()
        await late.subscribe("plant/site/+")
        async def one():
            async for topic, payload, retained in late.messages():
                return topic, retained
        assert await asyncio.wait_for(one(), 5) == ("plant/site/ENV-001", True)
        assert broker.messages == 2 and set(broker.sessions) == {"sub", "pub", "late"}

        # monitor: learn, then flag a new topic, a new publisher, a bad payload and an out-of-range value
        events = []
        bus = EventBus()
        bus.subscribe(events.append)
        mon = MQTTMonitor({"host": "127.0.0.1", "port": port, "learning_s": 0.3, "consecutive": 2}, bus, "SENSOR-T")
        stop = asyncio.Event()
        mtask = asyncio.create_task(mon.run(stop))
        await asyncio.sleep(0.15)
        for v in (2.4, 2.6, 2.5):
            await pub.publish("plant/intake/VIB-101", json.dumps({"sensor": "VIB-101", "vibration_mm_s": v}).encode())
        await asyncio.sleep(0.4)
        assert not mon.learning and "plant/intake/VIB-101" in mon.topics and "VIB-101" in mon.publishers
        await pub.publish("plant/intake/VIB-999", b'{"sensor":"VIB-999","vibration_mm_s":9}')
        await pub.publish("plant/intake/VIB-101", b'{"sensor":"VIB-777","vibration_mm_s":2.5}')
        await pub.publish("plant/intake/VIB-101", b'not json')
        for _ in range(3):
            await pub.publish("plant/intake/VIB-101", b'{"sensor":"VIB-101","vibration_mm_s":40}')
        assert await wait_for(lambda: sum(1 for e in events if e.rule == "OXP-020") >= 2 and sum(1 for e in events if e.rule == "OXP-019") >= 2, 5)
        kinds = {(e.rule, e.detail.get("topic"), e.detail.get("sensor") or e.detail.get("field")) for e in events}
        assert ("OXP-019", "plant/intake/VIB-999", "VIB-999") in kinds and ("OXP-019", "plant/intake/VIB-101", "VIB-777") in kinds
        assert any(e.rule == "OXP-020" and e.detail.get("field") == "vibration_mm_s" for e in events)
        flows = mon.flow_records()
        assert {f["source_ip"] for f in flows} >= {"plant/intake/VIB-101", "plant/intake/VIB-999"} and all(f["protocol"] == "MQTT" for f in flows)
        stop.set()
        await sub.close(); await pub.close(); await late.close()
        await broker.stop()
        mtask.cancel()
    run(inner())


# ---------------- integrity invariants against a live PLC ----------------
def test_integrity_invariant_flags_a_stuck_sensor(run):
    from oxplant.config import IntegrityConfig, IntegrityTarget, InvariantConfig, TagConfig
    from oxplant.integrity import IntegrityMonitor

    async def inner():
        port = free_port()
        plc, task = await start_plc("PLC-T", "intake", port, time_scale=600, scan_ms=50)
        plc.program.set("AUTO_MODE", 0)               # pump off: tank only drains through the (absent) draw -> level static
        events = []
        cfg = IntegrityConfig(poll_s=0.1, identity_s=999, targets=[IntegrityTarget("PLC-T", "127.0.0.1", port, 1,
            [TagConfig("LT-101", "input", 0, 100, "%"), TagConfig("FT-101", "input", 1, 10), TagConfig("FT-201-R", "input", 6, 10)],
            [InvariantConfig("balance", "not (1 < v('LT-101') < 99) or abs((v('LT-101') - prev('LT-101')) / dt - (v('FT-101') - v('FT-201-R')) / 30) < 0.5", 3)])])
        mon = IntegrityMonitor(cfg, emit=events.append, on_change=lambda *a, **k: None, get_golden=lambda a, t: None,
                               get_identity=lambda a: None, set_identity=lambda a, i: None, on_asset_status=lambda a, s: None, on_asset_identity=lambda a, i: None)
        stop = asyncio.Event()
        mtask = asyncio.create_task(mon.run(stop))
        await asyncio.sleep(1.0)
        assert not any(e.rule == "OXP-018" for e in events)
        assert mon.live["PLC-T"]["invariants"]["balance"]["status"] == "ok"
        # freeze the level while the pump runs: reported level no longer follows the inflow
        plc.faults.inject({"type": "sensor_stuck", "tag": "LT-101"})
        plc.program.set("PUMP_START_CMD", 1)
        plc.program.set("VALVE_OPEN_CMD", 1)
        assert await wait_for(lambda: any(e.rule == "OXP-018" for e in events), 8)
        plc.faults.inject({"type": "clear"})
        assert await wait_for(lambda: any(e.resolve_key == "OXP-018:PLC-T:balance" for e in events), 8)
        stop.set()
        await asyncio.wait_for(mtask, 5)
        await stop_plc(plc, task)
    run(inner())
