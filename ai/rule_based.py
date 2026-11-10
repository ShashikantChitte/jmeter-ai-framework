#!/usr/bin/env python3
"""Offline 'analyst': deterministic rules that turn evidence into findings + hypotheses.
Same output schema as the LLM answer, so the pipeline works with no API key. Hypotheses are NEVER stated as facts."""
import re


def _h(cause, conf, verify):
    return {"cause": cause, "confidence": conf, "verify": verify}


ERROR_RULES = [
    (r"timed? ?out|timeout|read timed", "Timeouts: a dependency or the app is slower than the client timeout (queueing, slow DB query, GC pause, thread/connection pool exhausted).",
     "Take 3-5 thread dumps during the failures; check pool wait metrics, slow query log and GC pauses at the same timestamps."),
    (r"connection refused|connection reset|broken pipe|connect exception|no route", "Connection-level failures: listener backlog/max connections reached, pod restarts, LB limits or firewall.",
     "Check pod restarts/OOMKilled, LB connection limits, `ss -s` on the server, ulimit -n, and the time alignment with the knee point."),
    (r"\b401\b|unauthori|\b403\b|forbidden|token", "Authentication problem: token expired/not refreshed, wrong secret, or rate-limited login.",
     "Compare error start time with token TTL; check auth_mode, OAUTH_CLIENT_SECRET and the auth service logs."),
    (r"\b429\b|too many requests|rate limit", "Rate limiting is active (API gateway/WAF) - the test may be measuring the limiter, not the system.",
     "Check gateway limits and whether the load-generator IPs/keys are being throttled; look for Retry-After headers."),
    (r"\b50[234]\b|service unavailable|bad gateway|gateway time", "Upstream overload or restarts behind the load balancer (502/503/504).",
     "Check LB target health, idle-timeout vs keep-alive mismatch, pod restarts and autoscaling events."),
    (r"not_found|jsonpath|no match|extractor|correlat", "Correlation/test-script problem: an extracted value was missing, so later requests are invalid (test defect, not an app defect).",
     "Open errors.jtl / run 1 user in the GUI with View Results Tree; verify the extractor expression against the real response."),
    (r"assert|expected|does not match", "Assertion failures: functional errors under load (wrong body, 4xx/5xx) rather than slowness.",
     "Inspect failureMessage and the response body of failed samples; reproduce with a single user."),
    (r"\b500\b|internal server error|nullpointer|unhandled", "Server-side errors (unhandled exceptions) under load: race conditions, resource exhaustion, or a defect.",
     "Correlate with application logs/APM errors at the same minute; look for pool exhaustion or null/concurrency bugs."),
]


def analyze(ev):
    findings, nxt, notes = [], [], []
    fid = 0

    def add(sev, title, evidence, hyps):
        nonlocal fid
        fid += 1
        findings.append({"id": f"F{fid}", "severity": sev, "title": title, "evidence": evidence, "hypotheses": hyps})

    ov = ev["overall"]
    # --- validity
    sat = ev.get("generator_saturated_hosts", [])
    for h in sat:
        s = ev["load_generators"][h]
        notes.append(f"Load generator {h} looks saturated (CPU>80% in {s['share_samples_cpu_over_80_pct']}% of samples, "
                     f"GC {s['gc_time_share_pct']}% of time): client-side latency may be inflated.")
    if sat:
        add("high", "Load generator saturation may invalidate the results",
            [f"{h}: max CPU {ev['load_generators'][h]['sys_cpu_max_pct']}%, heap peak {ev['load_generators'][h]['heap_peak_pct_of_max']}%" for h in sat],
            [_h("The generator, not the system under test, is the bottleneck.", "high",
                "Compare client-side vs server-side (APM) latency; rerun with more workers so each stays < 70% CPU.")])
        nxt.append("Rerun with more workers (or fewer users per worker) until generator CPU stays below 70%.")
    if ev["steady_state_seconds"] < 300:
        notes.append(f"Steady state is only {ev['steady_state_seconds']}s - too short for reliable percentiles (use >= 30 min for load tests).")

    # --- SLA breaches
    fails = [r for r in ev["sla_table"] if r["status"] == "FAIL"]
    for r in fails:
        slow = r["p95_ms"] > r["p95_threshold_ms"]
        errs = r["error_pct"] > r["error_threshold_pct"]
        evid = [f"p95 {r['p95_ms']} ms vs threshold {r['p95_threshold_ms']} ms", f"error rate {r['error_pct']}% vs {r['error_threshold_pct']}%",
                f"{r['samples']} samples, {r['tps']} tps"]
        hyps = []
        if slow and not errs:
            hyps.append(_h("Waiting rather than computing: slow query/lock wait, slow downstream call or pool queueing.", "medium",
                           "Thread dumps (BLOCKED/WAITING groups), pool wait metrics, slow query log, trace the slowest requests."))
            hyps.append(_h("GC pauses or CPU throttling add tail latency.", "low", "Check GC log pauses and container throttling for the same minutes."))
        if errs:
            hyps.append(_h("Saturation: a hard limit (connections, threads, rate limit) is reached.", "medium",
                           "Check pool/connection limits and 4xx/5xx breakdown in error_clusters."))
        add("high", f"SLA breach: {r['transaction']}", evid, hyps)
    if fails:
        nxt.append("Run bottleneck analysis for the failing transactions: thread dumps x5, GC log, slow queries and a trace sample at the failing load level.")

    # --- regressions
    for r in ev["sla_table"]:
        if r["status"] == "WARN":
            add("medium", f"Regression vs baseline: {r['transaction']}",
                [f"p95 {r['p95_ms']} ms is {r['p95_vs_baseline_pct']}% above baseline (SLA still met)"],
                [_h("A recent code/config/data change slowed this path.", "medium", "Diff the builds between baseline and this run; profile this transaction (JFR/async-profiler).")])
    # --- thin headroom
    for r in ev["sla_table"]:
        if r["status"] == "PASS" and r["p95_headroom_pct"] < 10:
            add("low", f"Thin SLA headroom: {r['transaction']}", [f"p95 {r['p95_ms']} ms is only {r['p95_headroom_pct']}% below the {r['p95_threshold_ms']} ms threshold"],
                [_h("Little capacity margin; small growth or noise will breach the SLA.", "high", "Run a capacity test and plan optimisation or scaling.")])

    # --- knee
    k = ev.get("knee_point")
    if k:
        add("medium", "Saturation (knee) point detected",
            [f"Throughput peaked at {k['peak_tps']} tps with ~{k['threads_at_peak_throughput']} concurrent users (second {k['peak_at_second']})",
             f"Beyond it, load rose to {k['max_threads_reached']} users: throughput {k['tps_after_peak']} tps, p95 {k['p95_ms_at_peak']} -> {k['p95_ms_after_peak']} ms, errors {k['err_pct_after_peak']}%"],
            [_h("A shared resource saturates near this load (DB connections, thread pool, CPU or a lock).", "medium",
                "Capture server CPU, pool and DB metrics at that timestamp; rerun a stepped test to confirm.")])
        nxt.append(f"Record ~{k['threads_at_peak_throughput']} users as the current capacity estimate and compare with the planned peak (keep >= 30% headroom).")

    # --- episodes / drift
    for e in ev.get("degradation_episodes", [])[:5]:
        add("medium", f"Latency degradation episode at {e['start_s']//60} min",
            [f"p95 reached {e['peak']} ms ({e['ratio_vs_baseline']}x steady baseline {round(e['baseline'])} ms) for ~{e['end_s']-e['start_s']} s"],
            [_h("Periodic work (GC, batch job, cache expiry, autoscaling event, failover) interrupted steady state.", "medium",
                "Align the timestamp with GC log, cron/batch schedules, HPA events and cache metrics.")])
    d = ev.get("latency_drift")
    if d and d.get("significant"):
        add("high", "Latency drifts over time at steady load",
            [f"p95 trend {d['slope_pct_per_hour']}%/hour (r2={d['r2']}) over {d['duration_min']} min"],
            [_h("Resource leak or accumulation (memory, threads, connections, DB bloat, cache growth).", "medium",
                "Compare heap-after-GC, thread count, pool usage and DB table/index size at start vs end; take heap dumps at intervals.")])
        nxt.append("Run/extend a soak test with heap dumps at start and end and compare in Eclipse MAT.")
    for h, s in ev.get("load_generators", {}).items():
        fd = s.get("heap_floor_drift")
        if fd and fd.get("significant"):
            notes.append(f"Generator {h} heap floor drifts {fd['slope_pct_per_hour']}%/h - reduce stored data/listeners in JMeter.")

    # --- error clusters
    for c in ev.get("error_clusters", [])[:5]:
        text = f"{c['code']} {c['message']}".lower()
        hyps = [_h(cause, "medium", verify) for pat, cause, verify in ERROR_RULES if re.search(pat, text)][:2]
        if hyps:
            add("high" if c["count"] > 50 else "medium", f"Error cluster: {c['label']} ({c['count']}x, code {c['code'] or 'n/a'})",
                [f"normalized message: {c['message'][:100]}"], hyps)
    if ov["error_pct"] > 1 and not ev.get("error_clusters"):
        notes.append("Errors present but no error details were captured (errors.jtl missing).")

    # --- verdict
    if sat:
        verdict = "RESULTS_UNRELIABLE"
    elif ev["gate"] == "FAIL":
        verdict = "FAIL"
    elif ev["gate"] == "WARN" or any(f["severity"] in ("high", "medium") for f in findings):
        verdict = "PASS_WITH_RISKS"
    else:
        verdict = "PASS"
    n_tx = len(ev["sla_table"])
    summary = (f"Gate: {ev['gate']}. {n_tx} transactions measured at {ov['tps']} tps (steady {ev['steady_state_seconds']} s, up to "
               f"{ov['max_concurrent_threads']} concurrent users), overall error rate {ov['error_pct']}%. "
               + (f"{len(fails)} transaction(s) breach their SLA. " if fails else "All transactions meet their SLAs. ")
               + (f"Capacity knee at ~{k['threads_at_peak_throughput']} users. " if k else "")
               + (f"{len(findings)} finding(s) need attention." if findings else "No anomalies detected."))
    if not nxt:
        nxt.append("No action required; keep this run as the baseline once approved (cp results/summary.json baseline/summary.json).")
    return {"verdict": verdict, "executive_summary": summary, "findings": findings,
            "recommended_next_steps": nxt, "test_validity_notes": notes, "source": "rule-based"}
