# Experimental methodology

`research/run_experiments.py` runs controlled scenarios against the local lab and measures detection latency per rule.

## Procedure

1. The harness starts the lab (`scripts/run_local.py --no-hmi`), waits for start-up and, when a scenario depends on behavioural baselines, for the learning windows (`sensors[].learning_s`, 45 s locally) to close.
2. It records every warning/critical event raised during the quiet period as *false alerts* (the plant runs normally during this time).
3. For each run and scenario it resolves open alerts, records `t0`, performs the action, and polls the console for the first event of the expected rule with `ts >= t0`. Latency is `event.ts - t0` in real seconds. If nothing arrives within the scenario's timeout the run counts as *not detected*.
4. The scenario's cleanup runs (faults cleared, setpoints restored) and the harness waits `--settle` seconds.
5. Results go to `research/results/experiments-<timestamp>.json` (raw latencies per run) and `.md` (summary table with detection rate, mean, median and max latency).

## Scenarios

| Key | Action | Expected rule | Mechanism |
|---|---|---|---|
| config-drift | EWS lowers AAHH_LIMIT on PLC-002 | OXP-006 | Integrity poll vs golden value |
| setpoint-change | HMI changes PRESSURE_SP | OXP-013 | Integrity poll |
| write-out-of-range | HMI writes holding 200 | OXP-002 | Conduit refusal |
| unlisted-host | 127.0.3.77 reads PLC-002 | OXP-003 | Conduit refusal |
| identification-denied | HMI sends FC 43 | OXP-001 | Conduit refusal |
| new-pattern | HMI reads holding 150-157 for the first time | OXP-017 | Behavioural baseline |
| mqtt-new-topic | New publisher on the broker | OXP-019 | MQTT monitor baseline |
| stuck-sensor | LT-101 frozen while P-101 fails | OXP-018 | Mass-balance invariant |
| dosing-failure | P-201 fails | OXP-005 | Envelope after residual decay |
| plc-blackout | PLC-003 stops answering | OXP-016 | Conduit upstream failure |
| plc-offline-integrity | same | OXP-007 | Integrity poll failures |
| firmware-change | PLC-002 reports revision 9.9.9 | OXP-008 | Identity snapshot comparison |
| rogue-device | Modbus server on 127.0.1.25 | OXP-009 | Sensor discovery cycle |

## Reproducibility

* Plant physics are seeded (`seed` per PLC) and deterministic apart from wall-clock scheduling.
* Detection latency depends on configured intervals: integrity `poll_s`, discovery `interval_s`, MQTT/conduit `learning_s`. Report them with results.
* Use `--runs N` for repeated measurements; the JSON keeps every raw latency.
* The traffic recorder (`sensors[].record`) produces the labelled dataset of every request seen during the experiment (see dataset.md).

## Sample results

A complete single-run campaign on the local lab is kept in [`research/results/sample-20260924T183441Z.md`](../../research/results/sample-20260924T183441Z.md) (13/13 scenarios detected, no false alerts in the quiet period). Refusals are immediate; integrity findings arrive within one poll interval plus debounce; the dosing failure is bounded by chlorine decay dynamics.

## Extending

Add a `Scenario` to `research/run_experiments.py`: an action, an optional cleanup, the rule you expect and a timeout. Actions may use the fault API (`plant/faults.py`), the Modbus client bound to any loopback source address, the EWS tool or the MQTT client.
