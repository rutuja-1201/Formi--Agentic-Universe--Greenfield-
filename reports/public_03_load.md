# Public 03 load run

Measured on the local Apple Silicon Mac, 4 vCPU/8 GB target hardware was not available. The controller used a streaming NDJSON reader, SQLite WAL, compact indexed KPI rows, and compressed per-call audit records.

| Measure | Result |
|---|---:|
| Scenario | `public_03_load` (12 workflows, 5 simulated days) |
| Wall time, controller process | 733.4 s (12 min 13 s), including history bootstrap |
| End-to-end events ingested | 9,737,591, including pre-D0 history |
| Events/sec, whole run average | 13,277 |
| Active simulated-day decision loop | 92.7–97.7 s/day, approximately 8,600–9,100 events/s |
| History bootstrap | 5.54M events in 258.7 s, approximately 21,400 events/s |
| Peak controller RSS | 45.08 MB (`getrusage`; does not include simulator process or OS page cache) |
| Retained call records | 7,433,447 compressed call records, plus 144 control-plane events |
| SQLite database size | 4.1 GB |
| API errors | 0 |
| Credits used | 0 of 400 |
| Config decisions | 0; no population incident crossed policy thresholds |

The target asks for one simulated day in under ten wall-clock minutes; the measured active-day loop took under 100 seconds, including a six-hour status interval. The full five-day run plus history bootstrap took just over twelve minutes. This was not measured on the specified Linux host, so it is a local reference point, not a hardware guarantee.

**First limit at 10×:** retained call history and SQLite write volume, followed by single-process chunk parsing and one SQLite writer. The current design is memory-light, but it retains detailed call-level evidence indefinitely. Production would need a governed retention tier: compact aggregates and decision evidence in the hot relational store, with encrypted call evidence archived by tenant and time. Partition workers by workflow/tenant and keep event IDs idempotent across partitions. Do not claim 10× capacity from this run.
