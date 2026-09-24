# Traffic dataset

When `sensors[].record` is set, every request that reaches a conduit is appended to a JSON Lines file, one object per line, with the policy decision as the label. The local configuration writes `data/traffic-local.jsonl`.

| Field | Type | Meaning |
|---|---|---|
| ts | float | Unix time the request was decoded |
| sensor, conduit, asset | string | Sensor name, conduit id, protected PLC |
| src, src_asset | string | Source IP and the asset name resolved from the policy (`unlisted` if none) |
| fc, fn | int, string | Modbus function code and name |
| table | string | coils, discrete, holding, input or null |
| address, quantity | int | Start address and count |
| values | list or null | Values for write requests (first 16) |
| allowed | bool | Policy decision |
| rule | string or null | OXP rule id when refused (OXP-001/002/003/004) |
| exception | int | Modbus exception code returned (0 = normal response) |
| latency_ms | float | Round trip to the PLC for forwarded requests |
| baseline | string | off, learning or enforcing |

Example:

```json
{"ts":1790000000.123456,"sensor":"SENSOR-001","conduit":"CONDUIT-002","asset":"PLC-002","src":"127.0.3.30","src_asset":"HMI-001","fc":6,"fn":"Write Single Register","table":"holding","address":200,"quantity":1,"values":[500],"allowed":false,"rule":"OXP-002","exception":2,"latency_ms":0.0,"baseline":"enforcing"}
```

Console data (events, alerts, changes, assets, flows, audit) can be exported as CSV from `/api/export/<kind>.csv` with a console session, and time series are available from `/metrics` in Prometheus format for scraping.

Suggested uses: supervised classification of permitted vs refused requests, sequence modelling of per-source polling patterns, evaluation of the built-in baseline (OXP-017) against learned models, and correlation of conduit traffic with process values from `/metrics`.
