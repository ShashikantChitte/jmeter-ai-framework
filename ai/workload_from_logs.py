#!/usr/bin/env python3
"""Derive a realistic workload model from production access logs  ->  config/load_profiles/from_logs.properties

  python3 ai/workload_from_logs.py access.log --growth 1.3 --buffer 1.2 [--ai]

What it computes (deterministic, standard library):
  * peak window (busiest N minutes) and requests/s
  * user SESSIONS (client key + 30 min inactivity) -> peak concurrent sessions = concurrent users
  * transaction mix by session 'depth' (checkout > cart > search > history/browse)  == the framework's thread groups
  * think time from real inter-request gaps, checkout rate at peak
Supported: nginx/Apache combined (+ optional request_time) and JSON lines.  --ai asks an LLM only to classify UNMAPPED endpoints."""
import argparse, json, math, os, re, statistics, sys
from collections import Counter, defaultdict
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

COMBINED = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) \S+'
                      r'(?: "[^"]*" "(?P<ua>[^"]*)")?(?P<rest>.*)$')
RT = re.compile(r'(?:rt|request_time|urt)=([\d.]+)')
_ts_cache = {}


def parse_ts(s):
    if s not in _ts_cache:
        try:
            _ts_cache[s] = datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z").timestamp()
        except ValueError:
            _ts_cache[s] = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    return _ts_cache[s]


def norm_path(p):
    p = p.split("?")[0]
    p = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{id}", p, flags=re.I)
    p = re.sub(r"/\d+(?=/|$)", "/{id}", p)
    p = re.sub(r"/(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9\-]{9,}(?=/|$)", "/{id}", p)   # long token containing a digit
    return p


def parse_line(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            j = json.loads(line)
            ts = j.get("time") or j.get("timestamp") or j.get("@timestamp")
            path = j.get("path") or j.get("uri") or j.get("url") or j.get("request_uri")
            if ts is None or path is None:
                return None
            t = float(ts) if isinstance(ts, (int, float)) else parse_ts(str(ts))
            client = str(j.get("client") or j.get("user") or j.get("ip") or j.get("remote_addr") or "") + "|" + str(j.get("ua") or "")
            return {"t": t, "method": (j.get("method") or "GET").upper(), "path": path, "status": int(j.get("status", 200)),
                    "client": client, "rt": float(j.get("request_time") or j.get("duration") or 0) or None}
        except Exception:
            return None
    m = COMBINED.match(line)
    if not m:
        return None
    rt = RT.search(m.group("rest") or "")
    return {"t": parse_ts(m.group("ts")), "method": m.group("method"), "path": m.group("path"), "status": int(m.group("status")),
            "client": m.group("ip") + "|" + (m.group("ua") or ""), "rt": float(rt.group(1)) if rt else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log"); ap.add_argument("--map", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "endpoint_map.json"))
    ap.add_argument("--out", default="config/load_profiles/from_logs.properties")
    ap.add_argument("--peak-window-min", type=int, default=15)
    ap.add_argument("--growth", type=float, default=1.3); ap.add_argument("--buffer", type=float, default=1.2)
    ap.add_argument("--rampup", type=int, default=600); ap.add_argument("--hold", type=int, default=3600)
    ap.add_argument("--session-gap", type=int, default=1800)
    ap.add_argument("--ai", action="store_true")
    a = ap.parse_args()

    cfg = json.load(open(a.map))
    rules = [(r["scenario"], re.compile(r["pattern"])) for r in cfg["rules"]]
    ignore = [re.compile(p) for p in cfg.get("ignore", [])]
    events, unmapped, total = [], Counter(), 0
    with open(a.log, errors="replace") as f:
        for line in f:
            r = parse_line(line)
            if not r:
                continue
            total += 1
            np_ = norm_path(r["path"])
            if any(p.search(np_) for p in ignore):
                continue
            sc = next((s for s, p in rules if p.search(np_)), None)
            if sc is None:
                unmapped[f"{r['method']} {np_}"] += 1
                continue
            r.update(scenario=sc, npath=np_)
            events.append(r)
    if not events:
        sys.exit("no mapped requests found - check the log format and endpoint_map.json")
    events.sort(key=lambda e: e["t"])

    # ---- peak window
    t0 = int(events[0]["t"] // 60)
    per_min = Counter(int(e["t"] // 60) - t0 for e in events)
    W = a.peak_window_min
    n_min = max(per_min) + 1
    best, best_sum = 0, -1
    run = sum(per_min.get(i, 0) for i in range(W))
    for s in range(0, max(n_min - W, 0) + 1):
        if s:
            run += per_min.get(s + W - 1, 0) - per_min.get(s - 1, 0)
        if run > best_sum:
            best, best_sum = s, run
    p_start, p_end = (t0 + best) * 60, (t0 + best + W) * 60
    peak_rps = best_sum / (W * 60.0)

    # ---- sessions
    by_client = defaultdict(list)
    for e in events:
        by_client[e["client"]].append(e)
    sessions, gaps = [], []
    for evs in by_client.values():
        cur = [evs[0]]
        for e in evs[1:]:
            d = e["t"] - cur[-1]["t"]
            if d > a.session_gap:
                sessions.append(cur); cur = [e]
            else:
                if 0.2 <= d <= 120:
                    gaps.append(d)
                cur.append(e)
        sessions.append(cur)
    think = statistics.median(gaps) if gaps else 3.0
    q = statistics.quantiles(gaps, n=4) if len(gaps) >= 8 else [think * 0.5, think, think * 1.5]
    think_min_ms, think_range_ms = int(q[0] * 1000), max(int((q[2] - q[0]) * 1000), 500)

    def klass(sess):
        sc = {e["scenario"] for e in sess}
        for k in ("checkout", "cart", "search"):
            if k in sc:
                return k
        return "history" if ("history" in sc and "browse" not in sc) else "browse" if sc - {"auth"} else None

    in_peak = [s for s in sessions if s[-1]["t"] + think >= p_start and s[0]["t"] <= p_end]
    mix_c = Counter(k for k in (klass(s) for s in in_peak) if k)
    tot = sum(mix_c.values()) or 1
    raw = {k: 100.0 * mix_c.get(k, 0) / tot for k in ("browse", "search", "cart", "checkout", "history")}
    mix = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - mix[k], reverse=True)[: 100 - sum(mix.values())]:
        mix[k] += 1

    # ---- concurrency (sweep line inside peak window)
    pts = []
    for s in in_peak:
        pts += [(max(s[0]["t"], p_start), 1), (min(s[-1]["t"] + think, p_end), -1)]
    pts.sort()
    cur = peak_conc = 0
    for _, d in pts:
        cur += d
        peak_conc = max(peak_conc, cur)
    users = int(math.ceil(peak_conc * a.growth * a.buffer / 10.0) * 10)
    co = sum(1 for e in events if p_start <= e["t"] < p_end and e["npath"].startswith("/api/checkout") and e["method"] == "POST")
    checkout_tps = max(round(co / (W * 60.0) * a.growth * a.buffer, 2), 0.1)
    rts = [e["rt"] for e in events if e["rt"]]

    lines = [f"# Generated by ai/workload_from_logs.py from {os.path.basename(a.log)} ({total} lines, {len(sessions)} sessions)",
             f"# Peak {W}-min window: {datetime.fromtimestamp(p_start).isoformat(' ', 'minutes')}  ~{peak_rps:.2f} mapped req/s, "
             f"{peak_conc} concurrent sessions (x{a.growth} growth x{a.buffer} buffer)",
             f"users={users}", f"rampup={a.rampup}", f"duration={a.rampup + a.hold}"]
    lines += [f"mix_{k}={v}" for k, v in mix.items()]
    lines += [f"checkout_tps={checkout_tps}", f"think_min_ms={think_min_ms}", f"think_range_ms={think_range_ms}"]
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w").write("\n".join(lines) + "\n")

    print("\n".join(lines))
    print(f"\n# observed: median think {think:.1f}s" + (f", median server rt {statistics.median(rts)*1000:.0f} ms (Little's Law check: "
          f"{peak_rps:.1f} req/s x ({statistics.median(rts):.2f}+{think:.1f}) s)" if rts else ""))
    if unmapped:
        top = unmapped.most_common(10)
        print("\n# UNMAPPED endpoints (add to ai/endpoint_map.json or create new fragments):")
        for k, n in top:
            print(f"#   {n:>7}  {k}")
        if a.ai:
            import llm
            if llm.available():
                try:
                    ans = llm.call("You are a performance architect. Classify web/API endpoints into the load-test scenarios "
                                   "browse, search, cart, checkout, history, auth, ignore, or new_flow (needs a new script). "
                                   "Answer ONE JSON object: {\"classification\": {\"<METHOD path>\": \"<scenario>\"}, \"notes\": str}. "
                                   "Use only the given list.", json.dumps(dict(top)))
                    notes = llm.extract_json(ans)
                    open(os.path.splitext(a.out)[0] + ".ai_notes.json", "w").write(json.dumps(notes, indent=1))
                    print("\n# AI classification of unmapped endpoints saved (review before editing endpoint_map.json)")
                except Exception as e:
                    print(f"# [ai] skipped: {e}")
            else:
                print("# [ai] no ANTHROPIC_API_KEY - skipped")


if __name__ == "__main__":
    main()
