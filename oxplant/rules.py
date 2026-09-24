"""Detection rule catalog. Every alert and security event references one of these."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

INFO, WARNING, CRITICAL = "info", "warning", "critical"


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    category: str
    description: str
    reference: str = ""


RULES: Dict[str, Rule] = {r.id: r for r in [
    Rule("OXP-001", "Unauthorized protocol function", CRITICAL, "SECURITY",
         "A source used a Modbus function code it is not permitted to use on this conduit.",
         "IEC 62443-3-3 SR 5.2, MITRE ATT&CK for ICS T0855"),
    Rule("OXP-002", "Write outside permitted range", CRITICAL, "SECURITY",
         "A write targeted registers or coils outside the ranges permitted for the source.",
         "IEC 62443-3-3 SR 3.8, MITRE ATT&CK for ICS T0836"),
    Rule("OXP-003", "Unknown source on conduit", CRITICAL, "SECURITY",
         "A host that is not part of the conduit policy attempted to communicate with a protected asset.",
         "IEC 62443-3-3 SR 1.2, NIST SP 800-82 6.2.2"),
    Rule("OXP-004", "Request rate anomaly", WARNING, "SECURITY",
         "A source exceeded the request rate baseline for the conduit; excess requests were throttled.",
         "MITRE ATT&CK for ICS T0814"),
    Rule("OXP-005", "Process value outside safe envelope", CRITICAL, "PROCESS",
         "A monitored process value left its engineered safe operating envelope.",
         "IEC 61511, NIST SP 800-82 6.2.6"),
    Rule("OXP-006", "Configuration drift from golden baseline", WARNING, "CONFIG",
         "An engineering configuration register differs from the approved golden value.",
         "IEC 62443-2-1 4.3.4.3, NERC CIP-010"),
    Rule("OXP-007", "Asset offline", WARNING, "AVAILABILITY",
         "A monitored asset stopped answering protocol requests.",
         "IEC 62443-3-3 SR 7.1"),
    Rule("OXP-008", "Device identity changed", CRITICAL, "CONFIG",
         "The vendor, model, firmware revision or application name reported by the device changed.",
         "NERC CIP-010 R1, MITRE ATT&CK for ICS T0857"),
    Rule("OXP-009", "Rogue device discovered", CRITICAL, "NETWORK",
         "A host not present in the approved asset inventory exposes services on an OT network.",
         "IEC 62443-2-1 4.2.3.4, NIST SP 800-82 6.2.1"),
    Rule("OXP-010", "Insecure service exposed", WARNING, "NETWORK",
         "A cleartext or legacy service (Telnet, FTP, HTTP, MQTT without TLS, SMBv1) is reachable on an OT network.",
         "IEC 62443-3-3 SR 4.1"),
    Rule("OXP-011", "Malformed protocol frame", WARNING, "SECURITY",
         "A source sent a frame that does not decode as Modbus/TCP; the connection was dropped.",
         "MITRE ATT&CK for ICS T0868"),
    Rule("OXP-012", "Safety interlock tripped", CRITICAL, "PROCESS",
         "A PLC reported a latched safety interlock.",
         "IEC 61511"),
    Rule("OXP-013", "Operator setpoint change", INFO, "OPERATIONS",
         "An operator setpoint changed; recorded for change tracking.",
         "IEC 62443-2-1 4.3.4.3"),
    Rule("OXP-014", "Console authentication failure", WARNING, "AUTH",
         "Repeated failed logins to the 0xPlant console; the account was temporarily locked.",
         "IEC 62443-3-3 SR 1.11"),
    Rule("OXP-015", "New conversation observed", INFO, "NETWORK",
         "A permitted source started talking to a protected asset for the first time since start-up.",
         "NIST SP 800-82 6.2.2"),
    Rule("OXP-016", "Upstream device unreachable", WARNING, "AVAILABILITY",
         "The conduit could not reach the protected asset behind it.",
         "IEC 62443-3-3 SR 7.1"),
]}


def get(rule_id: str) -> Rule:
    return RULES[rule_id]
