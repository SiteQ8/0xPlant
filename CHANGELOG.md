# Changelog

## 2.2.0 — control system, console widgets and a live browser demo

- HMI backend: ISA-18.2 style alarm manager (active/unacknowledged, acknowledge, history), 1 s historian with a trends screen, operator and supervisor sessions with role-checked writes
- Plant sequence on the treatment PLC (stopped, wait upstream, filling, running, stopping, held) with supervisor start/stop and reset commands; PLC logic now acts on the measured transmitter values, so a spoofed or frozen sensor drives the control system as it would on a real PLC
- Console dashboard rebuilt as a widget grid: plant overview driven by the integrity polls, security posture score, alert timeline, zone health, invariant and sensor health widgets, function-code heatmap
- Browser port of the plant physics and PLC programs (`plant/plantsim.js`, parity-tested against the Python programs) with an in-browser HMI backend and an in-browser integrity monitor: the GitHub Pages demo runs a real control system and detects injected faults live
- Research controls on both demo pages (sensor freeze, actuator failure, sensor spoofing, link loss, blackout, configuration tampering, operator setpoint changes)

## 2.1.0 — research testbed

- Fault injection API on every soft PLC (sensor stuck/offset/noise, actuator failure, link loss, blackout, identity change)
- Physics-based invariants with a safe expression language (OXP-018): mass balances and actuator/flow consistency
- Behavioural baselining of conduit traffic (OXP-017) and a labelled JSONL traffic recorder
- IoT layer: dependency-free MQTT 3.1.1 broker and client, virtual condition-monitoring sensor fleet, MQTT monitor (OXP-019/020)
- Console: Prometheus `/metrics`, CSV exports, invariant status on the process page
- Reproducible experiment harness (`research/run_experiments.py`) measuring detection latency per rule
- Research documentation: architecture, threat model, methodology, dataset schema; CITATION.cff

## 2.0.1 — interactive UI and review fixes

- Redesigned console (charts, topology map, drawers, search/sort, dark mode) and animated HMI
- Fixes from an independent code review across store, policy, conduit, auth, console and plant logic
- Live demo website built from the real UI with captured data

## 2.0.0 — real plant and security tool

- Simulated water treatment plant with three Modbus/TCP soft PLCs, HMI and EWS tool
- 0xPlant conduits, discovery, integrity monitor, console, alerting, change tracking, audit, syslog/webhook outputs
- Docker Compose lab with Purdue-segmented networks, local runner, test-suite, CI

## 1.0.0

- Static UI demo of the management platform concept
