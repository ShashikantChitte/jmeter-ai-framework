#!/usr/bin/env python3
"""Performance quality gate. Parses the JTL (CSV) and applies per-transaction SLAs.

Exit codes: 0 = pass | 1 = SLA breach | 2 = regression warning vs baseline (SLAs still met)
Features: ramp-up exclusion, per-transaction p95/p99/error%, min TPS, baseline compare,
          JUnit XML (for Jenkins/Azure/GitLab test tabs), summary.json (reusable as next baseline).
"""
import argparse, csv, json, re, sys
from collections import defaultdict
from xml.sax.saxutils import escape

ap = argparse.ArgumentParser()
ap.add_argument("jtl")
ap.add_argument("--thresholds", default="thresholds/thresholds.json")
ap.add_argument("--skip-sec", type=int, default=0, help="ignore first N seconds (ramp-up)")
ap.add_argument("--baseline", help="summary.json of a previous good run")
ap.add_argument("--junit"); ap.add_argument("--summary")
ap.add_argument("--label-regex", default=r"^(TX|AUTH|PROTO)")
a = ap.parse_args()

cfg = json.load(open(a.thresholds))
default = cfg.get("default", {"p95_ms": 2000, "error_pct": 1.0})
per = cfg.get("transactions", {})
tol = cfg.get("regression_tolerance_pct", 15)
min_tps = cfg.get("min_tps", 0)

rows = []
with open(a.jtl, newline="") as f:
    for r in csv.DictReader(f):
        if re.search(a.label_regex, r["label"]):
            rows.append(r)
if not rows:
    print("No samples found - did the test run?"); sys.exit(1)

t0 = int(rows[0]["timeStamp"])
rows = [r for r in rows if int(r["timeStamp"]) - t0 >= a.skip_sec * 1000] or rows
lat, err = defaultdict(list), defaultdict(int)
for r in rows:
    lat[r["label"]].append(int(r["elapsed"]))
    if r["success"] != "true":
        err[r["label"]] += 1

def pct(v, p):
    v = sorted(v); return v[max(int(len(v) * p / 100.0 + 0.5) - 1, 0)]

dur = max((int(rows[-1]["timeStamp"]) - int(rows[0]["timeStamp"])) / 1000.0, 1)
base = json.load(open(a.baseline))["transactions"] if a.baseline else {}
summary, breach, warn, cases = {}, False, False, []
print(f"{'RESULT':7}{'TRANSACTION':34}{'N':>8}{'p95ms':>8}{'p99ms':>8}{'err%':>7}{'tps':>8}  notes")
for label in sorted(lat):
    v = lat[label]; n = len(v)
    p95, p99, e, tps = pct(v, 95), pct(v, 99), err[label] * 100.0 / n, n / dur
    t = {**default, **per.get(label, {})}
    notes, status = [], "PASS"
    if p95 > t["p95_ms"]: status = "FAIL"; notes.append(f"p95>{t['p95_ms']}")
    if "p99_ms" in t and p99 > t["p99_ms"]: status = "FAIL"; notes.append(f"p99>{t['p99_ms']}")
    if e > t["error_pct"]: status = "FAIL"; notes.append(f"err>{t['error_pct']}%")
    b = base.get(label)
    if b and status == "PASS" and p95 > b["p95_ms"] * (1 + tol / 100.0):
        status = "WARN"; notes.append(f"regression {b['p95_ms']}->{p95}ms (>{tol}%)")
    breach |= status == "FAIL"; warn |= status == "WARN"
    summary[label] = {"samples": n, "p95_ms": p95, "p99_ms": p99, "error_pct": round(e, 3), "tps": round(tps, 2)}
    cases.append((label, status, notes, f"p95={p95}ms err={e:.2f}%"))
    print(f"{status:7}{label:34}{n:>8}{p95:>8}{p99:>8}{e:>7.2f}{tps:>8.1f}  {'; '.join(notes)}")

total_tps = len(rows) / dur
print(f"Overall throughput: {total_tps:.1f} samples/s over {dur:.0f}s (steady-state window)")
if min_tps and total_tps < min_tps:
    breach = True; print(f"FAIL: throughput {total_tps:.1f} < min_tps {min_tps}")

if a.summary:
    json.dump({"overall_tps": round(total_tps, 2), "transactions": summary}, open(a.summary, "w"), indent=2)
if a.junit:
    fails = sum(1 for c in cases if c[1] == "FAIL")
    with open(a.junit, "w") as f:
        f.write(f'<testsuite name="performance-gate" tests="{len(cases)}" failures="{fails}">\n')
        for label, status, notes, info in cases:
            f.write(f'  <testcase classname="perf" name="{escape(label)}">')
            if status == "FAIL": f.write(f'<failure message="{escape("; ".join(notes))}">{escape(info)}</failure>')
            f.write("</testcase>\n")
        f.write("</testsuite>\n")

print("QUALITY GATE:", "FAILED" if breach else ("PASSED WITH WARNINGS" if warn else "PASSED"))
sys.exit(1 if breach else (2 if warn else 0))
