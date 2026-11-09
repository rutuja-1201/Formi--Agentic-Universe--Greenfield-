#!/usr/bin/env python3
"""Restartable, conservative controller for the Formi simulator HTTP API."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import resource
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (event_id INTEGER PRIMARY KEY, workflow_id TEXT, kind TEXT, ts TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_wf_ts ON events(workflow_id, ts);
CREATE TABLE IF NOT EXISTS call_audit (event_id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, ts TEXT NOT NULL, payload BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS call_audit_wf_ts ON call_audit(workflow_id, ts);
CREATE TABLE IF NOT EXISTS call_stats (event_id INTEGER PRIMARY KEY, workflow_id TEXT NOT NULL, ts TEXT NOT NULL,
    caller_id TEXT, connected INTEGER NOT NULL, engaged INTEGER NOT NULL, hallucination_flag INTEGER NOT NULL,
    config_version INTEGER, prompt_version TEXT, voice TEXT, welcome_message TEXT, cohort_id TEXT);
CREATE INDEX IF NOT EXISTS call_stats_wf_ts ON call_stats(workflow_id, ts);
CREATE INDEX IF NOT EXISTS call_stats_wf_cid_ts ON call_stats(workflow_id, caller_id, ts);
CREATE INDEX IF NOT EXISTS call_stats_wf_voice_ts ON call_stats(workflow_id, voice, ts);
CREATE INDEX IF NOT EXISTS call_stats_wf_wm_ts ON call_stats(workflow_id, welcome_message, ts);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, workflow_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (proposal_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, base_version INTEGER NOT NULL, approval_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS proposals_wf_status ON proposals(workflow_id, status);
"""

# Minimum arm sample and a family-wise conservative threshold across two
# dimensions and up to six pairwise comparisons per dimension.
MIN_VARIANT_SAMPLE = 500
MIN_TOTAL_SAMPLE = 600
Z_THRESHOLD = 2.8
# Maximum shift per change to avoid drastic swings
CONSERVATIVE_SHIFT = 0.15


def now_utc() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Controller:
    def __init__(self, url: str, db_path: Path):
        self.url = url.rstrip("/")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Migrate: add columns for variant tracking if missing
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(call_stats)").fetchall()}
        for col in ["voice", "welcome_message", "cohort_id"]:
            if col not in cols:
                self.db.execute(f"ALTER TABLE call_stats ADD COLUMN {col} TEXT")
        self.db.commit()
        self.http = httpx.Client(base_url=self.url, timeout=120)
        self.workflows: dict[str, dict[str, Any]] = {}
        self.run_id = ""
        self.stats = {"chunks": 0, "events": 0, "decisions": 0, "api_errors": 0, "credits_start": 0}
        self.hall_baselines: dict[tuple[str, str], tuple[int, int]] = {}
        self.started = time.monotonic()
        self.peak_rss_mb = 0.0

    def get(self, path: str, **kwargs):
        r = self.http.get(path, **kwargs)
        if r.status_code >= 500:
            self.stats["api_errors"] += 1
            r.raise_for_status()
        return r

    def post(self, path: str, **kwargs):
        r = self.http.post(path, **kwargs)
        if r.status_code >= 500:
            self.stats["api_errors"] += 1
            r.raise_for_status()
        return r

    def audit(self, workflow_id: str | None, action: str, detail: dict[str, Any], ts: str | None = None, commit: bool = True):
        self.db.execute("INSERT INTO audit(ts,workflow_id,action,detail) VALUES(?,?,?,?)",
                        (ts or now_utc(), workflow_id, action, json_dump(detail)))
        if commit:
            self.db.commit()
        self.stats["decisions"] += 1

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: Any, commit: bool = True):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))
        if commit:
            self.db.commit()

    def initialize(self):
        state = self.get("/sim/state").json()
        self.run_id = state["scenario"]
        if self.meta("scenario") != self.run_id:
            self.db.execute("DELETE FROM meta")
            self.db.execute("DELETE FROM events")
            self.db.execute("DELETE FROM call_audit")
            self.db.execute("DELETE FROM call_stats")
            self.db.execute("DELETE FROM audit")
            self.db.execute("DELETE FROM proposals")
            self.db.commit()
            self.set_meta("scenario", self.run_id)
            self.audit(None, "controller_started", {"scenario": self.run_id, "db": str(self.db.execute("PRAGMA database_list").fetchone()[2])}, state["ts"])
        else:
            self.audit(None, "controller_resumed", {"scenario": self.run_id, "sim_minute": state["sim_minute"],
                        "last_chunk": self.meta("last_chunk", "-1"), "last_event_id": self.meta("last_event_id", "0")}, state["ts"])
        self.stats["credits_start"] = state.get("credits_remaining", 0)
        self.workflows = {w["workflow_id"]: w for w in self.get("/workflows").json()}
        # Bootstrap from API-provided history. Event IDs make replay idempotent.
        if self.meta("history_loaded") != "1":
            listing = self.get("/events/chunks", params={"kind": "history"}).json()
            for item in listing:
                self.ingest_chunk(item["chunk"])
            self.set_meta("history_loaded", "1")
            self.audit(None, "history_bootstrapped", {"chunks": len(listing), "events": self.meta("events_ingested_total", "0")}, state["ts"])
        # Recover approvals if the process stopped after a remote create but before local response save.
        for wid in self.workflows:
            try:
                rows = self.get("/approvals", params={"workflow_id": wid}).json()
                for a in rows if isinstance(rows, list) else rows.get("approvals", []):
                    pid = a.get("proposal_id")
                    if pid and self.db.execute("SELECT 1 FROM proposals WHERE proposal_id=?", (pid,)).fetchone():
                        self.db.execute("UPDATE proposals SET approval_id=?,status=?,updated_at=? WHERE proposal_id=?",
                                        (a.get("approval_id", a.get("id")), a.get("status", "pending"), now_utc(), pid))
                self.db.commit()
            except httpx.HTTPError:
                self.stats["api_errors"] += 1
        return state

    def ingest_chunk(self, n: int):
        last_event_id = int(self.meta("last_event_id", "0"))
        new_count = 0
        for line in self.event_lines(n):
            if not line.strip():
                continue
            ev = json.loads(line)
            eid = ev.get("event_id")
            if eid is None or eid <= last_event_id:
                continue
            last_event_id = eid
            new_count += 1
            # Keep call-level evidence and control-plane events. Lead feature
            # records are intentionally not retained (C2 is out of scope).
            if ev.get("type") not in ("call", "config_changed", "approval_decided", "campaign_created"):
                continue
            if ev.get("type") == "call":
                ev = {k: ev[k] for k in ("event_id", "type", "ts", "tenant_id", "workflow_id", "campaign_id",
                     "lead_id", "person_id", "attempt_no", "campaign_day_index", "config_version", "cohort_id",
                     "variant", "connected", "engaged", "disposition", "duration_s", "turns", "qa") if k in ev}
                variant = ev.get("variant", {})
                qa = ev.get("qa") or {}
                self.db.execute("INSERT OR IGNORE INTO call_audit(event_id,workflow_id,ts,payload) VALUES(?,?,?,?)",
                                (eid, ev.get("workflow_id"), ev.get("ts", ""), zlib.compress(json_dump(ev).encode(), 6)))
                self.db.execute("""INSERT OR IGNORE INTO call_stats(event_id,workflow_id,ts,caller_id,connected,engaged,
                    hallucination_flag,config_version,prompt_version,voice,welcome_message,cohort_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (eid, ev.get("workflow_id"), ev.get("ts", ""), variant.get("caller_id"),
                                 int(bool(ev.get("connected"))), int(bool(ev.get("engaged"))),
                                 int(bool(qa.get("hallucination_flag"))), ev.get("config_version"), variant.get("prompt_version"),
                                 variant.get("voice"), variant.get("welcome_message"), ev.get("cohort_id")))
                continue
            cur = self.db.execute("INSERT OR IGNORE INTO events(event_id,workflow_id,kind,ts,payload) VALUES(?,?,?,?,?)",
                                   (eid, ev.get("workflow_id"), ev.get("type"), ev.get("ts", ""), json_dump(ev)))
            if cur.rowcount:
                self.process_event(ev)
        self.stats["events"] += new_count
        self.set_meta("last_event_id", last_event_id, commit=False)
        self.set_meta("events_ingested_total", int(self.meta("events_ingested_total", "0")) + new_count, commit=False)
        self.set_meta("last_chunk", max(n, int(self.meta("last_chunk", "-1"))), commit=False)
        self.db.commit()
        self.stats["chunks"] += 1

    def event_lines(self, chunk: int):
        # NDJSON history chunks can be very large. Stream line-by-line so one
        # HTTP response cannot expand into a multi-gigabyte Python string.
        with self.http.stream("GET", f"/events/chunks/{chunk}") as response:
            if response.status_code >= 500:
                self.stats["api_errors"] += 1
            response.raise_for_status()
            yield from response.iter_lines()

    def process_event(self, ev: dict[str, Any]):
        if ev.get("type") == "config_changed":
            self.audit(ev.get("workflow_id"), "observed_config_change", {
                "actor": ev.get("actor"), "version": ev.get("version"),
                "changed_dims": ev.get("changed_dims", []), "proposal_id": ev.get("proposal_id"),
                "approval_id": ev.get("approval_id"), "event_id": ev.get("event_id")}, ev.get("ts"), commit=False)
            if ev.get("actor") == "supervisor":
                for dim in ("voice", "welcome_message"):
                    if dim in ev.get("changed_dims", []):
                        key = f"experiment:{ev.get('workflow_id')}:{dim}"
                        raw = self.meta(key)
                        state = json.loads(raw) if raw else {}
                        if state.get("status") == "active":
                            state.update({"status": "cancelled", "cancelled_at": ev.get("ts"), "reason": "supervisor_edit"})
                            self.set_meta(key, json_dump(state), commit=False)
        elif ev.get("type") == "approval_decided":
            row = self.db.execute("SELECT proposal_id FROM proposals WHERE approval_id=?", (ev.get("approval_id"),)).fetchone()
            if row:
                self.db.execute("UPDATE proposals SET status=?,updated_at=? WHERE proposal_id=?",
                                (ev.get("status"), now_utc(), row["proposal_id"]))
            self.audit(ev.get("workflow_id"), "approval_decided", {k: ev.get(k) for k in ("approval_id", "status", "reasons", "event_id")}, ev.get("ts"), commit=False)

    def synchronize_events(self):
        after = int(self.meta("last_chunk", "-1"))
        listing = self.get("/events/chunks", params={"kind": "events", "after": after}).json()
        for item in listing:
            self.ingest_chunk(item["chunk"])

    def query_calls(self, workflow_id: str, since: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM call_audit WHERE workflow_id=?"
        params: list[Any] = [workflow_id]
        if since:
            sql += " AND ts>=?"
            params.append(since)
        return [json.loads(zlib.decompress(r[0])) for r in self.db.execute(sql, params)]

    def current_config(self, wid: str) -> dict[str, Any]:
        return self.get(f"/workflows/{wid}/config").json()

    def active_proposal(self, wid: str) -> bool:
        return self.db.execute("SELECT 1 FROM proposals WHERE workflow_id=? AND status IN ('submitting','pending','approved') LIMIT 1", (wid,)).fetchone() is not None

    def proposal(self, wid: str, current: dict[str, Any], cfg: dict[str, Any], summary: str, rationale: str,
                 diff: dict[str, Any], impact: float, evidence: dict[str, Any] | None = None):
        wf = self.workflows[wid]
        state = self.get("/sim/state").json()
        sim_ts = state["ts"]
        local = datetime.fromisoformat(sim_ts)
        start, end = wf["governance"]["approver_hours"]
        if not start <= local.hour < end:
            return False
        # Deterministic idempotency key: a crash/retry cannot create a second request.
        latest_call = self.db.execute("SELECT coalesce(max(event_id),0) FROM call_stats WHERE workflow_id=?", (wid,)).fetchone()[0]
        # A terminal request may be retried with fresh data; its new event
        # watermark gives it a new id while a crash replay keeps this one.
        stable = json_dump([self.run_id, wid, current["version"], diff, latest_call])
        pid = "ctl_" + hashlib.sha256(stable.encode()).hexdigest()[:24]
        existing = self.db.execute("SELECT status FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        if existing:
            return False
        calls = self.query_calls(wid, (local - timedelta(days=1)).isoformat())
        body = {"proposal_id": pid, "workflow_id": wid, "base_version": current["version"],
                "proposed_config": cfg, "change_summary": summary[:180], "diff": diff,
                "affected_leads": int(wf.get("daily_batch", {}).get("size", 0)),
                "dry_run": {"method": "replay_observed_calls", "calls_observed_24h": len(calls),
                            "baseline_primary": self.primary_rate(wid, calls), "guardrail": "separately monitored"},
                "impact_score": max(0.0, min(1.0, impact)), "rationale": rationale}
        if evidence:
            body["evidence"] = evidence
        self.db.execute("INSERT INTO proposals(proposal_id,workflow_id,base_version,status,payload,updated_at) VALUES(?,?,?,?,?,?)",
                        (pid, wid, current["version"], "submitting", json_dump(body), now_utc()))
        self.db.commit()
        self.audit(wid, "proposal_prepared", {"proposal_id": pid, "base_version": current["version"],
                    "summary": summary, "diff": diff, "affected_leads_estimate": body["affected_leads"],
                    "dry_run": body["dry_run"], "impact_score": body["impact_score"], "rationale": rationale}, sim_ts)
        r = self.post("/approvals", json=body)
        if r.status_code == 409:
            self.db.execute("UPDATE proposals SET status='duplicate_reconciled',updated_at=? WHERE proposal_id=?", (now_utc(), pid))
            self.db.commit()
            self.audit(wid, "proposal_duplicate_reconciled", {"proposal_id": pid, "response": r.text[:500]}, sim_ts)
            return False
        if r.status_code >= 400:
            self.db.execute("UPDATE proposals SET status='submission_failed',updated_at=? WHERE proposal_id=?", (now_utc(), pid))
            self.db.commit()
            self.audit(wid, "proposal_submission_failed", {"proposal_id": pid, "status_code": r.status_code, "response": r.text[:500]}, sim_ts)
            return False
        obj = r.json()
        aid = obj.get("approval_id", obj.get("id"))
        self.db.execute("UPDATE proposals SET approval_id=?,status=?,updated_at=? WHERE proposal_id=?",
                        (aid, obj.get("status", "pending"), now_utc(), pid))
        self.db.commit()
        self.audit(wid, "proposal_submitted", {"proposal_id": pid, "approval_id": aid, "response": obj}, sim_ts)
        return True

    def primary_rate(self, wid: str, calls: list[dict[str, Any]]) -> float | None:
        objective = self.workflows[wid]["objective"]["primary"]
        if objective == "pickup_rate":
            n = len(calls)
            return sum(bool(c.get("connected")) for c in calls) / n if n else None
        con = [c for c in calls if c.get("connected")]
        return sum(bool(c.get("engaged")) for c in con) / len(con) if con else None

    def attempt_auto(self, wid: str, current: dict[str, Any], cfg: dict[str, Any], rationale: str, dim: str) -> bool:
        gov = self.workflows[wid]["governance"].get("auto_apply", {})
        if not gov.get("enabled") or dim not in gov.get("allowed_dims", []):
            return False
        body = {"proposal_id": "ctl_" + hashlib.sha256(f"{self.run_id}:{wid}:{current['version']}:{json_dump(cfg)}".encode()).hexdigest()[:24],
                "base_version": current["version"], "proposed_config": cfg, "rationale": rationale}
        r = self.http.put(f"/workflows/{wid}/config", json=body)
        if r.status_code == 200:
            self.audit(wid, "auto_applied", {"proposal_id": body["proposal_id"], "dimension": dim,
                        "base_version": current["version"], "result": r.json(), "rationale": rationale}, self.get("/sim/state").json()["ts"])
            return True
        self.audit(wid, "auto_apply_denied", {"proposal_id": body["proposal_id"], "dimension": dim,
                    "status_code": r.status_code, "response": r.text[:600]}, self.get("/sim/state").json()["ts"])
        return False

    def caller_id_detection(self, wid: str, sim_ts: str):
        wf = self.workflows[wid]
        since = (datetime.fromisoformat(sim_ts) - timedelta(days=2)).isoformat()
        grouped = self.db.execute("""SELECT caller_id,count(*),sum(connected),sum(engaged) FROM call_stats
            WHERE workflow_id=? AND ts>=? GROUP BY caller_id""", (wid, since)).fetchall()
        by_id = {r[0]: (int(r[1]), int(r[2] or 0), int(r[3] or 0)) for r in grouped if r[0]}
        if sum(n for n, _, _ in by_id.values()) < 80 or len(by_id) < 2:
            return
        # Compare each sufficiently sampled caller ID with all other caller IDs.
        bad = []
        guardrail = wf["objective"].get("guardrail", {})
        if "pickup_rate_max_drop" in guardrail:
            guardrail_metric, max_drop = "pickup", float(guardrail["pickup_rate_max_drop"])
        elif "engagement_rate_max_drop" in guardrail:
            guardrail_metric, max_drop = "engagement", float(guardrail["engagement_rate_max_drop"])
        else:
            guardrail_metric, max_drop = "", 0.0
        for cid, (n, connected_n, engaged_n) in by_id.items():
            other_n = sum(m for k, (m, _, _) in by_id.items() if k != cid)
            other_connected = sum(x for k, (_, x, _) in by_id.items() if k != cid)
            other_engaged = sum(y for k, (_, _, y) in by_id.items() if k != cid)
            if n < 50 or other_n < 50:
                continue
            rate, other_rate = connected_n / n, other_connected / other_n
            guardrail_rate = connected_n/n if guardrail_metric == "pickup" else (engaged_n/connected_n if connected_n else 0.0)
            other_guardrail_rate = other_connected/other_n if guardrail_metric == "pickup" else (other_engaged/other_connected if other_connected else 0.0)
            se = (rate * (1-rate) / n + other_rate * (1-other_rate) / other_n) ** 0.5
            if other_rate - rate >= max(0.08, 2.0 * se) and other_guardrail_rate >= guardrail_rate - max_drop:
                bad.append((cid, rate, other_rate, n))
        if not bad or self.active_proposal(wid):
            return
        cid, rate, others, n = sorted(bad, key=lambda x: x[1])[0]
        cur = self.current_config(wid)
        cfg = copy.deepcopy(cur["config"])
        changed = False
        for cohort, alloc in cfg.get("allocations", {}).items():
            dist = alloc.get("caller_id")
            if not dist or cid not in dist or len(dist) < 2:
                continue
            candidates = [k for k in dist if k != cid and k in by_id and by_id[k][0] >= 30]
            if not candidates:
                continue
            best = max(candidates, key=lambda k: by_id[k][1] / by_id[k][0])
            bad_weight = float(dist[cid])
            # The simulator's max_shift is a total-variation bound; moving x
            # probability mass from one caller ID to another costs exactly x.
            move = min(bad_weight, float(wf["governance"].get("auto_apply", {}).get("max_shift", 0)))
            dist[cid] = round(bad_weight - move, 6)
            dist[best] = round(float(dist.get(best, 0)) + move, 6)
            alloc["caller_id"] = {k: v for k, v in dist.items() if v > 0}
            changed = True
        if not changed or cfg == cur["config"]:
            return
        detail = {"bad_caller_id": cid, "bad_pickup": rate, "comparison_pickup": others, "sample": n}
        rationale = f"Population signal: {cid} pickup was {rate:.3f} over {n} dials versus {others:.3f} for alternatives over the recent window. Shifted within the workflow's configured max_shift toward the observed strongest alternative; pickup and engagement remain separate."
        diff = {"caller_id": detail, "new_allocations": cfg["allocations"]}
        if self.attempt_auto(wid, cur, cfg, rationale, "caller_id"):
            return
        if not self.active_proposal(wid):
            self.proposal(wid, cur, cfg, f"Reduce exposure to underperforming caller ID {cid}", rationale, diff,
                          min(1.0, max(0.0, (others-rate)*2)))

    def hallucination_detection(self, wid: str, sim_ts: str):
        prior_start = (datetime.fromisoformat(sim_ts) - timedelta(days=8)).isoformat()
        prior_end = (datetime.fromisoformat(sim_ts) - timedelta(days=1)).isoformat()
        day_key = sim_ts[:10]
        baseline_key = (wid, day_key)
        if baseline_key not in self.hall_baselines:
            row = self.db.execute("""SELECT count(*),sum(hallucination_flag) FROM call_stats
                WHERE workflow_id=? AND ts>=? AND ts<? AND connected=1""",
                (wid, prior_start, prior_end)).fetchone()
            self.hall_baselines[baseline_key] = (int(row[0] or 0), int(row[1] or 0))
        prior_n, prior_flagged = self.hall_baselines[baseline_key]
        recent = self.db.execute("""SELECT count(*),sum(hallucination_flag) FROM call_stats
            WHERE workflow_id=? AND ts>=? AND connected=1""",
            (wid, (datetime.fromisoformat(sim_ts) - timedelta(days=1)).isoformat())).fetchone()
        connected_n, flagged_n = int(recent[0] or 0), int(recent[1] or 0)
        prior_rate = prior_flagged / prior_n if prior_n else 0.02
        recent_rate = flagged_n / connected_n if connected_n else 0.0
        se = ((recent_rate * (1-recent_rate) / max(1, connected_n)) +
              (prior_rate * (1-prior_rate) / max(1, prior_n))) ** 0.5
        # Detector flags are noisy. Require a minimum population, an absolute
        # excess and a 3-SE separation from the preceding week.
        if connected_n < 80 or flagged_n < 6 or recent_rate < max(0.04, prior_rate * 2) or recent_rate-prior_rate < 3*se:
            return
        if self.active_proposal(wid):
            return
        cur = self.current_config(wid)
        prompt = cur["config"].get("prompt_version")
        # One response per affected prompt/day; a supervisor edit changes the key and is observed first.
        day = sim_ts[:10]
        incident_key = f"hall:{wid}:{prompt}:{day}"
        if self.meta(incident_key) == "handled" or self.meta(f"hallfix_attempted:{wid}"):
            return
        state = self.get("/sim/state").json()
        begin, end = self.workflows[wid]["governance"]["approver_hours"]
        if not begin <= datetime.fromisoformat(sim_ts).hour < end:
            return
        if state.get("credits_remaining", 0) < 12:
            self.audit(wid, "incident_deferred_low_credits", {"incident": "hallucination", "prompt": prompt,
                        "connected": connected_n, "flagged": flagged_n, "credits": state.get("credits_remaining")}, sim_ts)
            self.set_meta(incident_key, "handled")
            return
        problem = f"Population hallucination rate {flagged_n}/{connected_n} in last day; current prompt {prompt}. Propose a grounded safety correction without changing business cadence or call windows."
        # Spend credits once per workflow per run. A changed prompt is then
        # verified from call events instead of triggering a chain of retries.
        self.set_meta(f"hallfix_attempted:{wid}", "1")
        suggestion = self.post("/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem})
        if suggestion.status_code >= 400:
            self.audit(wid, "llm_suggestion_failed", {"status_code": suggestion.status_code, "body": suggestion.text[:500]}, sim_ts)
            return
        obj = suggestion.json()
        choices = obj.get("suggestions", obj.get("fixes", obj.get("alternatives", [])))
        if isinstance(choices, dict):
            choices = [choices]
        ranked = []
        min_pass = self.workflows[wid]["governance"]["min_validation_pass_rate"]
        for choice in choices[:3]:
            pv = choice.get("prompt_version", choice.get("version"))
            if not pv or pv == prompt or pv not in self.workflows[wid]["options"].get("prompt_version", []):
                continue
            val = self.post("/validation/runs", json={"workflow_id": wid, "prompt_version": pv, "n": 200})
            if val.status_code >= 400:
                continue
            v = val.json()
            pass_rate = v.get("pass_rate", 0)
            ranked.append((pass_rate, pv, v.get("validation_run_id", v.get("id")), choice.get("summary", "Validated prompt option")))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        self.audit(wid, "hallucination_detected", {"prompt": prompt, "connected": connected_n, "flagged": flagged_n,
                    "rate": recent_rate, "baseline_rate": prior_rate, "suggestion_response": obj, "validated_candidates": ranked}, sim_ts)
        self.set_meta(incident_key, "handled")
        eligible = [x for x in ranked if x[0] >= min_pass]
        if not eligible:
            return
        pass_rate, pv, validation_id, summary = eligible[0]
        cfg = copy.deepcopy(cur["config"])
        cfg["prompt_version"] = pv
        evidence = {"validation_run_id": validation_id}
        rationale = f"A thresholded population incident flagged {flagged_n}/{connected_n} connected calls ({recent_rate:.1%}) in 24h versus a prior baseline of {prior_rate:.1%}. Selected {pv} deterministically by highest validation pass rate ({pass_rate:.3f}) among suggested options meeting policy minimum {min_pass:.3f}; post-apply QA will be monitored."
        diff = {"prompt_version": {"from": prompt, "to": pv}, "validation_pass_rate": pass_rate,
                "incident": {"rate": recent_rate, "connected": connected_n,
                             "baseline_rate": prior_rate, "baseline_connected": prior_n}}
        self.proposal(wid, cur, cfg, f"Validate safer prompt after hallucination spike: {summary}", rationale, diff,
                      min(1.0, recent_rate*2), evidence)

    def get_variant_stats(self, wid: str, dimension: str, since: str) -> dict[str, dict[str, int]]:
        """Get {variant: {dials, connected, engaged}} for a dimension (voice or welcome_message)."""
        sql = f"""SELECT {dimension}, count(*), sum(connected), sum(engaged) FROM call_stats
            WHERE workflow_id=? AND ts>=? AND {dimension} IS NOT NULL GROUP BY {dimension}"""
        rows = self.db.execute(sql, (wid, since)).fetchall()
        return {r[0]: {"dials": int(r[1]), "connected": int(r[2] or 0), "engaged": int(r[3] or 0)} for r in rows if r[0]}

    def z_test_proportions(self, n1: int, s1: int, n2: int, s2: int) -> float:
        """Z-score for difference in proportions. Positive means variant 1 > variant 2."""
        if n1 < 10 or n2 < 10:
            return 0.0
        p1, p2 = s1 / n1, s2 / n2
        p_pooled = (s1 + s2) / (n1 + n2)
        if p_pooled <= 0 or p_pooled >= 1:
            return 0.0
        se = (p_pooled * (1 - p_pooled) * (1/n1 + 1/n2)) ** 0.5
        return (p1 - p2) / se if se > 0 else 0.0

    def variant_experimentation(self, wid: str, sim_ts: str):
        """Run at most one bounded explore-then-decide experiment per dimension."""
        wf = self.workflows[wid]
        if self.active_proposal(wid):
            return
        local = datetime.fromisoformat(sim_ts)
        begin, end = wf["governance"]["approver_hours"]
        gov = wf["governance"].get("auto_apply", {})
        objective = wf["objective"]["primary"]
        guardrail = wf["objective"].get("guardrail", {})
        guardrail_metric = "pickup" if "pickup_rate_max_drop" in guardrail else "engagement"
        max_drop = float(guardrail.get("pickup_rate_max_drop", guardrail.get("engagement_rate_max_drop", 0.0)))

        for dimension in ("voice", "welcome_message"):
            key = f"experiment:{wid}:{dimension}"
            raw = self.meta(key)
            exp = json.loads(raw) if raw else {}
            if exp.get("status") in ("complete", "abandoned", "cancelled"):
                continue
            cur = self.current_config(wid)
            cfg = copy.deepcopy(cur["config"])
            current_dist = cfg.get("allocations", {}).get("default", {}).get(dimension, {})

            if exp.get("status") == "active":
                # A supervisor edit to this dimension ends the trial; preserve it.
                if current_dist != exp.get("test_dist"):
                    exp["status"] = "cancelled"
                    exp["cancelled_at"] = sim_ts
                    self.set_meta(key, json_dump(exp))
                    self.audit(wid, "experiment_cancelled_supervisor_edit", {"dimension": dimension,
                                "expected": exp.get("test_dist"), "current": current_dist}, sim_ts)
                    continue
                stats = self.get_variant_stats(wid, dimension, exp["started_at"])
                arms = exp.get("arms", [])
                if not arms or any(stats.get(v, {}).get("dials", 0) < MIN_VARIANT_SAMPLE for v in arms):
                    return
                for s in stats.values():
                    s["rate"] = (s["connected"] / s["dials"] if objective == "pickup_rate" and s["dials"]
                                 else s["engaged"] / s["connected"] if s["connected"] else 0.0)
                    s["guardrail_rate"] = (s["connected"] / s["dials"] if guardrail_metric == "pickup" and s["dials"]
                                           else s["engaged"] / s["connected"] if s["connected"] else 0.0)
                eligible = {v: stats[v] for v in arms if v in stats}
                best_variant, best_stats = sorted(eligible.items(), key=lambda x: (-x[1]["rate"], x[0]))[0]
                runner_up, runner_stats = sorted(eligible.items(), key=lambda x: (-x[1]["rate"], x[0]))[1]
                if objective == "pickup_rate":
                    z = self.z_test_proportions(best_stats["dials"], best_stats["connected"], runner_stats["dials"], runner_stats["connected"])
                else:
                    z = self.z_test_proportions(best_stats["connected"], best_stats["engaged"], runner_stats["connected"], runner_stats["engaged"])
                if z < Z_THRESHOLD:
                    exp.update({"status": "complete", "result": "inconclusive", "completed_at": sim_ts, "z_score": round(z, 3)})
                    self.set_meta(key, json_dump(exp))
                    self.audit(wid, "experiment_inconclusive", {"dimension": dimension, "arms": arms, "z_score": round(z, 3)}, sim_ts)
                    continue
                wkey = "dials" if guardrail_metric == "pickup" else "connected"
                total_w = sum(stats[v][wkey] for v in arms)
                baseline_guard = sum(stats[v]["guardrail_rate"] * stats[v][wkey] for v in arms) / total_w if total_w else 0
                if baseline_guard - best_stats["guardrail_rate"] > max_drop:
                    exp.update({"status": "complete", "result": "guardrail_blocked", "completed_at": sim_ts})
                    self.set_meta(key, json_dump(exp))
                    self.audit(wid, "experiment_guardrail_blocked", {"dimension": dimension, "winner": best_variant,
                                "drop": baseline_guard-best_stats["guardrail_rate"]}, sim_ts)
                    continue
                worst = min((v for v in arms if v != best_variant and float(current_dist.get(v, 0)) > 0),
                            key=lambda v: stats[v]["rate"], default=None)
                if not worst:
                    exp.update({"status": "complete", "result": "no_promotable_arm", "completed_at": sim_ts})
                    self.set_meta(key, json_dump(exp))
                    continue
                if not (begin <= local.hour < end):
                    return
                    continue
                auto_allowed = gov.get("enabled") and dimension in gov.get("allowed_dims", [])
                shift = min(CONSERVATIVE_SHIFT, float(gov.get("max_shift", 0.2)) if auto_allowed else CONSERVATIVE_SHIFT,
                            float(current_dist.get(worst, 0)))
                new_dist = dict(current_dist)
                new_dist[worst] = round(float(new_dist[worst])-shift, 6)
                new_dist[best_variant] = round(float(new_dist.get(best_variant, 0))+shift, 6)
                new_dist = {v: p for v, p in new_dist.items() if p > 0}
                cfg["allocations"]["default"][dimension] = new_dist
                lift = best_stats["rate"]-runner_stats["rate"]
                rationale = f"After a randomized {dimension} split, {best_variant} beat {runner_up} with z={z:.2f} (threshold {Z_THRESHOLD:.1f}) on the workflow primary KPI; guardrail passed. Promoting only {shift:.0%} of traffic."
                diff = {dimension: {"action": "promote", "winner": best_variant, "from": worst, "z_score": round(z, 3),
                        "shift": shift, "new_dist": new_dist}, "experiment": {"dimension": dimension, "action": "promote"}}
                if auto_allowed:
                    if self.attempt_auto(wid, cur, cfg, rationale, dimension):
                        exp.update({"status": "complete", "result": "promoted", "completed_at": sim_ts, "winner": best_variant})
                        self.set_meta(key, json_dump(exp))
                        self.audit(wid, "experiment_winner_promoted", {"dimension": dimension, "winner": best_variant, "z_score": round(z, 3)}, sim_ts)
                elif self.proposal(wid, cur, cfg, f"Promote {dimension} experiment winner: {best_variant}", rationale, diff, min(1.0, lift*5)):
                    exp.update({"status": "promoting", "winner": best_variant})
                    self.set_meta(key, json_dump(exp))
                return

            elif exp.get("status") in ("awaiting_approval", "promoting"):
                continue

            # Start one experiment per dimension. Only variants that actually
            # lack 500 recent calls get traffic; no arm is learned from a plan.
            if not (begin <= local.hour < end):
                return
            options = wf["options"].get(dimension, [])
            stats = self.get_variant_stats(wid, dimension, (local-timedelta(days=3)).isoformat())
            if sum(s["dials"] for s in stats.values()) < MIN_TOTAL_SAMPLE or len(options) < 2:
                continue
            incumbent = max(current_dist, key=lambda v: float(current_dist.get(v, 0)), default=None)
            untried = [v for v in options if v != incumbent and stats.get(v, {}).get("dials", 0) < MIN_VARIANT_SAMPLE]
            if not incumbent or not untried:
                continue
            auto_allowed = gov.get("enabled") and dimension in gov.get("allowed_dims", [])
            shift_cap = min(CONSERVATIVE_SHIFT, float(gov.get("max_shift", 0.2))) if auto_allowed else CONSERVATIVE_SHIFT
            per_arm = min(0.05, shift_cap/len(untried))
            shift = per_arm*len(untried)
            if per_arm < 0.01 or float(current_dist.get(incumbent, 0)) <= shift:
                continue
            new_dist = dict(current_dist)
            new_dist[incumbent] = round(float(new_dist[incumbent])-shift, 6)
            for arm in untried:
                new_dist[arm] = round(float(new_dist.get(arm, 0))+per_arm, 6)
            new_dist = {v: p for v, p in new_dist.items() if p > 0}
            cfg["allocations"]["default"][dimension] = new_dist
            arms = list(new_dist)
            diff = {dimension: {"action": "explore", "arms": arms, "new_dist": new_dist},
                    "experiment": {"dimension": dimension, "action": "explore"}}
            rationale = f"Starting one bounded {dimension} experiment with {len(arms)} arms. Traffic is randomized at {new_dist}; each challenger needs {MIN_VARIANT_SAMPLE} observed dials before a deterministic winner test."
            if auto_allowed:
                if self.attempt_auto(wid, cur, cfg, rationale, dimension):
                    state = {"status": "active", "dimension": dimension, "started_at": sim_ts,
                             "test_dist": new_dist, "arms": arms, "base_version": cur["version"]+1}
                    self.set_meta(key, json_dump(state))
                    self.audit(wid, "experiment_started", state, sim_ts)
                    return
            elif self.proposal(wid, cur, cfg, f"Explore {dimension} variants", rationale, diff, 0.25):
                state = {"status": "awaiting_approval", "dimension": dimension, "test_dist": new_dist,
                         "arms": arms, "base_version": cur["version"]+1}
                self.set_meta(key, json_dump(state))
                return

    def poll_approvals(self, sim_ts: str):
        rows = self.db.execute("SELECT * FROM proposals WHERE status IN ('pending','approved','submitting')").fetchall()
        for row in rows:
            pid, wid = row["proposal_id"], row["workflow_id"]
            aid = row["approval_id"]
            if not aid:
                # Resolve crash between remote POST and local approval_id commit.
                try:
                    arr = self.get("/approvals", params={"workflow_id": wid}).json()
                    arr = arr if isinstance(arr, list) else arr.get("approvals", [])
                    found = next((a for a in arr if a.get("proposal_id") == pid), None)
                    if found:
                        aid = found.get("approval_id", found.get("id"))
                        self.db.execute("UPDATE proposals SET approval_id=?,status=?,updated_at=? WHERE proposal_id=?",
                                        (aid, found.get("status", "pending"), now_utc(), pid))
                        self.db.commit()
                    else:
                        continue
                except httpx.HTTPError:
                    continue
            try:
                r = self.get(f"/approvals/{aid}")
            except httpx.HTTPError:
                continue
            if r.status_code >= 400:
                continue
            approval = r.json()
            status = approval.get("status", "pending")
            payload = json.loads(row["payload"])
            marker = payload.get("diff", {}).get("experiment")
            if marker and marker.get("action") == "explore" and status == "pending":
                exp_key = f"experiment:{wid}:{marker['dimension']}"
                if not self.meta(exp_key):
                    self.set_meta(exp_key, json_dump({"status": "awaiting_approval", "dimension": marker["dimension"],
                        "test_dist": payload.get("proposed_config", {}).get("allocations", {}).get("default", {}).get(marker["dimension"], {}),
                        "arms": marker.get("arms", []), "base_version": row["base_version"], "proposal_id": pid}))
            if status == "approved":
                apply = self.post(f"/workflows/{wid}/apply", json={"approval_id": aid})
                if apply.status_code == 200:
                    self.db.execute("UPDATE proposals SET status='applied',updated_at=? WHERE proposal_id=?", (now_utc(), pid))
                    self.db.commit()
                    result = apply.json()
                    self.start_prompt_verification(wid, result.get("version"), payload, aid, sim_ts)
                    self.transition_experiment(wid, result.get("version"), payload, sim_ts)
                    self.audit(wid, "approved_change_applied", {"proposal_id": pid, "approval_id": aid,
                                "result": result, "rationale": json.loads(row["payload"]).get("rationale")}, sim_ts)
                else:
                    label = "stale_reproposal_needed" if apply.status_code == 409 else "apply_failed"
                    self.db.execute("UPDATE proposals SET status=?,updated_at=? WHERE proposal_id=?", (label, now_utc(), pid))
                    self.db.commit()
                    self.audit(wid, label, {"proposal_id": pid, "approval_id": aid, "status_code": apply.status_code,
                                "response": apply.text[:500]}, sim_ts)
                    self.abandon_experiment(wid, payload, label, sim_ts)
            elif status == "applied":
                self.db.execute("UPDATE proposals SET status='applied',updated_at=? WHERE proposal_id=?", (now_utc(), pid))
                self.db.commit()
                cfg = payload.get("proposed_config", {})
                cur = self.current_config(wid)
                self.start_prompt_verification(wid, cur.get("version"), payload, aid, sim_ts)
                self.transition_experiment(wid, cur.get("version"), payload, sim_ts)
                self.audit(wid, "approved_apply_reconciled", {"proposal_id": pid, "approval_id": aid,
                            "version": cur.get("version"), "prompt_version": cfg.get("prompt_version")}, sim_ts)
            elif status in ("rejected", "expired", "lapsed"):
                self.db.execute("UPDATE proposals SET status=?,updated_at=? WHERE proposal_id=?", (status, now_utc(), pid))
                self.db.commit()
                self.audit(wid, "proposal_terminal", {"proposal_id": pid, "approval_id": aid, "status": status,
                            "reasons": approval.get("reasons", [])}, sim_ts)
                self.abandon_experiment(wid, payload, status, sim_ts)

    def transition_experiment(self, wid: str, version: int | None, payload: dict[str, Any], sim_ts: str):
        marker = payload.get("diff", {}).get("experiment")
        if not marker:
            return
        dimension, action = marker.get("dimension"), marker.get("action")
        if dimension not in ("voice", "welcome_message"):
            return
        key = f"experiment:{wid}:{dimension}"
        raw = self.meta(key)
        state = json.loads(raw) if raw else {}
        if action == "explore":
            state = {"status": "active", "dimension": dimension, "started_at": sim_ts,
                     "test_dist": payload.get("proposed_config", {}).get("allocations", {}).get("default", {}).get(dimension, {}),
                     "arms": marker.get("arms", []), "base_version": version}
            self.set_meta(key, json_dump(state))
            self.audit(wid, "experiment_started", state, sim_ts)
        elif action == "promote":
            state.update({"status": "complete", "result": "promoted", "winner": marker.get("winner"), "completed_at": sim_ts})
            self.set_meta(key, json_dump(state))

    def abandon_experiment(self, wid: str, payload: dict[str, Any], reason: str, sim_ts: str):
        marker = payload.get("diff", {}).get("experiment")
        if marker and marker.get("dimension") in ("voice", "welcome_message"):
            key = f"experiment:{wid}:{marker['dimension']}"
            raw = self.meta(key)
            state = json.loads(raw) if raw else {"dimension": marker["dimension"]}
            state.update({"status": "abandoned", "reason": reason, "completed_at": sim_ts})
            self.set_meta(key, json_dump(state))

    def start_prompt_verification(self, wid: str, version: int | None, payload: dict[str, Any], approval_id: str, sim_ts: str):
        diff = payload.get("diff", {})
        change = diff.get("prompt_version")
        incident = diff.get("incident")
        if not version or not isinstance(change, dict) or not incident:
            return
        key = f"prompt_verify:{wid}:{version}"
        if self.meta(key):
            return
        detail = {"from": change.get("from"), "to": change.get("to"), "approval_id": approval_id,
                  "baseline_rate": incident.get("rate"), "baseline_n": incident.get("connected"),
                  "prior_rate": incident.get("baseline_rate"), "prior_n": incident.get("baseline_connected"),
                  "applied_at": sim_ts, "version": version}
        self.set_meta(key, json_dump(detail))

    def verify_prompt_changes(self, sim_ts: str):
        for row in self.db.execute("SELECT key,value FROM meta WHERE key LIKE 'prompt_verify:%'").fetchall():
            detail = json.loads(row["value"])
            if detail.get("verified"):
                continue
            result = self.db.execute("""SELECT count(*),sum(hallucination_flag) FROM call_stats
                WHERE workflow_id=? AND config_version=? AND prompt_version=? AND ts>? AND connected=1""",
                (row["key"].split(":")[1], detail["version"], detail["to"], detail["applied_at"])).fetchone()
            n, flagged = int(result[0] or 0), int(result[1] or 0)
            elapsed = datetime.fromisoformat(sim_ts) - datetime.fromisoformat(detail["applied_at"])
            if n < 100 or elapsed < timedelta(hours=24):
                continue
            rate = flagged / n
            base, base_n = float(detail.get("baseline_rate") or 0), max(1, int(detail.get("baseline_n") or 1))
            se = ((base*(1-base)/base_n) + (rate*(1-rate)/n)) ** 0.5
            if base-rate >= 2*se:
                status = "improved"
            elif rate-base >= 2*se:
                status = "regressed"
            else:
                status = "inconclusive"
            detail.update({"verified": True, "verified_at": sim_ts, "post_connected": n,
                           "post_flagged": flagged, "post_rate": rate, "status": status})
            self.set_meta(row["key"], json_dump(detail))
            self.audit(row["key"].split(":")[1], "prompt_post_apply_verification", detail, sim_ts)

    def status_line(self, state: dict[str, Any]):
        if state["sim_minute"] % 360 == 0 or state["finished"]:
            live_state = self.get("/sim/state").json()
            credits = live_state.get("credits_remaining")
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = usage / (1024 * 1024 if sys.platform == "darwin" else 1024)
            print(json_dump({"sim_ts": state["ts"], "events_ingested_total": self.meta("events_ingested_total", "0"),
                             "events_retained": self.db.execute("SELECT count(*) FROM events").fetchone()[0] + self.db.execute("SELECT count(*) FROM call_audit").fetchone()[0],
                             "new_events": self.stats["events"], "chunks": self.stats["chunks"], "pending": self.db.execute("SELECT count(*) FROM proposals WHERE status IN ('pending','approved','submitting')").fetchone()[0],
                             "credits_remaining": credits, "decisions": self.stats["decisions"], "api_errors": self.stats["api_errors"],
                             "wall_seconds": round(time.monotonic()-self.started, 2), "rss_mb": round(rss_mb, 2)}), flush=True)

    def run(self):
        state = self.initialize()
        self.synchronize_events()
        self.poll_approvals(state["ts"])
        while not state.get("finished"):
            # Advance one hour, then ingest and act on thresholded populations.
            try:
                r = self.post("/sim/advance", json={"minutes": 60})
                if r.status_code == 409:
                    state = self.get("/sim/state").json()
                    break
                r.raise_for_status()
                state = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.audit(None, "advance_error", {"error": str(exc)})
                time.sleep(2)
                state = self.get("/sim/state").json()
                continue
            self.synchronize_events()
            self.poll_approvals(state["ts"])
            for wid in self.workflows:
                try:
                    self.caller_id_detection(wid, state["ts"])
                    self.hallucination_detection(wid, state["ts"])
                    self.variant_experimentation(wid, state["ts"])
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    self.stats["api_errors"] += 1
                    self.audit(wid, "workflow_decision_error", {"error": str(exc)}, state["ts"])
            self.verify_prompt_changes(state["ts"])
            self.status_line(state)
            delay = float(os.environ.get("CONTROLLER_TICK_DELAY", "0"))
            if delay > 0:
                time.sleep(delay)
        self.synchronize_events()
        self.poll_approvals(state["ts"])
        final = self.get("/sim/state").json()
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.peak_rss_mb = usage / (1024 * 1024 if sys.platform == "darwin" else 1024)
        self.audit(None, "controller_finished", {"sim_ts": final["ts"], "finished": final["finished"],
                    "events_ingested": self.meta("events_ingested_total", "0"),
                    "credits_remaining": final.get("credits_remaining"), "wall_seconds": round(time.monotonic()-self.started, 3),
                    "peak_rss_mb": round(self.peak_rss_mb, 2), "metrics": self.stats}, final["ts"])
        stored_total = int(self.meta("events_ingested_total", "0"))
        retained_total = self.db.execute("SELECT count(*) FROM events").fetchone()[0] + self.db.execute("SELECT count(*) FROM call_audit").fetchone()[0]
        self.db.close()
        print(json_dump({"finished": final.get("finished"), "sim_ts": final["ts"], "scenario": self.run_id,
                         "database": os.environ.get("CONTROLLER_DB", "runs/controller.sqlite"),
                         "events_ingested": stored_total, "events_retained": retained_total,
                         "wall_seconds": round(time.monotonic()-self.started, 3), "peak_rss_mb": round(self.peak_rss_mb, 2),
                         "decisions": self.stats["decisions"],
                         "credits_remaining": final.get("credits_remaining")}), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("SIM_URL", "http://127.0.0.1:8700"))
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "runs/controller.sqlite"))
    args = ap.parse_args()
    Controller(args.url, Path(args.db)).run()


if __name__ == "__main__":
    main()
