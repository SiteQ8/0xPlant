"""Modbus/TCP framing and PDU codec."""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# --- function codes -------------------------------------------------------
FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_COIL = 0x05
FC_WRITE_REGISTER = 0x06
FC_WRITE_COILS = 0x0F
FC_WRITE_REGISTERS = 0x10
FC_MASK_WRITE = 0x16     # Mask Write Register (address not decoded here; policy treats as write)
FC_READ_WRITE_REGISTERS = 0x17
FC_ENCAP = 0x2B          # Encapsulated Interface Transport
MEI_DEVICE_ID = 0x0E     # Read Device Identification

READ_FUNCTIONS = frozenset({FC_READ_COILS, FC_READ_DISCRETE, FC_READ_HOLDING, FC_READ_INPUT})
WRITE_FUNCTIONS = frozenset({FC_WRITE_COIL, FC_WRITE_REGISTER, FC_WRITE_COILS, FC_WRITE_REGISTERS})

FUNCTION_NAMES = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x07: "Read Exception Status",
    0x08: "Diagnostics",
    0x0B: "Get Comm Event Counter",
    0x0C: "Get Comm Event Log",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
    0x11: "Report Server ID",
    0x14: "Read File Record",
    0x15: "Write File Record",
    0x16: "Mask Write Register",
    0x17: "Read/Write Multiple Registers",
    0x18: "Read FIFO Queue",
    0x2B: "Encapsulated Interface Transport",
}

# --- exception codes ------------------------------------------------------
EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02
EXC_ILLEGAL_VALUE = 0x03
EXC_DEVICE_FAILURE = 0x04
EXC_DEVICE_BUSY = 0x06
EXC_GATEWAY_PATH = 0x0A
EXC_GATEWAY_TARGET = 0x0B

EXCEPTION_NAMES = {
    0x01: "Illegal Function",
    0x02: "Illegal Data Address",
    0x03: "Illegal Data Value",
    0x04: "Server Device Failure",
    0x05: "Acknowledge",
    0x06: "Server Device Busy",
    0x08: "Memory Parity Error",
    0x0A: "Gateway Path Unavailable",
    0x0B: "Gateway Target Device Failed To Respond",
}

DEVICE_ID_OBJECTS = {
    0x00: "VendorName",
    0x01: "ProductCode",
    0x02: "MajorMinorRevision",
    0x03: "VendorUrl",
    0x04: "ProductName",
    0x05: "ModelName",
    0x06: "UserApplicationName",
}

MBAP_LEN = 7
MAX_PDU = 253


class ModbusError(Exception):
    """Base class for modbuslite errors."""


class DecodeError(ModbusError):
    """The frame or PDU could not be decoded."""


class ModbusException(ModbusError):
    """The remote device answered with a Modbus exception response."""

    def __init__(self, function: int, code: int):
        self.function = function
        self.code = code
        super().__init__(
            f"{FUNCTION_NAMES.get(function, hex(function))}: "
            f"{EXCEPTION_NAMES.get(code, hex(code))} (0x{code:02X})"
        )


@dataclass
class Request:
    """A decoded Modbus request PDU."""

    function: int
    address: int = 0
    quantity: int = 0
    values: List[int] = field(default_factory=list)   # register values or coil bits
    mei_type: Optional[int] = None
    dev_id_code: Optional[int] = None
    object_id: Optional[int] = None
    raw: bytes = b""

    @property
    def name(self) -> str:
        return FUNCTION_NAMES.get(self.function, f"Function 0x{self.function:02X}")

    @property
    def is_read(self) -> bool:
        return self.function in READ_FUNCTIONS

    @property
    def is_write(self) -> bool:
        return self.function in WRITE_FUNCTIONS

    @property
    def table(self) -> Optional[str]:
        """Data table this request touches: coils, discrete, holding or input."""
        if self.function in (FC_READ_COILS, FC_WRITE_COIL, FC_WRITE_COILS):
            return "coils"
        if self.function == FC_READ_DISCRETE:
            return "discrete"
        if self.function in (FC_READ_HOLDING, FC_WRITE_REGISTER, FC_WRITE_REGISTERS):
            return "holding"
        if self.function == FC_READ_INPUT:
            return "input"
        return None

    @property
    def end_address(self) -> int:
        """Last address touched (inclusive)."""
        return self.address + max(self.quantity, 1) - 1


# --- helpers --------------------------------------------------------------
def bits_to_bytes(bits: List[int]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def bytes_to_bits(data: bytes, count: int) -> List[int]:
    return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]


def regs_to_bytes(regs: List[int]) -> bytes:
    return struct.pack(f">{len(regs)}H", *[r & 0xFFFF for r in regs])


def bytes_to_regs(data: bytes) -> List[int]:
    return list(struct.unpack(f">{len(data) // 2}H", data[: len(data) // 2 * 2]))


def to_int16(value: int) -> int:
    """Encode a signed integer into an unsigned 16-bit register value."""
    value = max(-32768, min(32767, int(value)))
    return value & 0xFFFF


def from_int16(reg: int) -> int:
    """Decode an unsigned 16-bit register value into a signed integer."""
    reg &= 0xFFFF
    return reg - 0x10000 if reg & 0x8000 else reg


# --- PDU parsing ----------------------------------------------------------
def parse_pdu(pdu: bytes) -> Request:
    """Decode a request PDU into a Request. Raises DecodeError on malformed input."""
    if not pdu:
        raise DecodeError("empty PDU")
    fc = pdu[0]
    body = pdu[1:]
    req = Request(function=fc, raw=pdu)
    try:
        if fc in (FC_READ_COILS, FC_READ_DISCRETE, FC_READ_HOLDING, FC_READ_INPUT):
            req.address, req.quantity = struct.unpack(">HH", body[:4])
            if len(body) != 4:
                raise DecodeError("bad length for read request")
        elif fc == FC_WRITE_COIL:
            req.address, value = struct.unpack(">HH", body[:4])
            if len(body) != 4:
                raise DecodeError("bad length for write coil")
            if value not in (0x0000, 0xFF00):
                raise DecodeError("bad coil value")
            req.quantity = 1
            req.values = [1 if value == 0xFF00 else 0]
        elif fc == FC_WRITE_REGISTER:
            req.address, value = struct.unpack(">HH", body[:4])
            if len(body) != 4:
                raise DecodeError("bad length for write register")
            req.quantity = 1
            req.values = [value]
        elif fc == FC_WRITE_COILS:
            req.address, req.quantity, count = struct.unpack(">HHB", body[:5])
            data = body[5:]
            if len(data) != count or count != (req.quantity + 7) // 8:
                raise DecodeError("bad byte count for write coils")
            req.values = bytes_to_bits(data, req.quantity)
        elif fc == FC_WRITE_REGISTERS:
            req.address, req.quantity, count = struct.unpack(">HHB", body[:5])
            data = body[5:]
            if len(data) != count or count != req.quantity * 2:
                raise DecodeError("bad byte count for write registers")
            req.values = bytes_to_regs(data)
        elif fc == FC_ENCAP:
            if len(body) < 1:
                raise DecodeError("missing MEI type")
            req.mei_type = body[0]
            if req.mei_type == MEI_DEVICE_ID:
                if len(body) != 3:
                    raise DecodeError("bad length for device identification")
                req.dev_id_code, req.object_id = body[1], body[2]
        else:
            # Unknown or unsupported function: keep the raw bytes for policy logging.
            pass
    except struct.error as exc:
        raise DecodeError(f"truncated PDU for function 0x{fc:02X}") from exc
    return req


def build_mbap(transaction: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack(">HHHB", transaction & 0xFFFF, 0, len(pdu) + 1, unit & 0xFF) + pdu


def build_exception(function: int, code: int) -> bytes:
    return bytes([(function | 0x80) & 0xFF, code & 0xFF])


async def read_frame(reader: asyncio.StreamReader) -> Tuple[int, int, bytes]:
    """Read one MBAP frame; returns (transaction id, unit id, pdu).

    Raises asyncio.IncompleteReadError on a closed connection and DecodeError
    on a frame that is not Modbus/TCP.
    """
    header = await reader.readexactly(MBAP_LEN)
    transaction, protocol, length, unit = struct.unpack(">HHHB", header)
    if protocol != 0:
        raise DecodeError(f"unexpected protocol id {protocol}")
    if length < 2 or length > MAX_PDU + 1:
        raise DecodeError(f"invalid MBAP length {length}")
    pdu = await reader.readexactly(length - 1)
    return transaction, unit, pdu


# --- request encoders (client side) ---------------------------------------
def encode_read(function: int, address: int, quantity: int) -> bytes:
    return struct.pack(">BHH", function, address, quantity)


def encode_write_coil(address: int, value: bool) -> bytes:
    return struct.pack(">BHH", FC_WRITE_COIL, address, 0xFF00 if value else 0x0000)


def encode_write_register(address: int, value: int) -> bytes:
    return struct.pack(">BHH", FC_WRITE_REGISTER, address, value & 0xFFFF)


def encode_write_coils(address: int, values: List[int]) -> bytes:
    data = bits_to_bytes(values)
    return struct.pack(">BHHB", FC_WRITE_COILS, address, len(values), len(data)) + data


def encode_write_registers(address: int, values: List[int]) -> bytes:
    data = regs_to_bytes(values)
    return struct.pack(">BHHB", FC_WRITE_REGISTERS, address, len(values), len(data)) + data


def encode_device_id(code: int = 0x01, object_id: int = 0x00) -> bytes:
    return bytes([FC_ENCAP, MEI_DEVICE_ID, code, object_id])


def decode_device_id(pdu: bytes) -> dict:
    """Decode a Read Device Identification response PDU into {object name: value}."""
    if len(pdu) < 7 or pdu[0] != FC_ENCAP or pdu[1] != MEI_DEVICE_ID:
        raise DecodeError("not a device identification response")
    count = pdu[6]
    objects = {}
    pos = 7
    for _ in range(count):
        if pos + 2 > len(pdu):
            raise DecodeError("truncated device identification object")
        oid, length = pdu[pos], pdu[pos + 1]
        value = pdu[pos + 2: pos + 2 + length]
        objects[DEVICE_ID_OBJECTS.get(oid, f"Object{oid:02X}")] = value.decode("ascii", "replace")
        pos += 2 + length
    return objects
