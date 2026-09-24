import asyncio

import pytest

from modbuslite import DataStore, DeviceIdentity, ModbusServer
from oxplant.config import AssetConfig, DiscoveryScope
from oxplant.discovery import Discovery, expand_targets, scan
from tests.conftest import free_port


def test_expand_targets():
    assert expand_targets(["10.0.0.1-10.0.0.3"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert expand_targets(["10.0.0.0/30"]) == ["10.0.0.1", "10.0.0.2"]
    assert expand_targets(["1.2.3.4"]) == ["1.2.3.4"]
    with pytest.raises(ValueError):
        expand_targets(["10.0.0.9-10.0.0.1"])


def test_scan_rogue_known_insecure_and_offline(run):
    async def inner():
        open_port, closed_port = free_port(), free_port()
        srv = ModbusServer(DataStore(), DeviceIdentity(model_name="Rogue-9000"), "127.0.0.1", open_port)
        await srv.start()
        found = await scan(["127.0.0.1"], [open_port, closed_port])
        assert found == {"127.0.0.1": [open_port]}

        # 1) nothing in the baseline -> rogue device with identity
        events, assets = [], []
        scope = DiscoveryScope("S", ["127.0.0.1"], [open_port, closed_port], zone="L1")
        d = Discovery([scope], [], events.append, assets.append, runner="S")
        d.baseline = {}
        import oxplant.discovery as disc
        disc.MODBUS_PORTS.add(open_port)
        await d.run_scope(scope)
        assert any(e.rule == "OXP-009" for e in events) and assets[0]["id"] == "UNK-127.0.0.1"
        assert assets[0]["model"] == "Rogue-9000" and not assets[0]["approved"]
        await d.run_scope(scope)
        assert sum(1 for e in events if e.rule == "OXP-009") == 1          # reported once

        # 2) in the baseline -> known asset, insecure service flagged once
        events, assets = [], []
        disc.INSECURE_PORTS[open_port] = "Test cleartext service"
        try:
            baseline = [AssetConfig("PLC-T", "Test PLC", "PLC", "127.0.0.1", "L1", ports=[open_port])]
            d = Discovery([scope], baseline, events.append, assets.append, runner="S")
            await d.run_scope(scope)
            await d.run_scope(scope)
            assert not any(e.rule == "OXP-009" for e in events)
            assert assets[0]["id"] == "PLC-T" and assets[0]["status"] == "online" and assets[0]["approved"]
            assert sum(1 for e in events if e.rule == "OXP-010") == 1
        finally:
            disc.INSECURE_PORTS.pop(open_port, None)
            disc.MODBUS_PORTS.discard(open_port)

        # 3) baseline asset stops answering -> offline after two misses
        await srv.stop()
        events, assets = [], []
        d = Discovery([scope], baseline, events.append, assets.append, runner="S")
        await d.run_scope(scope)
        assert not any(e.rule == "OXP-007" for e in events)
        await d.run_scope(scope)
        assert any(e.rule == "OXP-007" and e.asset == "PLC-T" for e in events)
        assert assets[-1]["status"] == "offline"
    run(inner())
