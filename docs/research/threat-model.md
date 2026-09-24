# Threat model

The plant under protection is a water treatment works with three PLCs, an HMI, an engineering workstation and an IoT layer. Assets, adversary positions and the detection coverage 0xPlant provides are listed below so experiments can be designed against explicit assumptions.

## Assets and consequences

| Asset | Compromise consequence |
|---|---|
| Chlorine dosing (PLC-002) | Under-dosing (unsafe water) or over-dosing (AAHH interlock, plant stop) |
| Intake and clearwell levels (PLC-001/003) | Overflow, dry-running pumps, loss of supply |
| Network pressure (PLC-003) | Pipe bursts (PAHH) or loss of supply (PALL) |
| Engineering configuration (holding 200-219) | Interlock limits moved, protection silently weakened |
| Operator view (HMI) | Operators act on false data |
| IoT telemetry | Condition monitoring blinded or misled |

## Adversary positions considered

| Position | Description | Coverage |
|---|---|---|
| A1 Unlisted host on Level 2/3 | A device not in the conduit policy talks to a PLC | OXP-003 (refused), OXP-009 (discovery), OXP-015 |
| A2 Compromised HMI | The HMI's address is used for requests outside its allowlist | OXP-001, OXP-002, OXP-004, OXP-017 |
| A3 Compromised EWS / insider | Engineering changes without a change ticket | OXP-006 + change review, OXP-013 |
| A4 Sensor or PLC manipulation | Reported process values are false (stuck, offset, spoofed) | OXP-018 invariants, OXP-005 envelopes |
| A5 Device replacement / firmware change | A PLC reports a different identity | OXP-008 |
| A6 Denial of service on a PLC | PLC stops answering | OXP-016, OXP-007 |
| A7 Rogue IoT device or broker misuse | New topics/publishers, malformed telemetry | OXP-019, OXP-020, OXP-010 (cleartext broker) |
| A8 Console attacks | Credential guessing, CSRF, cross-site scripting | OXP-014, session/CSRF/CSP controls, audit log |

## Out of scope

* Attacks on the PLC firmware or the physical process from Level 0 (no field bus is modelled).
* Encrypted or authenticated Modbus (the plant models the common unauthenticated case deliberately).
* Sensor-to-console channel compromise (the bearer token is shared; TLS is configurable on the console).

## Notes on what "detection" means here

Refusals (OXP-001/002/003/004) are *preventive*: the request never reaches the PLC and the event is raised at the same time. Everything else is *detective* and its latency depends on polling intervals (integrity 2 s, discovery 60-120 s) or on process dynamics (envelope excursions). The experiment harness reports these latencies separately per rule.
