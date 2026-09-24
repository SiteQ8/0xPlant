import os
import tempfile

import pytest

from modbuslite import codec
from oxplant import config as oxconfig
from oxplant.policy import ConduitPolicy, SourceRule, parse_ranges

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def policy():
    return ConduitPolicy("C1", "PLC-X", [
        SourceRule("10.0.0.5", "HMI", allow=["read"], writes={"holding": parse_ranges(["100-119"]), "coils": parse_ranges("0-7")}),
        SourceRule("10.0.1.0/24", "EWS", allow=["read", "write", "identification"], functions=[8]),
        SourceRule("10.0.0.9", "MON", allow=["read"], max_rps=5),
    ], default="deny", max_rps=1000)


def test_parse_ranges():
    assert parse_ranges(["100-119", 7, "3"]) == [(100, 119), (7, 7), (3, 3)]
    assert parse_ranges(None) == []
    with pytest.raises(ValueError):
        parse_ranges("20-10")


def test_policy_decisions():
    p = policy()
    r = codec.parse_pdu
    assert p.evaluate("10.0.0.5", r(codec.encode_read(3, 0, 10))).allowed
    d = p.evaluate("10.0.0.5", r(codec.encode_write_register(100, 1)))
    assert d.allowed and d.source_asset == "HMI"
    d = p.evaluate("10.0.0.5", r(codec.encode_write_registers(110, [1] * 11)))
    assert not d.allowed and d.rule == "OXP-002" and d.exception_code == codec.EXC_ILLEGAL_ADDRESS   # 110..120 crosses the range end
    d = p.evaluate("10.0.0.5", r(codec.encode_write_registers(110, [1] * 10)))
    assert d.allowed                                                                                  # 110..119 is exactly inside
    d = p.evaluate("10.0.0.5", r(codec.encode_write_register(200, 1)))
    assert not d.allowed and d.rule == "OXP-002"
    d = p.evaluate("10.0.0.5", r(codec.encode_device_id()))
    assert not d.allowed and d.rule == "OXP-001"
    d = p.evaluate("10.0.0.5", r(bytes([0x08, 0, 0, 0, 0])))
    assert not d.allowed and d.rule == "OXP-001"
    assert p.evaluate("10.0.1.77", r(codec.encode_write_register(200, 1))).allowed
    assert p.evaluate("10.0.1.77", r(codec.encode_device_id())).allowed
    assert p.evaluate("10.0.1.77", r(bytes([0x08, 0, 0, 0, 0]))).allowed          # explicit function
    d = p.evaluate("192.168.1.1", r(codec.encode_read(3, 0, 1)))
    assert not d.allowed and d.rule == "OXP-003"


def test_rate_limit_per_source():
    p = policy()
    req = codec.parse_pdu(codec.encode_read(4, 0, 1))
    results = [p.evaluate("10.0.0.9", req) for _ in range(8)]
    assert all(r.allowed for r in results[:5])
    assert not results[5].allowed and results[5].rule == "OXP-004" and results[5].exception_code == codec.EXC_DEVICE_BUSY
    assert p.evaluate("10.0.0.5", req).allowed            # other sources unaffected


def test_default_allow_when_configured():
    p = ConduitPolicy("C", "A", [], default="allow")
    assert p.evaluate("1.2.3.4", codec.parse_pdu(codec.encode_read(3, 0, 1))).allowed


def test_local_config_loads_and_validates():
    cfg = oxconfig.load(os.path.join(ROOT, "config", "oxplant.local.yaml"))
    assert oxconfig.validate(cfg) == []
    assert len(cfg.conduits) == 3 and cfg.asset("PLC-002").zone == "L1"
    assert cfg.zone_for_ip("127.0.1.55") == "L1" and cfg.zone_for_ip("8.8.8.8") == ""
    c = cfg.conduits[1]
    hmi = c.policy.find("127.0.3.30")
    assert hmi.asset == "HMI-001" and hmi.writes["holding"] == [(100, 119)]


def test_docker_config_loads_with_env_token(monkeypatch):
    monkeypatch.setenv("OXPLANT_SENSOR_TOKEN", "secret-token")
    cfg = oxconfig.load(os.path.join(ROOT, "config", "oxplant.docker.yaml"))
    assert cfg.console.sensor_token == "secret-token"
    assert [s.name for s in cfg.sensors] == ["SENSOR-001", "SENSOR-002", "SENSOR-003"]
    assert oxconfig.validate(cfg) == []


def test_validation_warns_on_weak_configuration():
    text = """
console: {sensor_token: "", users: []}
assets:
  - {id: A, name: a, type: PLC, ip: 10.0.0.1}
conduits:
  - id: C
    asset: NOPE
    listen: {host: 127.0.0.1, port: 1}
    upstream: {host: 127.0.0.1, port: 2}
    default: allow
    rules:
      - {source: 10.0.0.2, allow: [read, write], writes: {holding: ["1-2"]}}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
    cfg = oxconfig.load(fh.name)
    problems = oxconfig.validate(cfg)
    assert any("unknown asset" in p for p in problems)
    assert any("default allow" in p for p in problems)
    assert any("ranges are ignored" in p for p in problems)
    assert any("sensor_token" in p for p in problems)
    assert any("no users" in p for p in problems)


def test_explicit_write_functions_do_not_bypass_write_ranges():
    p = ConduitPolicy("C", "A", [SourceRule("10.0.0.5", "HMI", allow=["read"], functions=[16, 22, 23], writes={"holding": parse_ranges("100-119")})])
    r = codec.parse_pdu
    assert p.evaluate("10.0.0.5", r(codec.encode_write_registers(100, [1, 2]))).allowed
    d = p.evaluate("10.0.0.5", r(codec.encode_write_registers(200, [1, 2])))
    assert not d.allowed and d.rule == "OXP-002"
    d = p.evaluate("10.0.0.5", r(bytes([0x16, 0, 0, 0, 0, 0, 0])))        # mask write: address not decoded -> needs full write grant
    assert not d.allowed and d.rule == "OXP-001"
    full = ConduitPolicy("C", "A", [SourceRule("10.0.0.6", "EWS", allow=["read", "write"], functions=[22])])
    assert full.evaluate("10.0.0.6", r(bytes([0x16, 0, 0, 0, 0, 0, 0]))).allowed
