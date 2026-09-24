import asyncio
import http.client
import json

from plant.config import HMIConfig, HMIPLC
from plant.hmi import HMI
from tests.conftest import free_port, start_plc, stop_plc, wait_for


def test_hmi_state_and_operator_writes(run):
    async def inner():
        plc_port, http_port = free_port(), free_port()
        plc, task = await start_plc("PLC-001", "intake", plc_port)
        hmi = HMI(HMIConfig("127.0.0.1", http_port, None, 100, [HMIPLC("PLC-001", "intake", "127.0.0.1", plc_port)]))
        htask = asyncio.create_task(hmi.run())
        loop = asyncio.get_running_loop()

        def call(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)
            conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
            r = conn.getresponse()
            data = json.loads(r.read() or b"{}") if "json" in (r.getheader("Content-Type") or "") else r.read()
            conn.close()
            return r.status, data

        assert await wait_for(lambda: hmi.pollers["PLC-001"].state["online"], 5)
        status, state = await loop.run_in_executor(None, call, "GET", "/api/state")
        assert status == 200 and state["plcs"]["PLC-001"]["tags"]["LEVEL_STOP_SP"] == 85.0
        status, page = await loop.run_in_executor(None, call, "GET", "/")
        assert status == 200 and b"Operator HMI" in page
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-001", "tag": "PUMP_SPEED_SP", "value": 65})
        assert status == 200 and res["ok"] and await wait_for(lambda: plc.store.holding[102] == 6500, 3)
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-001", "tag": "LAHH_LIMIT", "value": 50})
        assert status == 409 and "not an operator-writable" in res["error"]
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-009", "tag": "X", "value": 1})
        assert status == 409
        hmi.stop.set()
        await asyncio.wait_for(htask, 5)
        await stop_plc(plc, task)
    run(inner())
