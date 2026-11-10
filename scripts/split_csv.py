#!/usr/bin/env python3
"""Split users.csv across N workers so no two generators use the same accounts.
   python3 scripts/split_csv.py data/users.csv 3   -> data/split/users_0.csv ... users_2.csv
   Worker pod 'jmeter-worker-<n>' automatically picks users_<n>.csv (see docker/entrypoint.sh)."""
import csv, os, sys
src, n = sys.argv[1], int(sys.argv[2])
out = os.path.join(os.path.dirname(src), "split"); os.makedirs(out, exist_ok=True)
rows = list(csv.reader(open(src))); head, body = rows[0], rows[1:]
base = os.path.splitext(os.path.basename(src))[0]
for i in range(n):
    with open(os.path.join(out, f"{base}_{i}.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(head); w.writerows(body[i::n])
print(f"split {len(body)} rows into {n} files in {out}")
