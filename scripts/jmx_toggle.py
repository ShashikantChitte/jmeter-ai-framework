#!/usr/bin/env python3
"""Enable/disable a JMX element by name without opening the GUI.
   python3 scripts/jmx_toggle.py tests/main/omp_main.jmx "Backend Listener - InfluxDB (Grafana)" off
   python3 scripts/jmx_toggle.py tests/main/omp_main.jmx "Backend Listener - Graphite" on     (prefix match)"""
import sys, re
path, name, state = sys.argv[1], sys.argv[2], sys.argv[3].lower()
val = "true" if state in ("on", "true", "1") else "false"
s = open(path, encoding="utf-8").read()
pat = re.compile(r'(testname="' + re.escape(name) + r'[^"]*" enabled=")(true|false)(")')
s2, n = pat.subn(r"\g<1>" + val + r"\g<3>", s)
if n == 0: sys.exit(f"element not found: {name}")
open(path, "w", encoding="utf-8").write(s2)
print(f"{n} element(s) set enabled={val}")
