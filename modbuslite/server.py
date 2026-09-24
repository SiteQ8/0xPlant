"""Asyncio Modbus/TCP server with a simple in-memory data store."""
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import codec
from .codec import Request

log = logging.getLogger("modbuslite.server")

WriteHook = Callable[[str, int, List[int], List[int]], None]
WriteGuard = Callable[[str, int, List[int]], Optional[int]]
RequestHook = Callable[[str, int, Request, bytes], None]


class DataStore:
    """Four Modbus data tables. Addresses are zero-based protocol addresses."""

    def __init__(self, coils: int = 64, discrete: int = 64, holding: int = 256, input: int = 256):
        self.tables: Dict[str, List[int]] = {
            "coils": [0] * coils,
            "discrete": [0] * discrete,
            "holding": [0] * holding,
            "input": [0] * input,
        }
        self.on_write: Optional[WriteHook] = None
        self.write_guard: Optional[WriteGuard] = None

    # convenience accessors used by the PLC runtime
    @property
    def coils(self) -> List[int]:
        return self.tables["coils"]

    @property
    def discrete(self) -> List[int]:
        return self.tables["discrete"]

    @property
    def holding(self) -> List[int]:
        return self.tables["holding"]

    @property
    def input(self) -> List[int]:
        return self.tables["input"]

    def in_range(self, table: str, address: int, quantity: int) -> bool:
        t = self.tables[table]
        return 0 <= address and quantity >= 1 and address + quantity <= len(t)

    def read(self, table: str, address: int, quantity: int) -> List[int]:
        if not self.in_range(table, address, quantity):
            raise IndexError(f"{table}[{address}:{address + quantity}] out of range")
        return list(self.tables[table][address: address + quantity])

    def write(self, table: str, address: int, values: List[int]) -> None:
        if not self.in_range(table, address, len(values)):
            raise IndexError(f"{table}[{address}:{address + len(values)}] out of range")
        t = self.tables[table]
        old = t[address: address + len(values)]
        for i, v in enumerate(values):
            t[address + i] = (1 if v else 0) if table in ("coils", "discrete") else (v & 0xFFFF)
        if self.on_write:
            self.on_write(table, address, list(values), old)


@dataclass
class DeviceIdentity:
    """Objects returned by Read Device Identification (FC 43 / MEI 14)."""

    vendor_name: str = "0xPlant Labs"
    product_code: str = "vPLC"
    revision: str = "1.0.0"
    vendor_url: str = "https://github.com/SiteQ8/0xPlant"
    product_name: str = "0xPlant Virtual PLC"
    model_name: str = "vPLC-1200"
    application_name: str = ""

    def objects(self) -> Dict[int, str]:
        return {
            0x00: self.vendor_name, 0x01: self.product_code, 0x02: self.revision,
            0x03: self.vendor_url, 0x04: self.product_name, 0x05: self.model_name,
            0x06: self.application_name,
        }

    def encode(self, code: int, object_id: int) -> bytes:
        objs = self.objects()
        if code == 0x01:
            ids = [0x00, 0x01, 0x02]
        elif code in (0x02, 0x03):
            ids = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06]
        elif code == 0x04:
            if object_id not in objs:
                return codec.build_exception(codec.FC_ENCAP, codec.EXC_ILLEGAL_ADDRESS)
            ids = [object_id]
        else:
            return codec.build_exception(codec.FC_ENCAP, codec.EXC_ILLEGAL_VALUE)
        ids = [i for i in ids if i >= object_id] if code != 0x04 else ids
        body = bytearray([codec.FC_ENCAP, codec.MEI_DEVICE_ID, code, 0x82, 0x00, 0x00, len(ids)])
        for i in ids:
            value = objs[i].encode("ascii", "replace")[:255]
            body += bytes([i, len(value)]) + value
        return bytes(body)


class ModbusServer:
    """A Modbus/TCP server bound to one data store (one virtual device)."""

    def __init__(self, store: DataStore, identity: Optional[DeviceIdentity] = None,
                 host: str = "0.0.0.0", port: int = 502, unit_id: int = 1,
                 on_request: Optional[RequestHook] = None):
        self.store = store
        self.identity = identity or DeviceIdentity()
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.on_request = on_request
        self._server: Optional[asyncio.AbstractServer] = None
        self._writers: set = set()
        self.connections = 0
        self.requests = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("Modbus/TCP server listening on %s:%d (unit %d)", self.host, self.port, self.unit_id)

    async def stop(self) -> None:
        """Stop listening and drop every client connection (a stopped PLC answers nobody)."""
        if self._server:
            self._server.close()
            for w in list(self._writers):
                w.close()
            self._writers.clear()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except asyncio.TimeoutError:
                pass
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        peer_ip = str(peer[0])
        self.connections += 1
        self._writers.add(writer)
        try:
            while True:
                try:
                    transaction, unit, pdu = await codec.read_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                except codec.DecodeError as exc:
                    log.warning("dropping connection from %s: %s", peer_ip, exc)
                    break
                response = self.process(pdu, unit, peer_ip)
                writer.write(codec.build_mbap(transaction, unit, response))
                try:
                    await writer.drain()
                except ConnectionError:
                    break
        finally:
            self.connections -= 1
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - best effort close
                pass

    def process(self, pdu: bytes, unit: int, peer_ip: str = "") -> bytes:
        """Execute a request PDU against the store and return the response PDU."""
        self.requests += 1
        try:
            req = codec.parse_pdu(pdu)
        except codec.DecodeError:
            fc = pdu[0] if pdu else 0
            return codec.build_exception(fc, codec.EXC_ILLEGAL_VALUE)
        response = self._execute(req)
        if self.on_request:
            try:
                self.on_request(peer_ip, unit, req, response)
            except Exception:  # noqa: BLE001 - hooks must never break the server
                log.exception("request hook failed")
        return response

    def _execute(self, req: Request) -> bytes:
        fc = req.function
        store = self.store
        if fc in codec.READ_FUNCTIONS:
            limit = 2000 if fc in (codec.FC_READ_COILS, codec.FC_READ_DISCRETE) else 125
            if not 1 <= req.quantity <= limit:
                return codec.build_exception(fc, codec.EXC_ILLEGAL_VALUE)
            if not store.in_range(req.table, req.address, req.quantity):
                return codec.build_exception(fc, codec.EXC_ILLEGAL_ADDRESS)
            values = store.read(req.table, req.address, req.quantity)
            if fc in (codec.FC_READ_COILS, codec.FC_READ_DISCRETE):
                data = codec.bits_to_bytes(values)
            else:
                data = codec.regs_to_bytes(values)
            return bytes([fc, len(data)]) + data

        if fc in codec.WRITE_FUNCTIONS:
            if fc == codec.FC_WRITE_COILS and not 1 <= req.quantity <= 1968:
                return codec.build_exception(fc, codec.EXC_ILLEGAL_VALUE)
            if fc == codec.FC_WRITE_REGISTERS and not 1 <= req.quantity <= 123:
                return codec.build_exception(fc, codec.EXC_ILLEGAL_VALUE)
            if not store.in_range(req.table, req.address, req.quantity):
                return codec.build_exception(fc, codec.EXC_ILLEGAL_ADDRESS)
            if store.write_guard:
                code = store.write_guard(req.table, req.address, req.values)
                if code:
                    return codec.build_exception(fc, code)
            store.write(req.table, req.address, req.values)
            if fc in (codec.FC_WRITE_COIL, codec.FC_WRITE_REGISTER):
                return req.raw[:5]                       # echo of the request
            return struct.pack(">BHH", fc, req.address, req.quantity)

        if fc == codec.FC_ENCAP:
            if req.mei_type != codec.MEI_DEVICE_ID or req.dev_id_code is None:
                return codec.build_exception(fc, codec.EXC_ILLEGAL_FUNCTION)
            return self.identity.encode(req.dev_id_code, req.object_id or 0)

        return codec.build_exception(fc, codec.EXC_ILLEGAL_FUNCTION)
