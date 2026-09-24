"""modbuslite - a small, dependency-free Modbus/TCP implementation.

Used by the 0xPlant lab for the simulated PLCs, the HMI and the protective
conduit gateway. It implements the public function codes needed on a plant
floor: coils, discrete inputs, holding and input registers, single and
multiple writes, and Read Device Identification (FC 43 / MEI 14).
"""
from .codec import (  # noqa: F401
    FC_READ_COILS, FC_READ_DISCRETE, FC_READ_HOLDING, FC_READ_INPUT,
    FC_WRITE_COIL, FC_WRITE_REGISTER, FC_WRITE_COILS, FC_WRITE_REGISTERS,
    FC_ENCAP, MEI_DEVICE_ID,
    EXC_ILLEGAL_FUNCTION, EXC_ILLEGAL_ADDRESS, EXC_ILLEGAL_VALUE,
    EXC_DEVICE_FAILURE, EXC_DEVICE_BUSY, EXC_GATEWAY_PATH, EXC_GATEWAY_TARGET,
    READ_FUNCTIONS, WRITE_FUNCTIONS, FUNCTION_NAMES, EXCEPTION_NAMES,
    DEVICE_ID_OBJECTS,
    ModbusError, DecodeError, ModbusException,
    Request, parse_pdu, build_mbap, build_exception, read_frame,
    bits_to_bytes, bytes_to_bits, regs_to_bytes, bytes_to_regs,
    to_int16, from_int16,
)
from .server import DataStore, DeviceIdentity, ModbusServer  # noqa: F401
from .client import ModbusClient  # noqa: F401
