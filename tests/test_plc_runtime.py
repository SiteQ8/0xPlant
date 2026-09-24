import pytest

from modbuslite import ModbusClient, ModbusException, codec
from tests.conftest import free_port, start_plc, stop_plc, wait_for


def test_plc_rejects_writes_outside_operator_and_engineer_regions(run):
    async def inner():
        port = free_port()
        plc, task = await start_plc("PLC-T", "intake", port)
        async with ModbusClient("127.0.0.1", port) as c:
            for addr in (0, 2, 9, 50, 150, 220):
                with pytest.raises(ModbusException) as exc:
                    await c.write_register(addr, 1)
                assert exc.value.code == codec.EXC_ILLEGAL_ADDRESS
            await c.write_register(100, 4200)
            await c.write_register(200, 9000)
            await c.write_coil(0, False)
            with pytest.raises(ModbusException):
                await c.write_coil(30, True)
            ident = await c.read_device_identification()
            assert ident["ProductCode"] == "vPLC-INTAKE"
        await stop_plc(plc, task)
    run(inner())


def test_plc_to_plc_links_exchange_process_values(run):
    async def inner():
        p1, p2 = free_port(), free_port()
        intake, t1 = await start_plc("PLC-001", "intake", p1, remotes=[("PLC-002", p2)])
        treat, t2 = await start_plc("PLC-002", "treatment", p2, remotes=[("PLC-001", p1)])
        ok = await wait_for(lambda: intake.program.remote_ok.get("PLC-002") and treat.program.remote_ok.get("PLC-001"), 6)
        assert ok
        assert await wait_for(lambda: intake.program.get("FT-201-R") > 50, 6)
        assert treat.program.get("LT-101-R") > 0
        async with ModbusClient("127.0.0.1", p1) as c:
            hr = await c.read_holding_registers(0, 7)
            assert hr[0] == 1 and hr[6] & 1        # PLC in run mode, remote link bit set
        await stop_plc(treat, t2)
        assert await wait_for(lambda: intake.program.remote_ok.get("PLC-002") is False, 10)
        await stop_plc(intake, t1)
    run(inner())
