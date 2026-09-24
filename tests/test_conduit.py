import asyncio

import pytest

from modbuslite import ModbusClient, ModbusException, codec
from oxplant.conduit import Conduit
from oxplant.events import EventBus
from oxplant.policy import ConduitPolicy, SourceRule, parse_ranges
from tests.conftest import can_bind, free_port, start_plc, stop_plc, wait_for


def make_conduit(upstream_port, listen_port, bus, rules=None, default="deny"):
    rules = rules if rules is not None else [
        SourceRule("127.0.0.1", "HMI-T", allow=["read"], writes={"holding": parse_ranges("100-119"), "coils": parse_ranges("0-7")}),
        SourceRule("127.0.0.2", "EWS-T", allow=["read", "write", "identification"]),
    ]
    policy = ConduitPolicy("CONDUIT-T", "PLC-T", rules, default=default, max_rps=500)
    return Conduit(policy, ("127.0.0.1", listen_port), ("127.0.0.1", upstream_port), bus, "SENSOR-T", upstream_timeout=1.0)


def test_conduit_enforces_policy_and_reports_events(run):
    async def inner():
        events = []
        bus = EventBus()
        bus.subscribe(events.append)
        up, lp = free_port(), free_port()
        plc, task = await start_plc("PLC-T", "treatment", up)
        conduit = make_conduit(up, lp, bus)
        await conduit.start()
        async with ModbusClient("127.0.0.1", lp) as hmi:
            assert len(await hmi.read_input_registers(0, 4)) == 4
            await hmi.write_register(100, 180)                      # setpoint region: allowed and forwarded
            assert plc.store.holding[100] == 180
            with pytest.raises(ModbusException) as exc:
                await hmi.write_register(200, 500)                  # engineering region: blocked
            assert exc.value.code == codec.EXC_ILLEGAL_ADDRESS and plc.store.holding[200] == 400
            with pytest.raises(ModbusException) as exc:
                await hmi.read_device_identification()
            assert exc.value.code == codec.EXC_ILLEGAL_FUNCTION
        rules = [e.rule for e in events]
        assert "OXP-015" in rules and "OXP-002" in rules and "OXP-001" in rules
        blocked = next(e for e in events if e.rule == "OXP-002")
        assert blocked.asset == "PLC-T" and blocked.source_ip == "127.0.0.1" and blocked.detail["address"] == 200
        assert blocked.alert_key == "OXP-002:PLC-T:127.0.0.1" and blocked.sensor == "SENSOR-T"
        forwarded = [e for e in events if e.category == "OPERATIONS" and "wrote" in e.title]
        assert forwarded and forwarded[0].detail["values"] == [180]
        flows = conduit.flow_records()
        assert flows[0]["source_asset"] == "HMI-T" and flows[0]["requests"] == 4 and flows[0]["denied"] == 2
        await conduit.stop()
        await stop_plc(plc, task)
    run(inner())


def test_conduit_blocks_unlisted_source(run):
    if not can_bind("127.0.0.3"):
        pytest.skip("loopback aliases not available")

    async def inner():
        events = []
        bus = EventBus()
        bus.subscribe(events.append)
        up, lp = free_port(), free_port()
        plc, task = await start_plc("PLC-T", "intake", up)
        conduit = make_conduit(up, lp, bus)
        await conduit.start()
        async with ModbusClient("127.0.0.1", lp, local_addr=("127.0.0.3", 0)) as other:
            with pytest.raises(ModbusException) as exc:
                await other.read_holding_registers(0, 1)
            assert exc.value.code == codec.EXC_ILLEGAL_FUNCTION
        ev = next(e for e in events if e.rule == "OXP-003")
        assert ev.source_ip == "127.0.0.3" and ev.severity == "critical"
        assert not any(e.rule == "OXP-015" and e.source_ip == "127.0.0.3" for e in events)
        async with ModbusClient("127.0.0.1", lp, local_addr=("127.0.0.2", 0)) as ews:
            assert (await ews.read_device_identification())["ProductCode"] == "vPLC-INTAKE"
            await ews.write_register(200, 9200)
            assert plc.store.holding[200] == 9200
        await conduit.stop()
        await stop_plc(plc, task)
    run(inner())


def test_conduit_drops_malformed_frames(run):
    async def inner():
        events = []
        bus = EventBus()
        bus.subscribe(events.append)
        up, lp = free_port(), free_port()
        plc, task = await start_plc("PLC-T", "intake", up)
        conduit = make_conduit(up, lp, bus)
        await conduit.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", lp)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(100), 2)
        assert data == b""                                            # connection closed by the conduit
        writer.close()
        assert await wait_for(lambda: any(e.rule == "OXP-011" for e in events), 2)
        await conduit.stop()
        await stop_plc(plc, task)
    run(inner())


def test_conduit_reports_unreachable_upstream(run):
    async def inner():
        events = []
        bus = EventBus()
        bus.subscribe(events.append)
        up, lp = free_port(), free_port()
        conduit = make_conduit(up, lp, bus)
        await conduit.start()
        async with ModbusClient("127.0.0.1", lp, timeout=5) as hmi:
            with pytest.raises(ModbusException) as exc:
                await hmi.read_input_registers(0, 1)
            assert exc.value.code == codec.EXC_GATEWAY_TARGET
        assert any(e.rule == "OXP-016" for e in events)
        # bring the PLC up: the next request succeeds and the availability alert is resolved
        plc, task = await start_plc("PLC-T", "intake", up)
        async with ModbusClient("127.0.0.1", lp, timeout=5) as hmi:
            assert len(await hmi.read_input_registers(0, 1)) == 1
        assert any(e.resolve_key == "OXP-016:PLC-T" for e in events)
        await conduit.stop()
        await stop_plc(plc, task)
    run(inner())
