"""Operator HMI: polls the PLCs over Modbus/TCP and serves the operator screens.

Server side it keeps what a real HMI/SCADA node keeps: an alarm manager
(ISA-18.2 style states: active/unacknowledged, active/acknowledged,
cleared/unacknowledged), a short-term historian for trends, and operator
sessions with roles (viewer, operator, supervisor).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from modbuslite import ModbusClient, ModbusException

from .config import HMIConfig, HMIPLC
from .programs import PROGRAMS
from .registers import TagMap, status_tags

log = logging.getLogger("plant.hmi")
HTML_PATH = os.path.join(os.path.dirname(__file__), "hmi.html")
ROLE_RANK = {"viewer": 0, "operator": 1, "supervisor": 2}
ACCESS_RANK = {"operator": 1, "supervisor": 2}
HISTORY_SECONDS = 1800


def hash_password(password: str, iterations: int = 600_000, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_hex = encoded.split("$", 3)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iterations))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class AlarmManager:
    """Alarm states per (PLC, tag): derived from discrete 'Interlock:'/'Alarm:' tags plus communication loss."""

    def __init__(self) -> None:
        self.alarms: Dict[Tuple[str, str], dict] = {}
        self.history: Deque[dict] = deque(maxlen=500)
        self.lock = threading.Lock()

    @staticmethod
    def definitions(tag_map: TagMap) -> List[Tuple[str, str, str]]:
        out = []
        for t in tag_map:
            if t.table == "discrete" and t.desc.startswith(("Interlock:", "Alarm:")):
                out.append((t.name, t.desc.split(":", 1)[1].strip(), "trip" if t.desc.startswith("Interlock") else "alarm"))
        return out

    def _transition(self, key: Tuple[str, str], text: str, cls: str, active: bool, now: float) -> None:
        a = self.alarms.get(key)
        if active and a is None:
            self.alarms[key] = {"plc": key[0], "tag": key[1], "text": text, "class": cls, "active": True, "acked": False,
                                "first_ts": now, "last_ts": now, "count": 1, "ack_by": None}
            self.history.appendleft({"ts": now, "plc": key[0], "tag": key[1], "text": text, "class": cls, "event": "ACTIVE"})
        elif active and not a["active"]:
            a.update(active=True, acked=False, last_ts=now, count=a["count"] + 1)
            self.history.appendleft({"ts": now, "plc": key[0], "tag": key[1], "text": text, "class": cls, "event": "ACTIVE"})
        elif not active and a is not None and a["active"]:
            a["active"] = False
            self.history.appendleft({"ts": now, "plc": key[0], "tag": key[1], "text": text, "class": cls, "event": "CLEARED"})
            if a["acked"]:
                del self.alarms[key]                      # cleared and acknowledged: leaves the summary

    def update(self, plc: str, defs: List[Tuple[str, str, str]], tags: Dict[str, float], online: bool, error: str) -> None:
        now = time.time()
        with self.lock:
            self._transition((plc, "COMM"), f"Communication lost with {plc}" + (f" ({error})" if error else ""), "trip", not online, now)
            if online:
                for tag, text, cls in defs:
                    self._transition((plc, tag), text, cls, bool(tags.get(tag)), now)

    def ack(self, plc: Optional[str], tag: Optional[str], user: str) -> int:
        n = 0
        now = time.time()
        with self.lock:
            for key, a in list(self.alarms.items()):
                if (plc and key[0] != plc) or (tag and key[1] != tag) or a["acked"]:
                    continue
                a["acked"], a["ack_by"] = True, user
                self.history.appendleft({"ts": now, "plc": key[0], "tag": key[1], "text": a["text"], "class": a["class"], "event": f"ACK by {user}"})
                n += 1
                if not a["active"]:
                    del self.alarms[key]
        return n

    def snapshot(self) -> dict:
        with self.lock:
            active = sorted(self.alarms.values(), key=lambda a: (a["acked"], -a["last_ts"]))
            return {"alarms": [dict(a) for a in active], "history": list(self.history)[:200],
                    "unacked": sum(1 for a in active if not a["acked"]), "active": sum(1 for a in active if a["active"])}


class Historian:
    def __init__(self, seconds: int = HISTORY_SECONDS):
        self.seconds = seconds
        self.series: Dict[str, Dict[str, Deque[Tuple[float, float]]]] = {}
        self.lock = threading.Lock()
        self._last: Dict[str, float] = {}

    def record(self, plc: str, tags: Dict[str, float]) -> None:
        now = time.time()
        if now - self._last.get(plc, 0) < 1.0:
            return
        self._last[plc] = now
        with self.lock:
            s = self.series.setdefault(plc, {})
            for k, v in tags.items():
                s.setdefault(k, deque(maxlen=self.seconds)).append((round(now, 1), v))

    def query(self, plc: str, tags: List[str], seconds: int) -> Dict[str, list]:
        cutoff = time.time() - seconds
        with self.lock:
            s = self.series.get(plc, {})
            return {t: [p for p in s.get(t, []) if p[0] >= cutoff] for t in tags}


class PLCPoller:
    def __init__(self, cfg: HMIPLC, source_ip: Optional[str], poll_s: float):
        self.cfg = cfg
        self.map = TagMap(list(PROGRAMS[cfg.program].tags) + status_tags())
        self.alarm_defs = AlarmManager.definitions(self.map)
        kwargs = {"local_addr": (source_ip, 0)} if source_ip else {}
        self.client = ModbusClient(cfg.host, cfg.port, unit=cfg.unit, timeout=1.5, **kwargs)
        self.poll_s = poll_s
        self.state: Dict[str, Any] = {"name": cfg.name, "program": cfg.program, "online": False, "tags": {}, "error": "", "ts": 0.0}
        self.ranges = self._plan_reads()

    def _plan_reads(self):
        plan = []
        for table in ("input", "holding", "coils", "discrete"):
            addrs = sorted(t.address for t in self.map if t.table == table)
            if not addrs:
                continue
            start, prev = addrs[0], addrs[0]
            for a in addrs[1:] + [None]:
                if a is None or a - prev > 8:
                    plan.append((table, start, prev - start + 1))
                    if a is not None:
                        start = a
                if a is not None:
                    prev = a
        return plan

    async def poll_once(self) -> None:
        raw: Dict[str, Dict[int, int]] = {"input": {}, "holding": {}, "coils": {}, "discrete": {}}
        for table, start, count in self.ranges:
            if table == "input":
                vals = await self.client.read_input_registers(start, count)
            elif table == "holding":
                vals = await self.client.read_holding_registers(start, count)
            elif table == "coils":
                vals = await self.client.read_coils(start, count)
            else:
                vals = await self.client.read_discrete_inputs(start, count)
            for i, v in enumerate(vals):
                raw[table][start + i] = v
        tags = {t.name: self.map.decode(t.name, raw[t.table][t.address]) for t in self.map if t.address in raw[t.table]}
        self.state.update({"online": True, "tags": tags, "error": "", "ts": time.time()})

    async def run(self, stop: asyncio.Event, hmi: "HMI") -> None:
        failures = 0
        while not stop.is_set():
            try:
                await self.poll_once()
                failures = 0
                hmi.historian.record(self.cfg.name, self.state["tags"])
            except Exception as exc:  # noqa: BLE001 - transact() already closed the socket on protocol errors
                failures += 1
                self.state.update({"online": False, "error": f"{type(exc).__name__}: {exc}"})
                if failures in (1, 10):
                    log.warning("%s poll failed: %s", self.cfg.name, exc)
                await asyncio.sleep(min(5.0, failures * 0.5))
            hmi.alarms.update(self.cfg.name, self.alarm_defs, self.state["tags"], self.state["online"], self.state["error"])
            await asyncio.sleep(self.poll_s)
        await self.client.close()

    async def write(self, tag_name: str, value: float, role: str) -> None:
        tag = self.map.by_name.get(tag_name)
        if tag is None or tag.access not in ACCESS_RANK:
            raise PermissionError(f"{tag_name} is not writable from the HMI")
        if ROLE_RANK.get(role, 0) < ACCESS_RANK[tag.access]:
            raise PermissionError(f"{tag_name} needs the {tag.access} role")
        if tag.table == "holding":
            await self.client.write_register(tag.address, self.map.encode(tag_name, value))
        else:
            await self.client.write_coil(tag.address, bool(value))


class HMI:
    def __init__(self, cfg: HMIConfig):
        self.cfg = cfg
        self.pollers = {p.name: PLCPoller(p, cfg.source_ip, cfg.poll_ms / 1000.0) for p in cfg.plcs}
        self.alarms = AlarmManager()
        self.historian = Historian()
        self.sessions: Dict[str, dict] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop = asyncio.Event()
        self.started = time.time()
        self.users = {u["username"]: u for u in cfg.users}

    def login(self, username: str, password: str) -> Optional[dict]:
        u = self.users.get(username)
        if not u or not verify_password(password, u["password_hash"]):
            return None
        token = secrets.token_urlsafe(24)
        self.sessions[token] = {"token": token, "username": username, "role": u.get("role", "viewer"), "since": time.time()}
        log.info("HMI login %s (%s)", username, self.sessions[token]["role"])
        return self.sessions[token]

    def snapshot(self) -> dict:
        al = self.alarms.snapshot()
        return {"ts": time.time(), "plcs": {name: dict(p.state, tag_defs=p.map.describe()) for name, p in self.pollers.items()},
                "alarm_summary": {"unacked": al["unacked"], "active": al["active"]}}

    def write(self, plc: str, tag: str, value: float, session: dict) -> dict:
        if plc not in self.pollers:
            return {"ok": False, "error": f"unknown PLC {plc}"}
        assert self.loop
        fut = asyncio.run_coroutine_threadsafe(self.pollers[plc].write(tag, value, session["role"]), self.loop)
        try:
            fut.result(timeout=3.0)
            log.info("operator write %s %s=%s by %s ok", plc, tag, value, session["username"])
            return {"ok": True}
        except PermissionError as exc:
            return {"ok": False, "error": str(exc)}
        except ModbusException as exc:
            log.warning("operator write %s %s=%s rejected: %s", plc, tag, value, exc)
            return {"ok": False, "error": f"Rejected by device/conduit: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        httpd = ThreadingHTTPServer((self.cfg.listen, self.cfg.port), make_handler(self))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        log.info("HMI listening on http://%s:%d (%d users)", self.cfg.listen, self.cfg.port, len(self.users))
        try:
            await asyncio.gather(*(p.run(self.stop, self) for p in self.pollers.values()))
        finally:
            httpd.shutdown()


def make_handler(hmi: HMI):
    class Handler(BaseHTTPRequestHandler):
        server_version = "0xPlant-HMI/2.2"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        def _json(self, code: int, payload: Any, cookie: Optional[str] = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _session(self) -> Optional[dict]:
            if not hmi.users:                                  # no users configured: open lab HMI, supervisor rights
                return {"username": "operator", "role": "supervisor"}
            m = SimpleCookie(self.headers.get("Cookie", "")).get("hmi_session")
            return hmi.sessions.get(m.value) if m else None

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as fh:
                    body = fh.read()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/api/health":
                return self._json(HTTPStatus.OK, {"ok": True, "uptime": time.time() - hmi.started})
            s = self._session()
            if url.path == "/api/me":
                return self._json(HTTPStatus.OK, {"authenticated": bool(s), "username": s and s["username"], "role": s and s["role"], "login_required": bool(hmi.users)})
            if not s:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "login required"})
            if url.path == "/api/state":
                return self._json(HTTPStatus.OK, dict(hmi.snapshot(), user=s["username"], role=s["role"]))
            if url.path == "/api/alarms":
                return self._json(HTTPStatus.OK, hmi.alarms.snapshot())
            if url.path == "/api/history":
                plc = q.get("plc", "")
                tags = [t for t in q.get("tags", "").split(",") if t][:12]
                seconds = max(10, min(HISTORY_SECONDS, int(q.get("seconds", "600") or 600)))
                return self._json(HTTPStatus.OK, {"plc": plc, "seconds": seconds, "series": hmi.historian.query(plc, tags, seconds)})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            try:
                length = max(0, min(int(self.headers.get("Content-Length", "0") or 0), 65536))
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("object expected")
            except ValueError as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad request: {exc}"})
            if self.path == "/api/login":
                s = hmi.login(str(data.get("username", ""))[:64], str(data.get("password", "")))
                if not s:
                    return self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid credentials"})
                return self._json(HTTPStatus.OK, {"ok": True, "username": s["username"], "role": s["role"]}, f"hmi_session={s['token']}; Path=/; HttpOnly; SameSite=Strict")
            s = self._session()
            if not s:
                return self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "login required"})
            if self.path == "/api/logout":
                hmi.sessions.pop(s.get("token", ""), None)
                return self._json(HTTPStatus.OK, {"ok": True}, "hmi_session=; Path=/; Max-Age=0")
            if self.path == "/api/write":
                try:
                    result = hmi.write(str(data["plc"]), str(data["tag"]), float(data["value"]), s)
                except (KeyError, ValueError, TypeError) as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"bad request: {exc}"})
                return self._json(HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT, result)
            if self.path == "/api/alarms/ack":
                if ROLE_RANK.get(s["role"], 0) < 1:
                    return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "operator role required"})
                n = hmi.alarms.ack(data.get("plc") or None, data.get("tag") or None, s["username"])
                return self._json(HTTPStatus.OK, {"ok": True, "acknowledged": n})
            return self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    return Handler
