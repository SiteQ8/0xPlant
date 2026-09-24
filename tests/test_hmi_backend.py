import time

from plant.hmi import AlarmManager, Historian, hash_password, verify_password
from plant.programs import Treatment
from plant.registers import TagMap, status_tags


def test_alarm_definitions_come_from_tag_descriptions():
    defs = AlarmManager.definitions(TagMap(list(Treatment.tags) + status_tags()))
    names = {d[0]: d[2] for d in defs}
    assert names["AAHH_TRIP"] == "trip" and names["AALL_ALARM"] == "alarm" and "DOSING_RUNNING" not in names


def test_alarm_lifecycle_isa_18_2():
    am = AlarmManager()
    defs = [("AAHH_TRIP", "Chlorine high-high", "trip")]
    am.update("PLC-002", defs, {"AAHH_TRIP": 1}, True, "")
    snap = am.snapshot()
    assert snap["unacked"] == 1 and snap["active"] == 1 and snap["alarms"][0]["class"] == "trip"
    am.update("PLC-002", defs, {"AAHH_TRIP": 1}, True, "")                 # still active: no new transition
    assert am.snapshot()["alarms"][0]["count"] == 1
    am.update("PLC-002", defs, {"AAHH_TRIP": 0}, True, "")                 # cleared but unacknowledged stays listed
    snap = am.snapshot()
    assert len(snap["alarms"]) == 1 and not snap["alarms"][0]["active"] and snap["unacked"] == 1
    assert am.ack("PLC-002", "AAHH_TRIP", "operator") == 1
    assert am.snapshot()["alarms"] == []                                   # acknowledged + cleared: gone
    assert [h["event"] for h in am.snapshot()["history"]] == ["ACK by operator", "CLEARED", "ACTIVE"]
    am.update("PLC-002", defs, {"AAHH_TRIP": 1}, True, "")
    am.ack(None, None, "sup")                                              # acknowledge everything while active
    a = am.snapshot()["alarms"][0]
    assert a["active"] and a["acked"] and a["ack_by"] == "sup"
    am.update("PLC-003", [], {}, False, "timeout")                         # communication loss is an alarm too
    assert any(x["tag"] == "COMM" and x["plc"] == "PLC-003" for x in am.snapshot()["alarms"])


def test_historian_query_window():
    h = Historian(seconds=100)
    h.record("PLC-001", {"LT-101": 50.0, "FT-101": 120.0})
    h._last["PLC-001"] = 0
    h.record("PLC-001", {"LT-101": 51.0, "FT-101": 121.0})
    q = h.query("PLC-001", ["LT-101", "NOPE"], 60)
    assert [p[1] for p in q["LT-101"]] == [50.0, 51.0] and q["NOPE"] == []
    assert h.query("PLC-009", ["LT-101"], 60) == {"LT-101": []}


def test_hmi_password_hashing():
    h = hash_password("Operator@2025", 1000)
    assert verify_password("Operator@2025", h) and not verify_password("x", h)
