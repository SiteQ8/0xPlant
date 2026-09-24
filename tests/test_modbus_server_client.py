import asyncio

import pytest

from modbuslite import DataStore, DeviceIdentity, ModbusClient, ModbusException, ModbusServer, codec
from tests.conftest import free_port


def test_server_client_roundtrip(run):
    async def inner():
        store = DataStore()
        store.holding[100] = 1234
        store.input[0] = 4321
        store.coils[3] = 1
        port = free_port()
        srv = ModbusServer(store, DeviceIdentity(model_name="vPLC-TEST"), "127.0.0.1", port)
        await srv.start()
        async with ModbusClient("127.0.0.1", port) as c:
            assert await c.read_holding_registers(100, 2) == [1234, 0]
            assert await c.read_input_registers(0, 1) == [4321]
            assert await c.read_coils(0, 8) == [0, 0, 0, 1, 0, 0, 0, 0]
            await c.write_register(101, 77)
            await c.write_registers(102, [1, 2, 3])
            await c.write_coil(5, True)
            await c.write_coils(8, [1, 0, 1, 1])
            assert await c.read_holding_registers(100, 5) == [1234, 77, 1, 2, 3]
            assert await c.read_coils(0, 12) == [0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 1]
            assert (await c.read_device_identification())["ModelName"] == "vPLC-TEST"
            with pytest.raises(ModbusException) as exc:
                await c.read_holding_registers(250, 20)
            assert exc.value.code == codec.EXC_ILLEGAL_ADDRESS
            with pytest.raises(ModbusException) as exc:
                await c.transact(bytes([0x11]))
            assert exc.value.code == codec.EXC_ILLEGAL_FUNCTION
        await srv.stop()
    run(inner())


def test_write_guard_and_hooks(run):
    async def inner():
        store = DataStore()
        seen = []
        store.on_write = lambda table, addr, vals, old: seen.append((table, addr, vals, old))
        store.write_guard = lambda table, addr, vals: codec.EXC_ILLEGAL_ADDRESS if addr < 10 else None
        port = free_port()
        srv = ModbusServer(store, host="127.0.0.1", port=port)
        await srv.start()
        async with ModbusClient("127.0.0.1", port) as c:
            with pytest.raises(ModbusException):
                await c.write_register(2, 1)
            await c.write_register(20, 9)
        assert seen == [("holding", 20, [9], [0])]
        await srv.stop()
    run(inner())


def test_pymodbus_interoperability(run):
    pymodbus = pytest.importorskip("pymodbus")
    from pymodbus.client import AsyncModbusTcpClient

    async def inner():
        store = DataStore()
        store.holding[100:105] = [1, 2, 3, 4, 5]
        port = free_port()
        srv = ModbusServer(store, host="127.0.0.1", port=port)
        await srv.start()
        pc = AsyncModbusTcpClient("127.0.0.1", port=port)
        await pc.connect()
        rr = await pc.read_holding_registers(100, count=5)
        assert rr.registers == [1, 2, 3, 4, 5]
        wr = await pc.write_register(110, 999)
        assert not wr.isError() and store.holding[110] == 999
        pc.close()
        await srv.stop()
    run(inner())
