<div align="center">

<img src="docs/screenshots/banner.svg" width="100%" alt="0xPlant"/>

<br>

[![License](https://img.shields.io/badge/License-MIT-10B981?style=flat-square)](LICENSE)
[![Assets](https://img.shields.io/badge/Demo_Assets-2,461-10B981?style=flat-square)]()
[![Protocols](https://img.shields.io/badge/OT_Protocols-9-10B981?style=flat-square)]()
[![Vendors](https://img.shields.io/badge/Vendors-13-10B981?style=flat-square)]()

### Manage your plant floor like you manage your cloud.

**ICS/OT/IoT Infrastructure Management Platform**

[Live Demo](https://siteq8.github.io/0xPlant) · [Modules](#modules) · [Quick Start](#quick-start)

</div>

---

## What is 0xPlant

0xPlant is a ProxCenter-style management interface for ICS, OT, and IoT infrastructure. Instead of managing VMs and containers, you manage PLCs, RTUs, HMIs, DCS, safety systems, switches, and IoT sensors.

**The name:** `0x` (hexadecimal prefix — because we speak in registers and opcodes) + `Plant` (the factory floor we protect). Geeky by design.

---

## Demo Access

| Username | Password |
|----------|----------|
| `admin` | `Plant@2025` |

**Live:** [https://siteq8.github.io/0xPlant](https://siteq8.github.io/0xPlant)

---

## Modules

| Module | Description |
|--------|-------------|
| **Dashboard** | Overview: 2,461 assets, asset distribution by type (PLC/RTU/HMI/DCS/SIS/Switch/IoT/Server), recent activity feed with color-coded events |
| **Inventory** | Full asset table: ID, name, type, vendor/model, firmware, protocol, Purdue zone, IP, status. 12 demo assets from Siemens, Rockwell, Yokogawa, Schneider, GE, ABB, Palo Alto, Hirschmann, AVEVA, OSIsoft |
| **Topology** | Protocol connection map: 8 protocols with connection counts, unique communication pairs, cross-zone flows, and anomaly detection (unauthorized SMBv1 flagged) |
| **Protocols** | 9 ICS/OT protocol cards: Modbus, S7comm, OPC UA, EtherNet/IP, DNP3, MQTT, BACnet, IEC 104, TriStation — with port numbers, standards, and live traffic stats |
| **Purdue Zones** | ISA/IEC 62443 zone model (L0-L5 + DMZ) with asset counts, conduit counts, findings, and health status per zone |
| **Events** | Real-time event log: security incidents, config changes, remote access sessions, patch deployments, backups — with timestamps and source details |
| **Change Tracking** | Configuration change audit: who changed what, when, whether it was approved (CAB reference), unauthorized changes flagged in red |
| **Alerts** | Active alerts: 3 critical, 8 warning, 12 info. Alert table with severity, affected asset, description, timestamp, and status |
| **Task Center** | Operational tasks: patching schedules, firmware upgrades, investigations, pen tests — with assignee, due date, priority, and status |
| **Vulnerabilities** | ICS-CERT correlation: 6 real CVEs matched to inventory with CVSS scores, affected asset counts, available patches, and remediation status |
| **Settings** | Discovery, alerts, SIEM integration, golden image monitoring, rogue device detection, protocol baseline enforcement |

---

## Supported Vendors & Protocols

### Vendors
Siemens · Allen-Bradley/Rockwell · Yokogawa · Schneider Electric · GE · ABB · AVEVA · OSIsoft · Palo Alto · Hirschmann · Johnson Controls · Eclipse Mosquitto · Moxa

### Protocols
| Protocol | Port | Standard |
|----------|------|----------|
| Modbus/TCP | 502 | IEC 61158 |
| S7comm+ | 102 | Siemens |
| OPC UA | 4840 | IEC 62541 |
| EtherNet/IP | 44818 | IEC 61158 |
| DNP3 | 20000 | IEEE 1815 |
| MQTT | 1883 | ISO 20922 |
| BACnet/IP | 47808 | ISO 16484-5 |
| IEC 60870-5-104 | 2404 | IEC 60870 |
| TriStation | 1502 | Schneider |

---

## Quick Start

### Online
**[https://siteq8.github.io/0xPlant](https://siteq8.github.io/0xPlant)**

### Local
```bash
git clone https://github.com/SiteQ8/0xPlant.git
cd 0xPlant && open docs/index.html
```

Login: `admin` / `Plant@2025`

---

## Design

**Sidebar navigation** (like ProxCenter/Vercel) instead of top tabs. Clean light content area with dark navy sidebar. Fira Code mono for technical data, Source Sans 3 for body text. Emerald green (`#10B981`) accent.

**11 pages** organized into 3 sections:
- **Infrastructure:** Inventory, Topology, Protocols, Purdue Zones
- **Operations:** Events, Change Tracking, Alerts, Task Center
- **Security:** Vulnerabilities, Settings

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">
  <sub>0xPlant — ICS/OT/IoT Infrastructure Management</sub><br>
  <sub><a href="https://github.com/SiteQ8">@SiteQ8</a> — Ali AlEnezi</sub>
</div>
