"""0xPlant - open source ICS/OT security monitoring and protection.

Components:
  conduit    protective Modbus/TCP gateway enforcing per-source allowlists (IEC 62443 conduit)
  discovery  asset discovery with device identification and rogue device detection
  integrity  process integrity monitoring: safe envelopes, configuration drift, interlocks
  console    web console, REST API, alerting, change tracking, audit log
  sensor     runs conduits and discovery close to the process and reports to the console
"""
__version__ = "2.2.0"
