# Crash recovery evidence

- Scenario: public_02_incidents.
- The controller was killed with SIGKILL after its approval request was durably recorded as pending; the simulator process remained alive.
- The restarted controller reused the same SQLite database, reconciled the request through `/approvals`, resumed from the event cursor, applied it once, and finished the scenario.
- Controller finish line: `{"credits_remaining":191.0,"database":"runs/controller_crash_20261107_131610.sqlite","decisions":37,"events_ingested":324295,"events_retained":245095,"finished":true,"peak_rss_mb":60.94,"scenario":"public_02_incidents","sim_ts":"2027-01-14T00:00:00+05:30","wall_seconds":9.526}`
- Database check:

```
processed_events=324295
stored_call_records=245008
resume_audits=1
applied_proposals=4
```

Logs: `reports/crash_first.log`, `reports/crash_resume.log`, `reports/crash_sim.log`.
