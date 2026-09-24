"""0xPlant console: REST API, sensor ingest, integrity monitoring, alert outputs and the web UI."""
from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import logging
import os
import ssl
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .auth import Authenticator, Session
from .config import Config
from .discovery import Discovery
from .events import Event, EventBus
from .integrity import IntegrityMonitor
from .outputs import Outputs
from .rules import RULES
from .store import Store

log = logging.getLogger("oxplant.console")
STATIC_DIR = os.path.dirname(__file__)
STATIC = {"/": ("ui.html", "text/html; charset=utf-8"), "/app.js": ("ui.js", "application/javascript; charset=utf-8")}


class Console:
    def __init__(self, cfg: Config, store: Optional[Store] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.console.database)
        self.bus = EventBus()
        self.bus.subscribe(self.store.add_event)
        self.outputs = Outputs(cfg.console.outputs, cfg.site)
        if self.outputs.enabled:
            self.bus.subscribe(self.outputs)
        self.auth = Authenticator({u.username: (u.role, u.password_hash) for u in cfg.console.users},
                                  cfg.console.session_hours * 3600)
        self.integrity = IntegrityMonitor(
            cfg.integrity, emit=self.bus.publish, on_change=self._on_change, get_golden=self.store.golden,
            get_identity=self.store.get_identity, set_identity=self.store.set_identity,
            on_asset_status=self.store.set_asset_status, on_asset_identity=self._on_identity)
        self.discovery = Discovery(cfg.discovery, cfg.assets, self.bus.publish, self.store.upsert_asset,
                                   runner="console", source_ip=cfg.integrity.source_ip, zone_for_ip=cfg.zone_for_ip)
        self.stop = asyncio.Event()
        self.started = time.time()
        self.ingested = 0
        for a in cfg.assets:   # seed the approved inventory
            self.store.upsert_asset({"id": a.id, "name": a.name, "type": a.type, "ip": a.ip, "zone": a.zone,
                                     "protocols": a.protocols, "ports": a.ports, "criticality": a.criticality,
                                     "vendor": a.vendor, "model": a.model, "approved": True, "status": "unknown",
                                     "detail": {"description": a.description}})

    # --- callbacks ---
    def _on_change(self, asset: str, tag: str, kind: str, old, new, status: str, note: str = "") -> None:
        self.store.add_change(asset, tag, kind, old, new, status=status, note=note)

    def _on_identity(self, asset: str, ident: dict) -> None:
        self.store.upsert_asset({"id": asset, "vendor": ident.get("VendorName", ""), "model": ident.get("ModelName") or ident.get("ProductCode", ""),
                                 "firmware": ident.get("MajorMinorRevision", ""), "application": ident.get("UserApplicationName", ""),
                                 "status": "online", "last_seen": time.time(), "detail": {"identity": ident}})

    def ingest(self, payload: dict, sensor: str) -> Dict[str, int]:
        """Store a sensor batch. Malformed records are counted and skipped; one bad record never fails the batch."""
        n = {"events": 0, "flows": 0, "assets": 0, "rejected": 0}
        lists = {k: (payload.get(k) if isinstance(payload.get(k), list) else []) for k in ("events", "flows", "assets")}
        for d in lists["events"][:5000]:
            try:
                ev = Event.from_dict(d)
                ev.sensor = ev.sensor or sensor
                self.bus.publish(ev)
                n["events"] += 1
            except (TypeError, ValueError):
                n["rejected"] += 1
        for f in lists["flows"][:5000]:
            try:
                if not isinstance(f, dict):
                    raise TypeError("flow must be an object")
                self.store.upsert_flow(dict(f, sensor=sensor))
                n["flows"] += 1
            except (TypeError, ValueError, OverflowError):
                n["rejected"] += 1
        for a in lists["assets"][:5000]:
            try:
                if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not a["id"]:
                    raise TypeError("asset must be an object with a string id")
                a.setdefault("discovered_by", sensor)
                self.store.upsert_asset(a)
                n["assets"] += 1
            except (TypeError, ValueError, OverflowError):
                n["rejected"] += 1
        if n["rejected"]:
            log.warning("sensor %s sent %d malformed record(s)", sensor, n["rejected"])
        self.store.sensor_seen(sensor, n["events"], {"last_batch": n})
        self.ingested += n["events"]
        return n

    # --- API views ---
    def summary(self) -> dict:
        assets = self.store.list_assets()
        open_alerts = self.store.list_alerts("open")
        alerts_by_asset: Dict[str, int] = {}
        for a in open_alerts:
            alerts_by_asset[a["asset"]] = alerts_by_asset.get(a["asset"], 0) + 1
        by_type: Dict[str, dict] = {}
        for a in assets:
            t = by_type.setdefault(a["type"], {"count": 0, "online": 0, "alerts": 0})
            t["count"] += 1
            t["online"] += 1 if a["status"] == "online" else 0
            t["alerts"] += alerts_by_asset.get(a["id"], 0)
        zones = []
        for z in self.cfg.zones:
            za = [a for a in assets if a["zone"] == z.id]
            zones.append({"id": z.id, "name": z.name, "level": z.level, "color": z.color, "assets": len(za),
                          "online": sum(1 for a in za if a["status"] == "online"),
                          "findings": sum(alerts_by_asset.get(a["id"], 0) for a in za),
                          "conduits": [c.id for c in self.cfg.conduits if self.cfg.asset(c.asset) and self.cfg.asset(c.asset).zone == z.id]})
        flows = self.store.list_flows()
        conduits = []
        for c in self.cfg.conduits:
            cf = [f for f in flows if f["conduit"] == c.id]
            conduits.append({"id": c.id, "asset": c.asset, "requests": sum(f["requests"] for f in cf),
                             "denied": sum(f["denied"] for f in cf), "sources": len(cf)})
        return {
            "site": self.cfg.site, "version": __version__, "uptime": time.time() - self.started, "now": time.time(),
            "assets": {"total": len(assets), "online": sum(1 for a in assets if a["status"] == "online"),
                       "offline": sum(1 for a in assets if a["status"] == "offline"),
                       "rogue": sum(1 for a in assets if not a["approved"]), "by_type": by_type},
            "alerts": self.store.alert_counts(), "events_24h": self.store.event_counts(time.time() - 86400),
            "sensors": self.store.list_sensors(), "conduits": conduits, "zones": zones,
            "recent": self.store.list_events(8),
            "process": {k: {"online": v.get("online"), "excursions": sum(1 for t in v.get("tags", {}).values() if t["status"] not in ("ok",))}
                        for k, v in list(self.integrity.live.items())},
            "outputs": {"syslog": bool(self.cfg.console.outputs.syslog_host), "webhook": bool(self.cfg.console.outputs.webhook_url)},
        }

    def protocols(self) -> list:
        flows = self.store.list_flows()
        assets = self.store.list_assets()
        out: Dict[str, dict] = {}
        for f in flows:
            p = out.setdefault(f["protocol"], {"protocol": f["protocol"], "connections": 0, "requests": 0, "denied": 0, "pairs": 0, "functions": {}, "assets": set()})
            p["pairs"] += 1
            p["requests"] += f["requests"]
            p["denied"] += f["denied"]
            p["assets"].add(f["asset"])
            for fn, n in (f.get("functions") or {}).items():
                p["functions"][fn] = p["functions"].get(fn, 0) + n
        for a in assets:
            for proto in a.get("protocols", []):
                p = out.setdefault(proto, {"protocol": proto, "connections": 0, "requests": 0, "denied": 0, "pairs": 0, "functions": {}, "assets": set()})
                p["assets"].add(a["id"])
        for p in out.values():
            p["assets"] = sorted(p["assets"])
        return sorted(out.values(), key=lambda p: -p["requests"])

    def settings(self) -> dict:
        c = self.cfg.console
        return {
            "site": self.cfg.site, "version": __version__, "config_path": self.cfg.path, "database": self.store.path,
            "retention_days": self.store.retention_days, "tls": bool(c.tls_cert), "session_hours": c.session_hours,
            "users": [{"username": u.username, "role": u.role} for u in c.users],
            "outputs": {"syslog": f"{c.outputs.syslog_host}:{c.outputs.syslog_port}" if c.outputs.syslog_host else "",
                        "webhook": _mask_url(c.outputs.webhook_url), "min_severity": c.outputs.min_severity,
                        "sent_syslog": self.outputs.sent_syslog, "sent_webhook": self.outputs.sent_webhook},
            "discovery": [{"runner": d.runner, "zone": d.zone, "targets": d.targets, "ports": d.ports, "interval_s": d.interval_s} for d in self.cfg.discovery],
            "integrity": {"source_ip": self.cfg.integrity.source_ip, "poll_s": self.cfg.integrity.poll_s,
                          "targets": [{"asset": t.asset, "host": t.host, "port": t.port, "tags": len(t.tags)} for t in self.cfg.integrity.targets]},
            "sensors": [{"name": s.name, "conduits": s.conduits} for s in self.cfg.sensors],
            "rules": [r.__dict__ for r in RULES.values()],
            "zones": [{"id": z.id, "name": z.name, "level": z.level, "subnets": z.subnets, "color": z.color} for z in self.cfg.zones],
        }

    def metrics(self) -> str:
        """Prometheus text exposition of the console's state (for time-series research and dashboards)."""
        lines = ["# HELP oxplant_info Build information", "# TYPE oxplant_info gauge", f'oxplant_info{{version="{__version__}",site="{self.cfg.site}"}} 1']
        assets = self.store.list_assets()
        lines += ["# TYPE oxplant_assets gauge"]
        for st in ("online", "offline", "unknown"):
            lines.append(f'oxplant_assets{{status="{st}"}} {sum(1 for a in assets if a["status"] == st)}')
        lines.append(f'oxplant_assets_unapproved {sum(1 for a in assets if not a["approved"])}')
        counts = self.store.alert_counts()
        lines += ["# TYPE oxplant_alerts_open gauge"] + [f'oxplant_alerts_open{{severity="{s}"}} {counts[s]}' for s in ("critical", "warning", "info")]
        ev = self.store.event_counts(0)
        lines += ["# TYPE oxplant_events_total counter"] + [f'oxplant_events_total{{severity="{s}"}} {ev.get(s, 0)}' for s in ("critical", "warning", "info")]
        lines += ["# TYPE oxplant_conduit_requests_total counter", "# TYPE oxplant_conduit_denied_total counter"]
        for f in self.store.list_flows():
            lbl = f'conduit="{f["conduit"]}",source="{f["source_asset"] or f["source_ip"]}",asset="{f["asset"]}"'
            lines.append(f"oxplant_conduit_requests_total{{{lbl}}} {f['requests']}")
            lines.append(f"oxplant_conduit_denied_total{{{lbl}}} {f['denied']}")
        lines += ["# TYPE oxplant_process_value gauge", "# TYPE oxplant_process_ok gauge", "# TYPE oxplant_invariant_ok gauge", "# TYPE oxplant_asset_online gauge"]
        for asset, v in list(self.integrity.live.items()):
            lines.append(f'oxplant_asset_online{{asset="{asset}"}} {1 if v.get("online") else 0}')
            for tag, t in (v.get("tags") or {}).items():
                if t.get("value") is not None:
                    lines.append(f'oxplant_process_value{{asset="{asset}",tag="{tag}",role="{t["role"]}"}} {t["value"]}')
                    lines.append(f'oxplant_process_ok{{asset="{asset}",tag="{tag}"}} {1 if t["status"] == "ok" else 0}')
            for name, inv in (v.get("invariants") or {}).items():
                lines.append(f'oxplant_invariant_ok{{asset="{asset}",invariant="{name}"}} {1 if inv["status"] != "violated" else 0}')
        lines += ["# TYPE oxplant_sensor_last_seen_seconds gauge"] + [f'oxplant_sensor_last_seen_seconds{{sensor="{s["name"]}"}} {s["last_seen"]}' for s in self.store.list_sensors()]
        lines.append(f"oxplant_ingested_events_total {self.ingested}")
        return "\n".join(lines) + "\n"

    def export_csv(self, kind: str) -> Optional[str]:
        rows = {"events": lambda: self.store.list_events(2000), "alerts": lambda: self.store.list_alerts("", 2000),
                "changes": lambda: self.store.list_changes(2000), "assets": self.store.list_assets, "flows": self.store.list_flows,
                "audit": lambda: self.store.list_audit(2000)}.get(kind)
        if not rows:
            return None
        data = rows()
        buf = io.StringIO()
        if data:
            cols = list(data[0].keys())
            w = csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            for r in data:
                w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        return buf.getvalue()

    def policy(self) -> list:
        return [dict(c.policy.describe(), listen=f"{c.listen_host}:{c.listen_port}", upstream=f"{c.upstream_host}:{c.upstream_port}")
                for c in self.cfg.conduits]

    # --- lifecycle ---
    async def _housekeeping(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                self.store.purge()

    def make_httpd(self) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((self.cfg.console.listen, self.cfg.console.port), make_handler(self))
        httpd.daemon_threads = True
        if self.cfg.console.tls_cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(self.cfg.console.tls_cert, self.cfg.console.tls_key or None)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        return httpd

    async def run(self) -> None:
        httpd = self.make_httpd()
        threading.Thread(target=httpd.serve_forever, daemon=True, name="oxplant-http").start()
        scheme = "https" if self.cfg.console.tls_cert else "http"
        log.info("0xPlant console %s listening on %s://%s:%d", __version__, scheme, self.cfg.console.listen, self.cfg.console.port)
        self.bus.publish(Event(f"Console started ({self.cfg.site})", "info", "SYSTEM", detail={"version": __version__}))
        tasks = [asyncio.create_task(self.integrity.run(self.stop)), asyncio.create_task(self.discovery.run(self.stop)),
                 asyncio.create_task(self._housekeeping())]
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            httpd.shutdown()


def _mask_url(url: str) -> str:
    """Show only the scheme and host of a webhook URL: paths often carry secrets."""
    if not url:
        return ""
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}/…" if u.netloc else "configured"


def _qint(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(q.get(key, default))))
    except (TypeError, ValueError):
        return default


def make_handler(console: Console):
    store, auth, cfg = console.store, console.auth, console.cfg
    secure_cookie = bool(cfg.console.tls_cert)

    class Handler(BaseHTTPRequestHandler):
        server_version = "0xPlant/" + __version__
        protocol_version = "HTTP/1.1"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

        # --- helpers ---
        def _client_ip(self) -> str:
            return self.client_address[0]

        def _session(self) -> Optional[Session]:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("oxp_session")
            return auth.get(morsel.value if morsel else None)

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                                        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: Any, extra: Optional[dict] = None) -> None:
            self._send(code, json.dumps(payload, default=str).encode(), extra=extra)

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                raise ValueError("bad content length")
            if length < 0 or length > 4_000_000:
                raise ValueError("body too large")
            data = self.rfile.read(length) if length else b""
            body = json.loads(data or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def _same_origin(self) -> bool:
            if self.headers.get("X-Requested-With") not in ("XMLHttpRequest", "oxplant-sensor"):
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = self.headers.get("Host", "")
                return urlparse(origin).netloc == host
            return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

        def _require(self, permission: str = "view") -> Optional[Session]:
            s = self._session()
            if not s:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
                return None
            if not s.can(permission):
                store.audit(s.username, "denied", self.path, "forbidden", self._client_ip(), {"permission": permission})
                self._json(HTTPStatus.FORBIDDEN, {"error": f"role {s.role} lacks permission {permission}"})
                return None
            return s

        # --- GET ---
        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            if path in STATIC:
                fname, ctype = STATIC[path]
                with open(os.path.join(STATIC_DIR, fname), "rb") as fh:
                    return self._send(HTTPStatus.OK, fh.read(), ctype)
            if path == "/api/health":
                return self._json(HTTPStatus.OK, {"ok": True, "version": __version__, "uptime": time.time() - console.started})
            if path == "/metrics":
                token = cfg.console.metrics_token
                if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad metrics token"})
                return self._send(HTTPStatus.OK, console.metrics().encode(), "text/plain; version=0.0.4; charset=utf-8")
            if path == "/api/me":
                s = self._session()
                return self._json(HTTPStatus.OK, {"authenticated": bool(s), "username": s.username if s else None,
                                                  "role": s.role if s else None, "site": cfg.site, "version": __version__})
            if not path.startswith("/api/"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            s = self._require("view")
            if not s:
                return None
            if path.startswith("/api/export/") and path.endswith(".csv"):
                body = console.export_csv(path[len("/api/export/"):-4])
                if body is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "unknown export"})
                store.audit(s.username, "export", path, "ok", self._client_ip())
                return self._send(HTTPStatus.OK, body.encode(), "text/csv; charset=utf-8", {"Content-Disposition": f"attachment; filename={path.rsplit('/', 1)[-1]}"})
            limit = _qint(q, "limit", 200, 1, 2000)
            try:
                since = max(0.0, min(float(q.get("since", 0) or 0), 4102444800.0))
            except (TypeError, ValueError):
                since = 0.0
            routes = {
                "/api/summary": lambda: console.summary(),
                "/api/assets": lambda: store.list_assets(),
                "/api/events": lambda: store.list_events(limit, q.get("severity", "")[:16], q.get("category", "")[:32], q.get("asset", "")[:128], q.get("rule", "")[:16], since),
                "/api/alerts": lambda: store.list_alerts(q.get("status", ""), limit),
                "/api/changes": lambda: store.list_changes(limit, q.get("status", "")),
                "/api/flows": lambda: store.list_flows(),
                "/api/protocols": lambda: console.protocols(),
                "/api/policy": lambda: console.policy(),
                "/api/process": lambda: dict(console.integrity.live),
                "/api/audit": lambda: {"total": store.audit_count(), "entries": store.list_audit(limit)},
                "/api/sensors": lambda: store.list_sensors(),
                "/api/settings": lambda: console.settings(),
                "/api/rules": lambda: [r.__dict__ for r in RULES.values()],
            }
            fn = routes.get(path)
            if not fn:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._json(HTTPStatus.OK, fn())

        # --- POST ---
        def do_POST(self):
            path = urlparse(self.path).path
            ip = self._client_ip()
            try:
                body = self._body()          # always consume the body first: keep-alive connections stay in sync
            except ValueError as exc:
                self.close_connection = True
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            if path == "/api/ingest":
                token = self.headers.get("Authorization", "")
                if not cfg.console.sensor_token or not hmac.compare_digest(token, f"Bearer {cfg.console.sensor_token}"):
                    store.audit("sensor", "ingest", path, "unauthorized", ip)
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "bad sensor token"})
                sensor = str(body.get("sensor", "unknown"))[:64]
                return self._json(HTTPStatus.OK, console.ingest(body, sensor))
            if not self._same_origin():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-site request refused"})
            if path == "/api/login":
                username = str(body.get("username", ""))[:64]
                session, reason = auth.login(username, str(body.get("password", "")), ip)
                if not session:
                    store.audit(username, "login", "console", f"failed: {reason}", ip)
                    n = auth.failure_count(username, ip)
                    if n and n % 5 == 0:
                        console.bus.publish(Event.from_rule("OXP-014", f"{n} failed console logins for '{username}' from {ip}; account locked",
                                                            source_ip=ip, detail={"username": username, "failures": n},
                                                            alert_key=f"OXP-014:{username}:{ip}"))
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": reason})
                store.audit(username, "login", "console", "ok", ip, {"role": session.role})
                cookie = f"oxp_session={session.token}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if secure_cookie else "")
                return self._json(HTTPStatus.OK, {"ok": True, "username": session.username, "role": session.role}, {"Set-Cookie": cookie})
            if path == "/api/logout":
                s = self._session()
                if s:
                    store.audit(s.username, "logout", "console", "ok", ip)
                    auth.logout(s.token)
                return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": "oxp_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[1] in ("alerts", "changes") and not parts[2].isdigit():
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "numeric id expected"})
            if len(parts) == 4 and parts[1] == "alerts" and parts[3] in ("ack", "resolve"):
                perm = "ack_alerts" if parts[3] == "ack" else "resolve_alerts"
                s = self._require(perm)
                if not s:
                    return None
                ok = store.set_alert_status(int(parts[2]), "acknowledged" if parts[3] == "ack" else "resolved", s.username)
                store.audit(s.username, f"alert.{parts[3]}", f"alert:{parts[2]}", "ok" if ok else "no-op", ip)
                return self._json(HTTPStatus.OK, {"ok": ok})
            if len(parts) == 4 and parts[1] == "changes" and parts[3] in ("approve", "reject"):
                s = self._require("review_changes")
                if not s:
                    return None
                change = store.get_change(int(parts[2]))
                if not change:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "no such change"})
                status = "approved" if parts[3] == "approve" else "rejected"
                ticket = str(body.get("ticket", ""))[:64]
                ok = store.review_change(change["id"], status, s.username, ticket, str(body.get("note", ""))[:500])
                if ok and status == "approved" and change["kind"] == "config" and change["new_value"] is not None:
                    store.set_golden(change["asset"], change["tag"], float(change["new_value"]))
                store.audit(s.username, f"change.{parts[3]}", f"change:{change['id']} {change['asset']}/{change['tag']}", "ok", ip, {"ticket": ticket})
                return self._json(HTTPStatus.OK, {"ok": ok})
            if len(parts) == 4 and parts[1] == "assets" and parts[3] in ("approve", "unapprove"):
                s = self._require("approve_assets")
                if not s:
                    return None
                ok = store.approve_asset(parts[2], parts[3] == "approve")
                if ok and parts[3] == "approve":
                    a = store.get_asset(parts[2])
                    if a:
                        store.resolve_alert_key(f"OXP-009:{a['ip']}", s.username)
                store.audit(s.username, f"asset.{parts[3]}", parts[2], "ok" if ok else "no-op", ip)
                return self._json(HTTPStatus.OK, {"ok": ok})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler
