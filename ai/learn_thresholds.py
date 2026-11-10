#!/usr/bin/env python3
"""Learn data-driven SLA threshold SUGGESTIONS from history (statistics: median + robust spread).
Never overwrites thresholds.json - a human reviews and commits.

  python3 ai/learn_thresholds.py baseline/history  [--k 3] [--out thresholds/thresholds.suggested.json]
Collect history automatically: run_test.sh with SAVE_HISTORY=true copies each PASSING summary.json to baseline/history/."""
import argparse, glob, json, math, os, statistics

ap = argparse.ArgumentParser()
ap.add_argument("history_dir"); ap.add_argument("--current", default="thresholds/thresholds.json")
ap.add_argument("--out", default="thresholds/thresholds.suggested.json"); ap.add_argument("--k", type=float, default=3.0)
ap.add_argument("--min-runs", type=int, default=5)
a = ap.parse_args()
runs = [json.load(open(f))["transactions"] for f in sorted(glob.glob(os.path.join(a.history_dir, "*.json")))]
if len(runs) < a.min_runs:
    raise SystemExit(f"need >= {a.min_runs} historical runs, found {len(runs)}")
cur = json.load(open(a.current)) if os.path.exists(a.current) else {}
ctx = cur.get("transactions", {})
default = cur.get("default", {"p95_ms": 2000, "error_pct": 1.0})
sug = {}
print(f"{len(runs)} runs analysed\n{'transaction':34}{'median':>8}{'MAD':>7}{'suggested':>11}{'current':>9}  note")
for lab in sorted({l for r in runs for l in r}):
    p95 = [r[lab]["p95_ms"] for r in runs if lab in r]
    err = [r[lab]["error_pct"] for r in runs if lab in r]
    if len(p95) < a.min_runs:
        continue
    med = statistics.median(p95)
    mad = statistics.median([abs(x - med) for x in p95]) or med * 0.03
    s = max(med + a.k * 1.4826 * mad, med * 1.10)               # >= 10% margin over the typical value
    s = int(math.ceil(s / 50.0) * 50)
    e = round(max(0.1, math.ceil(max(err) * 1.5 * 10) / 10), 1)
    cur_p95 = {**default, **ctx.get(lab, {})}["p95_ms"]
    note = "TIGHTEN" if s < cur_p95 * 0.8 else ("LOOSEN?" if s > cur_p95 else "ok")
    sug[lab] = {"p95_ms": s, "error_pct": e}
    print(f"{lab:34}{med:>8.0f}{mad:>7.0f}{s:>11}{cur_p95:>9}  {note}")
out = {"_comment": f"SUGGESTED from {len(runs)} runs (median + {a.k} x robust sigma). Review before adopting - do not auto-apply.",
       "default": default, "regression_tolerance_pct": cur.get("regression_tolerance_pct", 15), "transactions": sug}
json.dump(out, open(a.out, "w"), indent=2)
print(f"\nwritten {a.out}  (LOOSEN? means observed performance is worse than your current SLA - fix the system or renegotiate)")
