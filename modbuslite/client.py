"""Asyncio Modbus/TCP client."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from . import codec

log = logging.getLogger("modbuslite.client")


class ModbusClient:
    """A minimal Modbus/TCP client. One outstanding request at a time per connection."""

    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 2.0,
                 local_addr: Optional[Tuple[str, int]] = None):
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self.local_addr = local_addr
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._transaction = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        kwargs = {"local_addr": self.local_addr} if self.local_addr else {}
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, **kwargs), self.timeout)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = self._writer = None

    async def __aenter__(self) -> "ModbusClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def transact(self, pdu: bytes) -> bytes:
        """Send a request PDU and return the response PDU (raises ModbusException on errors)."""
        async with self._lock:
            if not self.connected:
                await self.connect()
            assert self._reader and self._writer
            self._transaction = (self._transaction + 1) & 0xFFFF
            tid = self._transaction
            try:
                self._writer.write(codec.build_mbap(tid, self.unit, pdu))
                await asyncio.wait_for(self._writer.drain(), self.timeout)
                while True:
                    rtid, _unit, rpdu = await asyncio.wait_for(codec.read_frame(self._reader), self.timeout)
                    if rtid == tid:
                        break
                    log.debug("discarding stale response tid=%d", rtid)
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, codec.DecodeError):
                await self.close()
                raise
            if not rpdu:
                await self.close()
                raise codec.DecodeError("empty response")
            if rpdu[0] & 0x80:
                code = rpdu[1] if len(rpdu) > 1 else 0
                raise codec.ModbusException(rpdu[0] & 0x7F, code)
            if rpdu[0] != pdu[0]:
                await self.close()
                raise codec.DecodeError("function code mismatch in response")
            return rpdu

    # --- reads ---
    async def _checked(self, rpdu: bytes, expected: int) -> bytes:
        if len(rpdu) < 2 or rpdu[1] != expected or len(rpdu) < 2 + expected:
            await self.close()
            raise codec.DecodeError("short or malformed read response")
        return rpdu[2:2 + expected]

    async def _read_bits(self, fc: int, address: int, count: int) -> List[int]:
        rpdu = await self.transact(codec.encode_read(fc, address, count))
        return codec.bytes_to_bits(await self._checked(rpdu, (count + 7) // 8), count)

    async def _read_regs(self, fc: int, address: int, count: int) -> List[int]:
        rpdu = await self.transact(codec.encode_read(fc, address, count))
        return codec.bytes_to_regs(await self._checked(rpdu, count * 2))

    async def read_coils(self, address: int, count: int = 1) -> List[int]:
        return await self._read_bits(codec.FC_READ_COILS, address, count)

    async def read_discrete_inputs(self, address: int, count: int = 1) -> List[int]:
        return await self._read_bits(codec.FC_READ_DISCRETE, address, count)

    async def read_holding_registers(self, address: int, count: int = 1) -> List[int]:
        return await self._read_regs(codec.FC_READ_HOLDING, address, count)

    async def read_input_registers(self, address: int, count: int = 1) -> List[int]:
        return await self._read_regs(codec.FC_READ_INPUT, address, count)

    # --- writes ---
    async def write_coil(self, address: int, value: bool) -> None:
        await self.transact(codec.encode_write_coil(address, value))

    async def write_register(self, address: int, value: int) -> None:
        await self.transact(codec.encode_write_register(address, value))

    async def write_coils(self, address: int, values: List[int]) -> None:
        await self.transact(codec.encode_write_coils(address, values))

    async def write_registers(self, address: int, values: List[int]) -> None:
        await self.transact(codec.encode_write_registers(address, values))

    # --- identification ---
    async def read_device_identification(self, code: int = 0x02) -> Dict[str, str]:
        objects: Dict[str, str] = {}
        next_id = 0x00
        for _ in range(8):   # follow "more follows" segments, bounded
            rpdu = await self.transact(codec.encode_device_id(code, next_id))
            objects.update(codec.decode_device_id(rpdu))
            if len(rpdu) < 6 or rpdu[4] != 0xFF or rpdu[5] <= next_id:
                break
            next_id = rpdu[5]
        return objects
