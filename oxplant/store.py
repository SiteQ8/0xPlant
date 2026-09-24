"""SQLite persistence for the console: events, alerts, changes, assets, flows, audit."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from .events import Event, SEVERITY_ORDER

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL, severity TEXT, category TEXT, rule TEXT, title TEXT,
  source_ip TEXT, dest_ip TEXT, asset TEXT, protocol TEXT, sensor TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, key TEXT, first_ts REAL, last_ts REAL, count INTEGER, severity TEXT, rule TEXT,
  asset TEXT, source_ip TEXT, title TEXT, status TEXT, ack_by TEXT, ack_ts REAL, resolved_ts REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, status);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY, ts REAL, asset TEXT, tag TEXT, kind TEXT, old_value REAL, new_value REAL,
  source_ip TEXT, source_asset TEXT, status TEXT, ticket TEXT, reviewed_by TEXT, reviewed_ts REAL, note TEXT);
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY, name TEXT, type TEXT, ip TEXT, zone TEXT, protocols TEXT, ports TEXT, criticality TEXT,
  vendor TEXT, model TEXT, firmware TEXT, application TEXT, status TEXT, approved INTEGER,
  first_seen REAL, last_seen REAL, discovered_by TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL, user TEXT, action TEXT, target TEXT, result TEXT, ip TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS flows (
  key TEXT PRIMARY KEY, source_ip TEXT, source_asset TEXT, asset TEXT, conduit TEXT, protocol TEXT,
  first_seen REAL, last_seen REAL, requests INTEGER, denied INTEGER, functions TEXT, sensor TEXT);
CREATE TABLE IF NOT EXISTS identities (asset TEXT PRIMARY KEY, ts REAL, identity TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sensors (name TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, events INTEGER, detail TEXT);
"""


def _row(cur, row) -> dict:
    d = {desc[0]: row[i] for i, desc in enumerate(cur.description)}
    for k in ("detail", "functions", "identity"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    for k in ("protocols", "ports"):
        if k in d and isinstance(d[k], str):
            d[k] = [p for p in d[k].split(",") if p]
            if k == "ports":
                d[k] = [int(p) for p in d[k]]
    if "approved" in d:
        d["approved"] = bool(d["approved"])
    return d


class Store:
    def __init__(self, path: str = ":memory:", retention_days: int = 365):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args: tuple = ()) -> List[dict]:
        with self._lock:
            cur = self._db.execute(sql, args)
            return [_row(cur, r) for r in cur.fetchall()]

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    # --- events & alerts ---
    def add_event(self, ev: Event) -> int:
        eid = self._x(
            "INSERT INTO events (ts,severity,category,rule,title,source_ip,dest_ip,asset,protocol,sensor,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ev.ts, ev.severity, ev.category, ev.rule, ev.title, ev.source_ip, ev.dest_ip, ev.asset, ev.protocol, ev.sensor, json.dumps(ev.detail)))
        if ev.alert_key:
            self.upsert_alert(ev)
        if ev.resolve_key:
            self.resolve_alert_key(ev.resolve_key, "system")
        return eid

    def upsert_alert(self, ev: Event) -> None:
        with self._lock:
            rows = self._q("SELECT id, severity FROM alerts WHERE key=? AND status IN ('active','acknowledged') ORDER BY id DESC LIMIT 1", (ev.alert_key,))
            if rows:
                sev = ev.severity if SEVERITY_ORDER.get(ev.severity, 0) > SEVERITY_ORDER.get(rows[0]["severity"], 0) else rows[0]["severity"]
                self._x("UPDATE alerts SET last_ts=?, count=count+1, severity=?, title=?, detail=? WHERE id=?",
                        (ev.ts, sev, ev.title, json.dumps(ev.detail), rows[0]["id"]))
            else:
                self._x("INSERT INTO alerts (key,first_ts,last_ts,count,severity,rule,asset,source_ip,title,status,detail) VALUES (?,?,?,1,?,?,?,?,?,'active',?)",
                        (ev.alert_key, ev.ts, ev.ts, ev.severity, ev.rule, ev.asset, ev.source_ip, ev.title, json.dumps(ev.detail)))

    def resolve_alert_key(self, key: str, by: str) -> int:
        return self._x("UPDATE alerts SET status='resolved', resolved_ts=?, ack_by=COALESCE(ack_by, ?) WHERE key=? AND status IN ('active','acknowledged')",
                       (time.time(), by, key))

    def set_alert_status(self, alert_id: int, status: str, by: str) -> bool:
        if status == "acknowledged":
            n = self._x("UPDATE alerts SET status='acknowledged', ack_by=?, ack_ts=? WHERE id=? AND status='active'", (by, time.time(), alert_id))
        elif status == "resolved":
            n = self._x("UPDATE alerts SET status='resolved', resolved_ts=?, ack_by=COALESCE(ack_by, ?) WHERE id=? AND status IN ('active','acknowledged')", (time.time(), by, alert_id))
        else:
            return False
        return n > 0

    def list_events(self, limit: int = 200, severity: str = "", category: str = "", asset: str = "", rule: str = "", since: float = 0) -> List[dict]:
        sql, args = "SELECT * FROM events WHERE ts>=?", [since]
        for col, val in (("severity", severity), ("category", category), ("asset", asset), ("rule", rule)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(int(limit), 2000))
        return self._q(sql, tuple(args))

    def list_alerts(self, status: str = "", limit: int = 200) -> List[dict]:
        if status == "open":
            return self._q("SELECT * FROM alerts WHERE status IN ('active','acknowledged') ORDER BY last_ts DESC LIMIT ?", (limit,))
        if status:
            return self._q("SELECT * FROM alerts WHERE status=? ORDER BY last_ts DESC LIMIT ?", (status, limit))
        return self._q("SELECT * FROM alerts ORDER BY last_ts DESC LIMIT ?", (limit,))

    def alert_counts(self) -> Dict[str, int]:
        out = {"critical": 0, "warning": 0, "info": 0, "resolved_7d": 0}
        for r in self._q("SELECT severity, COUNT(*) n FROM alerts WHERE status IN ('active','acknowledged') GROUP BY severity"):
            out[r["severity"]] = r["n"]
        r = self._q("SELECT COUNT(*) n FROM alerts WHERE status='resolved' AND resolved_ts>=?", (time.time() - 7 * 86400,))
        out["resolved_7d"] = r[0]["n"] if r else 0
        return out

    def event_counts(self, since: float) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self._q("SELECT severity, COUNT(*) n FROM events WHERE ts>=? GROUP BY severity", (since,)):
            out[r["severity"]] = r["n"]
        out["total"] = sum(out.values())
        return out

    # --- changes ---
    def add_change(self, asset: str, tag: str, kind: str, old: Optional[float], new: Optional[float],
                   source_ip: str = "", source_asset: str = "", status: str = "unreviewed", note: str = "") -> int:
        return self._x("INSERT INTO changes (ts,asset,tag,kind,old_value,new_value,source_ip,source_asset,status,ticket,reviewed_by,reviewed_ts,note) VALUES (?,?,?,?,?,?,?,?,?,'',NULL,NULL,?)",
                       (time.time(), asset, tag, kind, old, new, source_ip, source_asset, status, note))

    def list_changes(self, limit: int = 200, status: str = "") -> List[dict]:
        if status:
            return self._q("SELECT * FROM changes WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
        return self._q("SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,))

    def get_change(self, change_id: int) -> Optional[dict]:
        rows = self._q("SELECT * FROM changes WHERE id=?", (change_id,))
        return rows[0] if rows else None

    def review_change(self, change_id: int, status: str, by: str, ticket: str = "", note: str = "") -> bool:
        n = self._x("UPDATE changes SET status=?, reviewed_by=?, reviewed_ts=?, ticket=?, note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
                    (status, by, time.time(), ticket, note, note, change_id))
        return n > 0

    def golden(self, asset: str, tag: str) -> Optional[float]:
        rows = self._q("SELECT v FROM kv WHERE k=?", (f"golden:{asset}:{tag}",))
        return float(rows[0]["v"]) if rows else None

    def set_golden(self, asset: str, tag: str, value: float) -> None:
        self._x("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (f"golden:{asset}:{tag}", str(value)))

    # --- assets ---
    def upsert_asset(self, rec: Dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            existing = self._q("SELECT * FROM assets WHERE id=?", (rec["id"],))
            merged = dict(existing[0]) if existing else {"first_seen": rec.get("first_seen", now), "approved": rec.get("approved", True)}
            for k, v in rec.items():
                if v not in (None, "", [], {}) or k in ("status",):
                    merged[k] = v
            merged.setdefault("last_seen", now)
            self._x("""INSERT OR REPLACE INTO assets (id,name,type,ip,zone,protocols,ports,criticality,vendor,model,firmware,application,status,approved,first_seen,last_seen,discovered_by,detail)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (merged["id"], merged.get("name", merged["id"]), merged.get("type", "Unknown"), merged.get("ip", ""), merged.get("zone", ""),
                     ",".join(merged.get("protocols", []) or []), ",".join(str(p) for p in (merged.get("ports", []) or [])),
                     merged.get("criticality", "medium"), merged.get("vendor", ""), merged.get("model", ""), merged.get("firmware", ""),
                     merged.get("application", ""), merged.get("status", "unknown"), 1 if merged.get("approved", True) else 0,
                     merged.get("first_seen", now), merged.get("last_seen", now), merged.get("discovered_by", ""),
                     json.dumps(merged.get("detail", {}))))

    def list_assets(self) -> List[dict]:
        return self._q("SELECT * FROM assets ORDER BY zone, id")

    def get_asset(self, asset_id: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM assets WHERE id=?", (asset_id,))
        return rows[0] if rows else None

    def set_asset_status(self, asset_id: str, status: str) -> None:
        self._x("UPDATE assets SET status=?, last_seen=CASE WHEN ?='online' THEN ? ELSE last_seen END WHERE id=?", (status, status, time.time(), asset_id))

    def approve_asset(self, asset_id: str, approved: bool) -> bool:
        return self._x("UPDATE assets SET approved=? WHERE id=?", (1 if approved else 0, asset_id)) > 0

    # --- flows ---
    def upsert_flow(self, rec: dict) -> None:
        key = f"{rec.get('conduit','')}:{rec.get('source_ip','')}:{rec.get('asset','')}"
        self._x("""INSERT INTO flows (key,source_ip,source_asset,asset,conduit,protocol,first_seen,last_seen,requests,denied,functions,sensor)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET source_asset=excluded.source_asset, last_seen=excluded.last_seen,
                   requests=excluded.requests, denied=excluded.denied, functions=excluded.functions, sensor=excluded.sensor""",
                (key, rec.get("source_ip", ""), rec.get("source_asset", ""), rec.get("asset", ""), rec.get("conduit", ""),
                 rec.get("protocol", "Modbus/TCP"), rec.get("first_seen", time.time()), rec.get("last_seen", time.time()),
                 int(rec.get("requests", 0)), int(rec.get("denied", 0)), json.dumps(rec.get("functions", {})), rec.get("sensor", "")))

    def list_flows(self) -> List[dict]:
        return self._q("SELECT * FROM flows ORDER BY last_seen DESC")

    # --- identities ---
    def get_identity(self, asset: str) -> Optional[dict]:
        rows = self._q("SELECT identity FROM identities WHERE asset=?", (asset,))
        return rows[0]["identity"] if rows else None

    def set_identity(self, asset: str, identity: dict) -> None:
        self._x("INSERT OR REPLACE INTO identities (asset, ts, identity) VALUES (?,?,?)", (asset, time.time(), json.dumps(identity)))

    # --- audit ---
    def audit(self, user: str, action: str, target: str = "", result: str = "ok", ip: str = "", detail: Optional[dict] = None) -> None:
        self._x("INSERT INTO audit (ts,user,action,target,result,ip,detail) VALUES (?,?,?,?,?,?,?)",
                (time.time(), user, action, target, result, ip, json.dumps(detail or {})))

    def list_audit(self, limit: int = 200) -> List[dict]:
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def audit_count(self) -> int:
        return self._q("SELECT COUNT(*) n FROM audit")[0]["n"]

    # --- sensors ---
    def sensor_seen(self, name: str, events: int, detail: Optional[dict] = None) -> None:
        now = time.time()
        self._x("""INSERT INTO sensors (name, first_seen, last_seen, events, detail) VALUES (?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, events=sensors.events+excluded.events, detail=excluded.detail""",
                (name, now, now, events, json.dumps(detail or {})))

    def list_sensors(self) -> List[dict]:
        return self._q("SELECT * FROM sensors ORDER BY name")

    # --- housekeeping ---
    def purge(self) -> int:
        cutoff = time.time() - self.retention_days * 86400
        return self._x("DELETE FROM events WHERE ts<?", (cutoff,))
