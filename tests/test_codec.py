import pytest

from modbuslite import codec
from modbuslite.codec import DecodeError, parse_pdu


def test_read_request_roundtrip():
    pdu = codec.encode_read(codec.FC_READ_HOLDING, 100, 10)
    req = parse_pdu(pdu)
    assert (req.function, req.address, req.quantity, req.table, req.is_read) == (3, 100, 10, "holding", True)
    assert req.end_address == 109


def test_write_requests_decode_values():
    req = parse_pdu(codec.encode_write_registers(200, [1, 2, 65535]))
    assert req.values == [1, 2, 65535] and req.quantity == 3 and req.is_write and req.table == "holding"
    req = parse_pdu(codec.encode_write_coils(8, [1, 0, 1, 1, 0, 0, 0, 0, 1]))
    assert req.values == [1, 0, 1, 1, 0, 0, 0, 0, 1] and req.table == "coils"
    req = parse_pdu(codec.encode_write_coil(3, True))
    assert req.values == [1] and req.quantity == 1
    req = parse_pdu(codec.encode_write_register(7, 0xBEEF))
    assert req.values == [0xBEEF]


def test_malformed_pdus_raise():
    with pytest.raises(DecodeError):
        parse_pdu(b"")
    with pytest.raises(DecodeError):
        parse_pdu(b"\x03\x00\x01")                       # truncated read
    with pytest.raises(DecodeError):
        parse_pdu(b"\x05\x00\x01\x12\x34")               # bad coil value
    with pytest.raises(DecodeError):
        parse_pdu(b"\x10\x00\x00\x00\x02\x03\x00\x01\x00")  # byte count mismatch


def test_unknown_function_is_kept_for_policy():
    req = parse_pdu(bytes([0x08, 0x00, 0x00, 0x00, 0x00]))
    assert req.function == 8 and not req.is_read and not req.is_write and req.table is None


def test_bits_and_registers_helpers():
    assert codec.bits_to_bytes([1, 0, 1, 1, 0, 0, 0, 0, 1]) == b"\x0d\x01"
    assert codec.bytes_to_bits(b"\x0d\x01", 9) == [1, 0, 1, 1, 0, 0, 0, 0, 1]
    assert codec.bytes_to_regs(codec.regs_to_bytes([1, 65535])) == [1, 65535]
    assert codec.from_int16(codec.to_int16(-5)) == -5


def test_mbap_and_exception_frames():
    frame = codec.build_mbap(0x1234, 7, b"\x03\x02\x00\x01")
    assert frame[:7] == b"\x12\x34\x00\x00\x00\x05\x07"
    assert codec.build_exception(0x03, codec.EXC_ILLEGAL_ADDRESS) == b"\x83\x02"


def test_device_identification_roundtrip():
    from modbuslite.server import DeviceIdentity
    ident = DeviceIdentity(vendor_name="Acme", product_code="X1", revision="2.0", model_name="M")
    pdu = ident.encode(0x02, 0x00)
    objs = codec.decode_device_id(pdu)
    assert objs["VendorName"] == "Acme" and objs["ModelName"] == "M" and objs["MajorMinorRevision"] == "2.0"
    basic = codec.decode_device_id(ident.encode(0x01, 0x00))
    assert set(basic) == {"VendorName", "ProductCode", "MajorMinorRevision"}
