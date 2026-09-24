import time

from oxplant.events import Event
from oxplant.store import Store


def test_alert_dedupe_and_resolution():
    s = Store(":memory:")
    ev = Event.from_rule("OXP-002", "blocked", source_ip="1.2.3.4", asset="PLC-2", alert_key="k1")
    for _ in range(3):
        s.add_event(ev)
    alerts = s.list_alerts("open")
    assert len(alerts) == 1 and alerts[0]["count"] == 3 and alerts[0]["status"] == "active"
    assert s.set_alert_status(alerts[0]["id"], "acknowledged", "soc")
    assert s.list_alerts("open")[0]["ack_by"] == "soc"
    s.add_event(Event("gone", resolve_key="k1"))
    assert s.list_alerts("open") == [] and s.alert_counts()["resolved_7d"] == 1
    s.add_event(ev)                                     # a new occurrence opens a fresh alert
    assert len(s.list_alerts("open")) == 1 and s.list_alerts("open")[0]["count"] == 1
    assert len(s.list_events()) == 5
    assert s.event_counts(0)["critical"] == 4


def test_changes_and_golden_values():
    s = Store(":memory:")
    cid = s.add_change("PLC-2", "AAHH_LIMIT", "config", 4.0, 3.5)
    assert s.get_change(cid)["status"] == "unreviewed"
    assert s.review_change(cid, "approved", "engineer", "CAB-1")
    c = s.get_change(cid)
    assert c["status"] == "approved" and c["ticket"] == "CAB-1" and c["reviewed_by"] == "engineer"
    assert s.golden("PLC-2", "AAHH_LIMIT") is None
    s.set_golden("PLC-2", "AAHH_LIMIT", 3.5)
    assert s.golden("PLC-2", "AAHH_LIMIT") == 3.5
    assert s.list_changes(status="approved")[0]["id"] == cid


def test_asset_merge_flows_audit_and_purge():
    s = Store(":memory:", retention_days=0)
    s.upsert_asset({"id": "PLC-1", "name": "Intake", "type": "PLC", "ip": "10.0.0.1", "ports": [502], "protocols": ["Modbus/TCP"], "status": "unknown"})
    s.upsert_asset({"id": "PLC-1", "status": "online", "vendor": "Acme", "firmware": "1.2"})
    a = s.get_asset("PLC-1")
    assert a["name"] == "Intake" and a["status"] == "online" and a["vendor"] == "Acme" and a["ports"] == [502] and a["approved"]
    s.upsert_asset({"id": "UNK-10.0.0.9", "ip": "10.0.0.9", "approved": False, "status": "online"})
    assert not s.get_asset("UNK-10.0.0.9")["approved"]
    assert s.approve_asset("UNK-10.0.0.9", True) and s.get_asset("UNK-10.0.0.9")["approved"]
    s.upsert_flow({"conduit": "C1", "source_ip": "10.0.0.5", "asset": "PLC-1", "requests": 5, "denied": 1, "functions": {"Read Coils": 5}})
    s.upsert_flow({"conduit": "C1", "source_ip": "10.0.0.5", "asset": "PLC-1", "requests": 9, "denied": 1, "functions": {"Read Coils": 9}})
    flows = s.list_flows()
    assert len(flows) == 1 and flows[0]["requests"] == 9 and flows[0]["functions"] == {"Read Coils": 9}
    s.audit("admin", "login", "console", "ok", "127.0.0.1")
    assert s.audit_count() == 1 and s.list_audit()[0]["user"] == "admin"
    s.add_event(Event("old", ts=time.time() - 10))
    assert s.purge() == 1 and s.list_events() == []
    s.sensor_seen("S1", 3)
    s.sensor_seen("S1", 2)
    assert s.list_sensors()[0]["events"] == 5


def test_updates_report_real_row_counts_after_inserts():
    s = Store(":memory:")
    s.add_event(Event("seed"))
    assert not s.set_alert_status(12345, "acknowledged", "soc")
    assert not s.approve_asset("nope", True)
    assert not s.review_change(999, "approved", "eng")
    assert s.resolve_alert_key("nokey", "x") == 0


def test_discovery_cannot_revert_an_operator_approval():
    s = Store(":memory:")
    s.upsert_asset({"id": "UNK-1", "ip": "10.0.0.9", "approved": False, "status": "online"})
    assert s.approve_asset("UNK-1", True)
    s.upsert_asset({"id": "UNK-1", "ip": "10.0.0.9", "approved": False, "status": "online"})   # next scan
    assert s.get_asset("UNK-1")["approved"]
    s.upsert_asset({"id": "BAD", "ip": "10.0.0.8", "ports": ["abc", 502, 70000], "protocols": "x"})
    assert s.get_asset("BAD")["ports"] == [502] and s.list_assets()
