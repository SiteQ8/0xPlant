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
        from plant.hmi import hash_password
        users = [{"username": "operator", "role": "operator", "password_hash": hash_password("pw", 1000)},
                 {"username": "supervisor", "role": "supervisor", "password_hash": hash_password("pw", 1000)}]
        hmi = HMI(HMIConfig("127.0.0.1", http_port, None, 100, [HMIPLC("PLC-001", "intake", "127.0.0.1", plc_port)], users))
        htask = asyncio.create_task(hmi.run())
        loop = asyncio.get_running_loop()

        cookie = {"v": ""}

        def call(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)
            conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json", "Cookie": cookie["v"]})
            r = conn.getresponse()
            sc = r.getheader("Set-Cookie")
            if sc:
                cookie["v"] = sc.split(";")[0]
            data = json.loads(r.read() or b"{}") if "json" in (r.getheader("Content-Type") or "") else r.read()
            conn.close()
            return r.status, data

        assert await wait_for(lambda: hmi.pollers["PLC-001"].state["online"], 5)
        assert (await loop.run_in_executor(None, call, "GET", "/api/state"))[0] == 401          # login required
        status, res = await loop.run_in_executor(None, call, "POST", "/api/login", {"username": "operator", "password": "pw"})
        assert status == 200 and res["role"] == "operator"
        status, state = await loop.run_in_executor(None, call, "GET", "/api/state")
        assert status == 200 and state["plcs"]["PLC-001"]["tags"]["LEVEL_STOP_SP"] == 85.0 and state["role"] == "operator"
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-001", "tag": "RESET_CMD", "value": 1})
        assert status == 409 and "supervisor" in res["error"]
        await asyncio.sleep(1.2)
        status, h = await loop.run_in_executor(None, call, "GET", "/api/history?plc=PLC-001&tags=LT-101,FT-101&seconds=60")
        assert status == 200 and len(h["series"]["LT-101"]) >= 1
        status, al = await loop.run_in_executor(None, call, "GET", "/api/alarms")
        assert status == 200 and al["unacked"] == 0
        status, res = await loop.run_in_executor(None, call, "POST", "/api/login", {"username": "supervisor", "password": "pw"})
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-001", "tag": "RESET_CMD", "value": 1})
        assert status == 200 and res["ok"]
        status, page = await loop.run_in_executor(None, call, "GET", "/")
        assert status == 200 and b"Operator HMI" in page
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-001", "tag": "PUMP_SPEED_SP", "value": 65})
        assert status == 200 and res["ok"] and await wait_for(lambda: plc.store.holding[102] == 6500, 3)
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-001", "tag": "LAHH_LIMIT", "value": 50})
        assert status == 409 and "not writable" in res["error"]
        status, res = await loop.run_in_executor(None, call, "POST", "/api/write", {"plc": "PLC-009", "tag": "X", "value": 1})
        assert status == 409
        hmi.stop.set()
        await asyncio.wait_for(htask, 5)
        await stop_plc(plc, task)
    run(inner())
