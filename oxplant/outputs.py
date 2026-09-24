"""Alert outputs: syslog (CEF over UDP) and JSON webhooks."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request
from datetime import datetime, timezone

from .config import OutputConfig
from .events import SEVERITY_ORDER, Event

log = logging.getLogger("oxplant.outputs")
CEF_SEVERITY = {"info": 3, "warning": 6, "critical": 9}


def cef(ev: Event, site: str) -> str:
    def esc(s) -> str:
        return str(s).replace("\\", "\\\\").replace("|", "\\|").replace("=", "\\=").replace("\n", " ")
    ext = f"rt={int(ev.ts * 1000)} src={ev.source_ip} dst={ev.dest_ip} cs1Label=asset cs1={esc(ev.asset)} " \
          f"cs2Label=sensor cs2={esc(ev.sensor)} app={esc(ev.protocol)} cat={esc(ev.category)} msg={esc(json.dumps(ev.detail))}"
    return f"CEF:0|0xPlant|{esc(site)}|2.0|{ev.rule or ev.category}|{esc(ev.title)}|{CEF_SEVERITY.get(ev.severity, 3)}|{ext}"


class Outputs:
    def __init__(self, cfg: OutputConfig, site: str):
        self.cfg = cfg
        self.site = site
        self.min = SEVERITY_ORDER.get(cfg.min_severity, 1)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if cfg.syslog_host else None
        self.sent_syslog = 0
        self.sent_webhook = 0
        self.webhook_errors = 0

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.syslog_host or self.cfg.webhook_url)

    def __call__(self, ev: Event) -> None:
        if SEVERITY_ORDER.get(ev.severity, 0) < self.min:
            return
        if self.sock:
            pri = 8 * 1 + {"info": 6, "warning": 4, "critical": 2}.get(ev.severity, 5)   # facility user
            ts = datetime.fromtimestamp(ev.ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            line = f"<{pri}>1 {ts} oxplant-console oxplant - - - {cef(ev, self.site)}"
            try:
                self.sock.sendto(line.encode("utf-8", "replace"), (self.cfg.syslog_host, self.cfg.syslog_port))
                self.sent_syslog += 1
            except OSError as exc:
                log.warning("syslog send failed: %s", exc)
        if self.cfg.webhook_url:
            threading.Thread(target=self._post, args=(ev,), daemon=True).start()

    def _post(self, ev: Event) -> None:
        body = json.dumps({"site": self.site, "event": ev.to_dict(), "cef": cef(ev, self.site), "sent": time.time()}).encode()
        req = urllib.request.Request(self.cfg.webhook_url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "0xPlant/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            self.sent_webhook += 1
        except Exception as exc:  # noqa: BLE001
            self.webhook_errors += 1
            log.warning("webhook failed: %s", exc)
