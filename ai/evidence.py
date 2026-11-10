#!/usr/bin/env python3
"""Evidence extraction + statistical anomaly detection from JMeter results (standard library only).
This is the deterministic 'ML-lite' layer: knee-point, degradation episodes (robust z-score), drift (regression),
error clustering, load-generator validity. Its output feeds both the offline analyst and the LLM."""
import csv, json, math, os, re, statistics
from collections import Counter, defaultdict

LABEL_RE = re.compile(r"^(TX|AUTH|PROTO)")


def pct(v, p):
    v = sorted(v)
    return v[max(int(len(v) * p / 100.0 + 0.5) - 1, 0)] if v else 0


def read_props(path):
    d = {}
    if path and os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def load_jtl(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if not LABEL_RE.search(r["label"]):
                continue
            rows.append((int(r["timeStamp"]), int(r["elapsed"]), r["label"], r["success"] == "true",
                         int(r.get("allThreads") or 0)))
    rows.sort(key=lambda x: x[0])
    return rows


def timeline(rows, window_s):
    if not rows:
        return []
    t0 = rows[0][0]
    buckets = defaultdict(list)
    for r in rows:
        buckets[(r[0] - t0) // (window_s * 1000)].append(r)
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        el = [x[1] for x in b]
        out.append({"t": k * window_s, "tps": round(len(b) / window_s, 2), "p95": pct(el, 95), "p99": pct(el, 99),
                    "avg": round(sum(el) / len(el), 1), "err_pct": round(100.0 * sum(1 for x in b if not x[3]) / len(b), 2),
                    "threads": max(x[4] for x in b)})
    if len(out) > 2:          # last window is usually partial
        out = out[:-1]
    return out


def downsample(tl, n=48):
    if len(tl) <= n:
        return tl
    step = math.ceil(len(tl) / n)
    return [tl[i] for i in range(0, len(tl), step)]


def detect_knee(tl):
    """Throughput plateau while load keeps rising and latency/errors climb => saturation point."""
    if len(tl) < 8:
        return None
    sm = [statistics.mean(w["tps"] for w in tl[max(0, i - 1):i + 2]) for i in range(len(tl))]
    top = max(sm)
    best = next(i for i, v in enumerate(sm) if v >= 0.97 * top)     # first window reaching the plateau
    peak = tl[best]
    max_threads = max(w["threads"] for w in tl)
    if max_threads == 0 or peak["threads"] >= 0.97 * max_threads:
        return None
    later = [w for w in tl[best + 1:] if w["threads"] >= peak["threads"] * 1.15]
    if len(later) < 2:
        return None
    tps_after = statistics.mean(w["tps"] for w in later)
    p95_after = statistics.mean(w["p95"] for w in later)
    err_after = statistics.mean(w["err_pct"] for w in later)
    p95_at = statistics.mean(w["p95"] for w in tl[max(0, best - 1):best + 1])
    if tps_after > top * 1.05 or (p95_after < p95_at * 1.3 and err_after < 2):
        return None
    return {"threads_at_peak_throughput": peak["threads"], "peak_tps": round(top, 2), "p95_ms_at_peak": p95_at,
            "max_threads_reached": max_threads, "tps_after_peak": round(tps_after, 2),
            "p95_ms_after_peak": round(p95_after), "err_pct_after_peak": round(err_after, 2),
            "peak_at_second": peak["t"]}


def detect_episodes(tl, key="p95", k=4.0, window=None):
    if len(tl) < 8:
        return []
    window = window or (tl[1]["t"] - tl[0]["t"])
    mt = max(w["threads"] for w in tl)
    steady = [w for w in tl if w["threads"] >= 0.9 * mt] if mt else tl
    if len(steady) < 8:
        return []
    vals = [w[key] for w in steady]
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) or max(med * 0.05, 1)
    thr = med + k * 1.4826 * mad
    eps, cur = [], None
    for w in steady:
        if w[key] > thr:
            if cur and w["t"] - cur["end_s"] <= window:
                cur["end_s"] = w["t"] + window
                cur["peak"] = max(cur["peak"], w[key])
            else:
                cur = {"start_s": w["t"], "end_s": w["t"] + window, "peak": w[key], "baseline": med}
                eps.append(cur)
    for e in eps:
        e["ratio_vs_baseline"] = round(e["peak"] / e["baseline"], 2) if e["baseline"] else None
        e["metric"] = key
    return eps


def detect_drift(points, min_points=10, min_minutes=30):
    n = len(points)
    if n < min_points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0 or my == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in points) / sxx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (my + slope * (x - mx))) ** 2 for x, y in points)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    dur_min = (xs[-1] - xs[0]) / 60.0
    res = {"slope_pct_per_hour": round(slope * 3600 / my * 100, 2), "r2": round(r2, 2), "duration_min": round(dur_min, 1)}
    res["significant"] = bool(dur_min >= min_minutes and abs(res["slope_pct_per_hour"]) >= 5 and r2 >= 0.5)
    return res


def normalize_msg(s):
    s = (s or "").lower()
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", s)
    s = re.sub(r"\b[0-9a-f]{16,}\b", "<hex>", s)
    s = re.sub(r"\d+", "N", s)
    return s.strip()[:140]


def error_clusters(paths, top=10):
    c = Counter()
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r.get("success") == "true" or not LABEL_RE.search(r.get("label", "")):
                    continue
                msg = normalize_msg(" | ".join(x for x in ((r.get("responseMessage") or "").strip(), (r.get("failureMessage") or "").strip()) if x))
                c[(r["label"], r.get("responseCode", ""), msg)] += 1
        if c:
            break          # first file that has errors is enough (errors.jtl duplicates results.jtl failures)
    return [{"label": k[0], "code": k[1], "message": k[2], "count": n} for k, n in c.most_common(top)]


def _num(v):
    try:
        x = float(v)
        return None if x < 0 else x
    except Exception:
        return None


def lg_summary(path):
    if not path or not os.path.exists(path):
        return {}
    by = defaultdict(list)
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            by[r["host"]].append(r)
    out = {}
    for host, rs in by.items():
        sc = [_num(r["sys_cpu"]) for r in rs if _num(r["sys_cpu"]) is not None]
        pc = [_num(r["proc_cpu"]) for r in rs if _num(r["proc_cpu"]) is not None]
        hu = [(int(r["timestamp"]) / 1000.0, float(r["heap_used_mb"])) for r in rs]
        hm = max(float(r["heap_max_mb"]) for r in rs) or 1
        gc0, gc1 = int(rs[0]["gc_time_ms"]), int(rs[-1]["gc_time_ms"])
        span_ms = max(int(rs[-1]["timestamp"]) - int(rs[0]["timestamp"]), 1)
        # heap "floor" per 5-minute window ~ post-GC level => leak detection on the generator itself
        floor = defaultdict(list)
        for t, v in hu:
            floor[int((t - hu[0][0]) // 300)].append(v)
        drift = detect_drift([(k * 300.0, min(v)) for k, v in sorted(floor.items())], 6, 30)
        out[host] = {
            "samples": len(rs),
            "sys_cpu_mean_pct": round(100 * statistics.mean(sc), 1) if sc else None,
            "sys_cpu_max_pct": round(100 * max(sc), 1) if sc else None,
            "share_samples_cpu_over_80_pct": round(100.0 * sum(1 for x in sc if x > 0.8) / len(sc), 1) if sc else None,
            "proc_cpu_max_pct": round(100 * max(pc), 1) if pc else None,
            "heap_peak_pct_of_max": round(100 * max(v for _, v in hu) / hm, 1),
            "gc_time_share_pct": round(100.0 * (gc1 - gc0) / span_ms, 2),
            "open_fds_max": max(int(float(r["open_fds"])) for r in rs),
            "threads_max": max(int(float(r["threads"])) for r in rs),
            "rx_mb_s_max": round(max(float(r["rx_mb_s"]) for r in rs), 2),
            "tx_mb_s_max": round(max(float(r["tx_mb_s"]) for r in rs), 2),
            "heap_floor_drift": drift,
        }
    return out


def sla_table(summary, thresholds, baseline):
    default = thresholds.get("default", {"p95_ms": 2000, "error_pct": 1.0})
    per = thresholds.get("transactions", {})
    tol = thresholds.get("regression_tolerance_pct", 15)
    out = []
    for label, s in sorted(summary.items()):
        t = {**default, **per.get(label, {})}
        status, notes = "PASS", []
        if s["p95_ms"] > t["p95_ms"]:
            status = "FAIL"; notes.append("p95 above threshold")
        if "p99_ms" in t and s["p99_ms"] > t["p99_ms"]:
            status = "FAIL"; notes.append("p99 above threshold")
        if s["error_pct"] > t["error_pct"]:
            status = "FAIL"; notes.append("error rate above threshold")
        b = baseline.get(label)
        delta = None
        if b:
            delta = round(100.0 * (s["p95_ms"] - b["p95_ms"]) / b["p95_ms"], 1) if b["p95_ms"] else None
            if status == "PASS" and delta is not None and delta > tol:
                status = "WARN"; notes.append(f"p95 regressed {delta}% vs baseline")
        out.append({"transaction": label, "samples": s["samples"], "p95_ms": s["p95_ms"], "p99_ms": s["p99_ms"],
                    "error_pct": s["error_pct"], "tps": s["tps"], "p95_threshold_ms": t["p95_ms"],
                    "p95_headroom_pct": round(100.0 * (t["p95_ms"] - s["p95_ms"]) / t["p95_ms"], 1),
                    "error_threshold_pct": t["error_pct"], "p95_vs_baseline_pct": delta,
                    "status": status, "notes": notes})
    return out


def build_evidence(results_dir, thresholds_path, baseline_path=None, profile_path=None, skip_sec=None):
    jtl = os.path.join(results_dir, "results.jtl")
    if not os.path.exists(jtl):
        raise SystemExit(f"{jtl} not found")
    props = read_props(profile_path)
    skip = int(skip_sec if skip_sec is not None else props.get("rampup", 0) or 0)
    rows = load_jtl(jtl)
    if not rows:
        raise SystemExit("no TX/AUTH/PROTO samples in results.jtl")
    t0 = rows[0][0]
    steady = [r for r in rows if r[0] - t0 >= skip * 1000] or rows
    total_s = (rows[-1][0] - t0) / 1000.0
    dur = max((steady[-1][0] - steady[0][0]) / 1000.0, 1)
    per = defaultdict(list)
    err = Counter()
    for r in steady:
        per[r[2]].append(r[1])
        err[r[2]] += 0 if r[3] else 1
    summary = {lab: {"samples": len(v), "p95_ms": pct(v, 95), "p99_ms": pct(v, 99),
                     "error_pct": round(100.0 * err[lab] / len(v), 3), "tps": round(len(v) / dur, 2)}
               for lab, v in per.items()}
    thresholds = json.load(open(thresholds_path)) if thresholds_path and os.path.exists(thresholds_path) else {}
    baseline = {}
    if baseline_path and os.path.exists(baseline_path):
        baseline = json.load(open(baseline_path)).get("transactions", {})
    window = 60 if total_s >= 1800 else (15 if total_s >= 300 else 5)
    tl = timeline(rows, window)
    all_err = sum(err.values())
    run_info_path = os.path.join(results_dir, "run_info.json")
    ev = {
        "run": json.load(open(run_info_path)) if os.path.exists(run_info_path) else {},
        "load_profile": {k: props[k] for k in ("users", "rampup", "duration", "checkout_tps") if k in props},
        "window_seconds": window,
        "steady_state_seconds": round(dur),
        "overall": {"samples": len(steady), "tps": round(len(steady) / dur, 2),
                    "error_pct": round(100.0 * all_err / len(steady), 3),
                    "max_concurrent_threads": max((r[4] for r in rows), default=0)},
        "sla_table": sla_table(summary, thresholds, baseline),
        "knee_point": detect_knee(tl),
        "degradation_episodes": detect_episodes(tl),
        "latency_drift": detect_drift([(w["t"], w["p95"]) for w in tl if w["threads"] >= 0.9 * max(x["threads"] for x in tl)]),
        "error_clusters": error_clusters([os.path.join(results_dir, "errors.jtl"), jtl]),
        "load_generators": lg_summary(os.path.join(results_dir, "lg_health.csv")),
        "timeline_downsampled": downsample(tl),
    }
    sat = [h for h, s in ev["load_generators"].items()
           if (s.get("share_samples_cpu_over_80_pct") or 0) >= 10 or (s.get("gc_time_share_pct") or 0) > 10]
    ev["generator_saturated_hosts"] = sat
    ev["gate"] = "FAIL" if any(r["status"] == "FAIL" for r in ev["sla_table"]) else (
        "WARN" if any(r["status"] == "WARN" for r in ev["sla_table"]) else "PASS")
    return ev
