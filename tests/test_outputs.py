import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from oxplant.config import OutputConfig
from oxplant.events import Event
from oxplant.outputs import Outputs, cef
from tests.conftest import free_port


def test_cef_formatting_escapes_pipes():
    ev = Event.from_rule("OXP-002", "Write | blocked", source_ip="1.1.1.1", dest_ip="2.2.2.2", asset="PLC-1", protocol="Modbus/TCP")
    line = cef(ev, "Site|A")
    assert line.startswith("CEF:0|0xPlant|Site\\|A|2.0|OXP-002|Write \\| blocked|9|")
    assert "src=1.1.1.1 dst=2.2.2.2 cs1Label=asset cs1=PLC-1" in line


def test_syslog_and_webhook_delivery():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(3)
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", free_port()), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    out = Outputs(OutputConfig(syslog_host="127.0.0.1", syslog_port=udp.getsockname()[1],
                               webhook_url=f"http://127.0.0.1:{httpd.server_port}/hook", min_severity="warning"), "Test")
    out(Event("quiet", "info"))                                    # below threshold
    out(Event.from_rule("OXP-005", "excursion", asset="PLC-2"))
    data, _ = udp.recvfrom(4096)
    assert b"OXP-005" in data and data.startswith(b"<")
    import time
    for _ in range(50):
        if received:
            break
        time.sleep(0.05)
    assert received and received[0]["event"]["rule"] == "OXP-005" and received[0]["site"] == "Test"
    assert out.sent_syslog == 1 and out.sent_webhook == 1
    httpd.shutdown()
