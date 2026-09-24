import asyncio

from modbuslite import ModbusClient
from oxplant.config import IntegrityConfig, IntegrityTarget, TagConfig
from oxplant.integrity import IntegrityMonitor
from tests.conftest import free_port, start_plc, stop_plc, wait_for


class Sink:
    def __init__(self):
        self.events, self.changes, self.status, self.ident = [], [], [], {}
        self.golden = {}

    def monitor(self, cfg):
        return IntegrityMonitor(cfg, emit=self.events.append, on_change=lambda *a, **k: self.changes.append((a, k)),
                                get_golden=lambda a, t: self.golden.get((a, t)), get_identity=lambda a: self.ident.get(a),
                                set_identity=lambda a, i: self.ident.__setitem__(a, i),
                                on_asset_status=lambda a, s: self.status.append((a, s)), on_asset_identity=lambda a, i: None)

    def rules(self):
        return [e.rule for e in self.events]


def cfg_for(port):
    return IntegrityConfig(source_ip=None, poll_s=0.05, identity_s=0.3, targets=[IntegrityTarget("PLC-T", "127.0.0.1", port, 1, [
        TagConfig("PUMP_SPEED_SP", "holding", 102, 100, "%", role="process", min=0, max=50),      # 80 by default -> excursion
        TagConfig("LT-101", "input", 0, 100, "%", role="process", min=1, max=99),
        TagConfig("LAHH_LIMIT", "holding", 200, 100, "%", role="config", golden=95),
        TagConfig("LEVEL_START_SP", "holding", 100, 100, "%", role="setpoint"),
        TagConfig("TRIP_TEST", "holding", 110, 1, "", role="interlock"),
        TagConfig("AUTO_ACTIVE", "discrete", 5, 1, "", role="status"),
    ])])


def test_envelope_drift_setpoint_interlock_and_identity(run):
    async def inner():
        port = free_port()
        plc, task = await start_plc("PLC-T", "intake", port)
        sink = Sink()
        mon = sink.monitor(cfg_for(port))
        stop = asyncio.Event()
        mtask = asyncio.create_task(mon.run(stop))
        # excursion after debounce
        assert await wait_for(lambda: "OXP-005" in sink.rules(), 5)
        ev = next(e for e in sink.events if e.rule == "OXP-005")
        assert ev.detail["tag"] == "PUMP_SPEED_SP" and ev.alert_key == "OXP-005:PLC-T:PUMP_SPEED_SP"
        assert mon.live["PLC-T"]["tags"]["PUMP_SPEED_SP"]["status"] == "high"
        async with ModbusClient("127.0.0.1", port) as c:
            await c.write_register(102, 3000)                               # back inside the envelope
            assert await wait_for(lambda: any(e.resolve_key == "OXP-005:PLC-T:PUMP_SPEED_SP" for e in sink.events), 5)
            await c.write_register(200, 9000)                               # engineering change -> drift
            assert await wait_for(lambda: "OXP-006" in sink.rules(), 5)
            assert sink.changes and sink.changes[0][0][:6] == ("PLC-T", "LAHH_LIMIT", "config", 95, 90.0, "unreviewed")
            sink.golden[("PLC-T", "LAHH_LIMIT")] = 90.0                     # approved: becomes the golden value
            assert await wait_for(lambda: any(e.resolve_key == "OXP-006:PLC-T:LAHH_LIMIT" for e in sink.events), 5)
            await c.write_register(100, 4500)                               # operator setpoint
            assert await wait_for(lambda: "OXP-013" in sink.rules(), 5)
            assert any(ch[0][2] == "setpoint" and ch[0][5] == "logged" for ch in sink.changes)
            await c.write_register(110, 1)                                  # interlock word style register
            assert await wait_for(lambda: "OXP-012" in sink.rules(), 5)
            await c.write_register(110, 0)
            assert await wait_for(lambda: any(e.resolve_key == "OXP-012:PLC-T:TRIP_TEST" for e in sink.events), 5)
        # identity change (firmware revision)
        assert await wait_for(lambda: "PLC-T" in sink.ident, 5)
        plc.server.identity.revision = "9.9.9"
        assert await wait_for(lambda: "OXP-008" in sink.rules(), 5)
        ev = next(e for e in sink.events if e.rule == "OXP-008")
        assert ev.detail["diff"]["MajorMinorRevision"] == ["1.3.2", "9.9.9"] or ev.detail["diff"]["MajorMinorRevision"] == ("1.3.2", "9.9.9")
        assert ("PLC-T", "online") in sink.status
        # offline detection and recovery
        await stop_plc(plc, task)
        assert await wait_for(lambda: "OXP-007" in sink.rules(), 8)
        assert mon.live["PLC-T"]["online"] is False
        plc, task = await start_plc("PLC-T", "intake", port)
        assert await wait_for(lambda: any(e.resolve_key == "OXP-007:PLC-T" for e in sink.events), 8)
        stop.set()
        await asyncio.wait_for(mtask, 5)
        await stop_plc(plc, task)
    run(inner())
