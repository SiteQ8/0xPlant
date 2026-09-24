# 0xPlant testbed architecture

0xPlant is a research testbed made of two halves that run against each other: a plant that behaves like a real ICS, and a security product that protects it. Everything is implemented from the wire up (no vendor emulators, no third-party protocol stacks), so every layer is inspectable and modifiable.

## Layers

```
 Level 3   EWS-001 (engineering tool)      0xPlant CONSOLE (UI, API, /metrics, integrity monitor, discovery, store)
 Level 2   HMI-001 (operator screen)       IoT MQTT broker        0xPlant SENSORS (conduits, discovery, MQTT monitor, recorder)
 Level 1   PLC-001 intake  <->  PLC-002 treatment  <->  PLC-003 distribution      IoT sensor fleet
 Level 0   physical process model (tanks, pumps, filter, dosing, network demand)   fault-injection API (testbed instrumentation)
```

| Component | Module | Role |
|---|---|---|
| Modbus/TCP stack | `modbuslite/` | Codec, asyncio server and client; FC 1-6, 15, 16, 43/14 |
| Process model and PLC programs | `plant/programs.py` | Physics per section, control logic, interlocks, PLC-to-PLC coupling |
| Soft PLC runtime | `plant/plc.py` | Scan cycle, Modbus server, remote polling, fault board |
| Fault injection | `plant/faults.py` | HTTP API for sensor, actuator, link, blackout and identity faults |
| HMI / EWS | `plant/hmi.py`, `plant/ews.py` | Operator and engineering clients |
| IoT layer | `plant/mqtt.py`, `plant/iot.py` | MQTT 3.1.1 broker/client and a condition-monitoring sensor fleet |
| Conduit | `oxplant/conduit.py`, `oxplant/policy.py` | Inline Modbus gateway with allowlists, rate limits, baselining, recording |
| Baselining and recorder | `oxplant/baseline.py` | Learned traffic profiles (OXP-017) and JSONL datasets |
| Discovery | `oxplant/discovery.py` | Service probing, device identification, rogue detection |
| Integrity monitor | `oxplant/integrity.py`, `oxplant/invariants.py` | Envelopes, golden config, interlocks, identity, physics invariants (OXP-018) |
| MQTT monitor | `oxplant/mqttmon.py` | Topic/publisher/payload baselining (OXP-019/020) |
| Console | `oxplant/console.py`, `oxplant/store.py` | REST API, UI, auth, RBAC, audit, alert correlation, CSV export, Prometheus metrics |

## Data flow

1. PLCs execute a 250 ms scan: read commands, run interlocks and control, advance the physics by `scan × time_scale`, publish registers. Faults are applied after publication so the *reported* value can differ from the *true* state.
2. Every Level 2/3 request to a PLC passes through a conduit. The conduit decodes the request, applies the policy, forwards or refuses it, records it, updates the source's traffic profile, and emits events.
3. Sensors batch events, flow statistics and discovered assets to the console over an authenticated HTTP channel every 2 s (buffered while the console is down).
4. The console stores everything in SQLite, correlates events into alerts, polls the PLCs read-only through the conduits, evaluates envelopes and invariants, and exposes the UI, JSON API, CSV exports and `/metrics`.

## Time

The plant runs `time_scale` times faster than real time (60× by default): one real second is one simulated minute, so tank dynamics and the diurnal demand curve are observable in minutes. Detection latencies reported by the experiment harness are *real* seconds.

## Trust boundaries

* The PLCs trust nothing about the network: they clamp setpoints to engineering limits and reject writes outside the operator and engineering regions themselves.
* Conduits are the only path from Level 2/3 into Level 1 (enforced by Docker networks in the compose lab, by loopback address plan locally).
* The fault-injection API is testbed instrumentation. It is bound to the management interface and is not reachable through a conduit; in a real deployment it would not exist.

## Browser testbed

The plant physics and PLC programs also exist as a JavaScript port (`plant/plantsim.js`) that is parity-tested against the Python programs (`tests/test_browser_plant.py`: 1200 scans, every process value within 2 %). On top of it, `plant/hmi_demo.js` implements the HMI's HTTP API (alarm manager, historian, sessions, roles) and `oxplant/ui_live.js` implements the integrity monitor rules (OXP-005/006/007/012/013/018, including the invariant expression language). The published demo therefore runs a real control system and a real detector in the browser; it is useful for teaching and for quick what-if experiments, while measured results should come from the Python lab where traffic really crosses the conduits.

The PLC logic, in both implementations, acts on the transmitter values it published on the previous scan rather than on the physical state. A frozen or spoofed sensor therefore misleads the controller exactly as it would on a real PLC (a +2.5 bar offset on PT-301 trips the high-high interlock and stops the high-lift pumps), which is what makes sensor-integrity attacks worth detecting from the network side.
