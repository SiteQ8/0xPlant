import http.client
import json
import os
import tempfile
import threading

import pytest

from oxplant import config as oxconfig
from oxplant.auth import hash_password
from oxplant.console import Console
from oxplant.store import Store
from tests.conftest import free_port

TOKEN = "unit-test-sensor-token"


@pytest.fixture(scope="module")
def console():
    port = free_port()
    text = f"""
site: Test Site
console:
  listen: 127.0.0.1
  port: {port}
  sensor_token: "{TOKEN}"
  users:
    - {{username: admin, role: admin, password_hash: "{hash_password('pw', 1000)}"}}
    - {{username: viewer, role: viewer, password_hash: "{hash_password('pw', 1000)}"}}
    - {{username: soc, role: soc, password_hash: "{hash_password('pw', 1000)}"}}
zones:
  - {{id: L1, level: 1, name: "Level 1", subnets: ["10.0.1.0/24"]}}
assets:
  - {{id: PLC-1, name: Intake, type: PLC, ip: 10.0.1.10, zone: L1, ports: [502]}}
conduits:
  - id: C1
    asset: PLC-1
    listen: {{host: 127.0.0.1, port: 1}}
    upstream: {{host: 10.0.1.10, port: 502}}
    rules:
      - {{source: 10.0.3.30, asset: HMI, allow: [read], writes: {{holding: ["100-119"]}}}}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
    cfg = oxconfig.load(fh.name)
    c = Console(cfg, store=Store(":memory:"))
    httpd = c.make_httpd()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    c.port = port
    yield c
    httpd.shutdown()
    os.unlink(fh.name)


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def call(self, method, path, body=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        resp = conn.getresponse()
        data = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        result = (resp.status, data if raw else json.loads(data or b"{}"), dict(resp.getheaders()))
        conn.close()
        return result

    def login(self, user, pw="pw"):
        return self.call("POST", "/api/login", {"username": user, "password": pw})


def test_static_and_health(console):
    c = Client(console.port)
    status, body, headers = c.call("GET", "/", raw=True)
    assert status == 200 and b"0xPlant" in body and headers["X-Frame-Options"] == "DENY" and "script-src 'self'" in headers["Content-Security-Policy"]
    status, body, _ = c.call("GET", "/app.js", raw=True)
    assert status == 200 and b"use strict" in body
    assert c.call("GET", "/api/health")[1]["ok"] is True
    assert c.call("GET", "/api/summary")[0] == 401


def test_login_lockout_csrf_and_rbac(console):
    c = Client(console.port)
    for _ in range(5):
        assert c.login("admin", "wrong")[0] == 401
    status, body, _ = c.login("admin")
    assert status == 401 and body["error"].startswith("locked")
    assert any(e["rule"] == "OXP-014" for e in console.store.list_events())
    console.auth.failures.clear()
    status, body, headers = c.login("admin")
    assert status == 200 and body["role"] == "admin" and "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert c.call("GET", "/api/me")[1]["username"] == "admin"
    assert c.call("GET", "/api/summary")[1]["site"] == "Test Site"
    # CSRF: missing marker header, or a foreign origin, is refused
    assert c.call("POST", "/api/logout", {}, headers={"X-Requested-With": ""})[0] == 403
    assert c.call("POST", "/api/logout", {}, headers={"Origin": "http://evil.example"})[0] == 403
    # RBAC: viewer may read but not act
    v = Client(console.port)
    assert v.login("viewer")[0] == 200
    assert v.call("GET", "/api/alerts")[0] == 200
    assert v.call("POST", "/api/alerts/1/ack", {})[0] == 403
    assert any(a["action"] == "denied" for a in console.store.list_audit())
    assert c.call("POST", "/api/logout", {})[0] == 200
    assert c.call("GET", "/api/summary")[0] == 401


def test_ingest_alerts_changes_and_assets(console):
    s = Client(console.port)
    assert s.call("POST", "/api/ingest", {"sensor": "S1", "events": []}, headers={"Authorization": "Bearer nope"})[0] == 401
    payload = {"sensor": "S1", "events": [
        {"title": "blocked write", "severity": "critical", "category": "SECURITY", "rule": "OXP-002", "source_ip": "10.0.3.30",
         "asset": "PLC-1", "alert_key": "OXP-002:PLC-1:10.0.3.30", "detail": {"address": 200}},
        {"title": "junk", "severity": "info", "not_a_field": 1}],
        "flows": [{"conduit": "C1", "source_ip": "10.0.3.30", "source_asset": "HMI", "asset": "PLC-1", "requests": 10, "denied": 1, "functions": {"Read Coils": 10}}],
        "assets": [{"id": "UNK-10.0.1.99", "ip": "10.0.1.99", "name": "Unknown", "type": "Unknown", "approved": False, "status": "online"}]}
    status, body, _ = s.call("POST", "/api/ingest", payload, headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200 and body == {"events": 2, "flows": 1, "assets": 1, "rejected": 0}
    # poison records are counted and skipped, never fail the batch or crash the handler
    bad = {"sensor": "S1", "events": [{"title": 5}, "x", {"title": "ok", "detail": "notadict"}],
           "flows": [{"requests": "abc"}], "assets": [{"id": {"a": 1}}, {"id": "P2", "ports": ["abc"]}]}
    status, body, _ = s.call("POST", "/api/ingest", bad, headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200 and body == {"events": 0, "flows": 0, "assets": 1, "rejected": 5}
    assert s.call("POST", "/api/ingest", [1, 2], headers={"Authorization": f"Bearer {TOKEN}"})[0] == 400
    assert s.call("POST", "/api/login", [1])[0] == 400
    c = Client(console.port)
    c.login("admin")
    summary = c.call("GET", "/api/summary")[1]
    assert summary["alerts"]["critical"] == 1 and summary["assets"]["rogue"] == 1 and summary["conduits"][0]["denied"] == 1
    assert [x["name"] for x in summary["sensors"]] == ["S1"]
    alerts = c.call("GET", "/api/alerts?status=open")[1]
    aid = next(a["id"] for a in alerts if a["rule"] == "OXP-002")
    soc = Client(console.port)
    soc.login("soc")
    assert soc.call("POST", f"/api/alerts/{aid}/ack", {})[1]["ok"] is True
    assert next(a for a in c.call("GET", "/api/alerts?status=acknowledged")[1] if a["id"] == aid)["ack_by"] == "soc"
    assert soc.call("POST", f"/api/alerts/{aid}/resolve", {})[1]["ok"] is True
    assert not [a for a in c.call("GET", "/api/alerts?status=open")[1] if a["rule"] == "OXP-002"]
    assert c.call("GET", "/api/protocols")[1][0]["protocol"] == "Modbus/TCP"
    assert c.call("GET", "/api/flows")[1][0]["source_asset"] == "HMI"
    assert c.call("GET", "/api/policy")[1][0]["rules"][0]["writes"] == {"holding": ["100-119"]}
    # change approval sets the golden value
    cid = console.store.add_change("PLC-1", "LAHH_LIMIT", "config", 95.0, 90.0)
    assert c.call("POST", f"/api/changes/{cid}/approve", {"ticket": "CAB-7"})[1]["ok"] is True
    assert console.store.golden("PLC-1", "LAHH_LIMIT") == 90.0
    assert c.call("GET", "/api/changes")[1][0]["ticket"] == "CAB-7"
    assert c.call("POST", "/api/changes/999/approve", {})[0] == 404
    # asset approval clears the rogue alert
    console.store.add_event(__import__("oxplant.events", fromlist=["Event"]).Event.from_rule("OXP-009", "rogue", source_ip="10.0.1.99", alert_key="OXP-009:10.0.1.99"))
    assert c.call("POST", "/api/assets/UNK-10.0.1.99/approve", {})[1]["ok"] is True
    assert console.store.get_asset("UNK-10.0.1.99")["approved"] and not [a for a in console.store.list_alerts("open") if a["rule"] == "OXP-009"]
    settings = c.call("GET", "/api/settings")[1]
    assert [u["username"] for u in settings["users"]] == ["admin", "viewer", "soc"] and "password_hash" not in json.dumps(settings)
    assert len(c.call("GET", "/api/rules")[1]) >= 16
    assert c.call("GET", "/api/audit")[1]["total"] >= 5
    assert c.call("GET", "/api/assets")[0] == 200                      # the asset with a bad port did not poison listings
    assert c.call("POST", "/api/alerts/abc/ack", {})[0] == 400
    assert c.call("GET", "/api/events?limit=abc")[0] == 200
    assert c.call("POST", f"/api/alerts/{aid}/ack", {})[1]["ok"] is False   # already resolved: a real no-op now


def test_metrics_and_csv_export(console):
    c = Client(console.port)
    status, body, headers = c.call("GET", "/metrics", raw=True)
    assert status == 200 and headers["Content-Type"].startswith("text/plain") and b"oxplant_alerts_open{severity=\"critical\"}" in body
    assert c.call("GET", "/api/export/events.csv")[0] == 401
    c.login("admin")
    status, body, headers = c.call("GET", "/api/export/events.csv", raw=True)
    assert status == 200 and headers["Content-Type"].startswith("text/csv") and body.splitlines()[0].startswith(b"id,ts,severity")
    assert c.call("GET", "/api/export/nope.csv")[0] == 404
    assert any(a["action"] == "export" for a in console.store.list_audit())
