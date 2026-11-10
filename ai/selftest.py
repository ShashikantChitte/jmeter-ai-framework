#!/usr/bin/env python3
"""Self-test + demo data for the AI toolkit (no JMeter, no API key needed).
   python3 ai/selftest.py                 # run assertions
   python3 ai/selftest.py --demo /tmp/demo   # only write a synthetic results folder, then:
        python3 ai/analyze_run.py --results /tmp/demo --skip-sec 0 --out /tmp/demo/ai"""
import csv, json, math, os, random, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
T0 = 1_700_000_000_000


def make_run(d, saturate_generator=True):
    random.seed(3)
    os.makedirs(d, exist_ok=True)
    labels = [("TX02_Search_Query", .5, .35), ("TX04_Checkout_Submit", .3, .8), ("TX05_OrderHistory", .2, .45)]
    cap_threads, cap_tps, rows = 600, 120, []
    for sec in range(1800):                      # ramp 0 -> 1500 users in 1200 s, hold 600 s; system saturates at 600 users
        threads = max(5, min(1500, int(1500 * sec / 1200)))
        tps = cap_tps * min(threads, cap_threads) / cap_threads
        over = max(0, (threads - cap_threads) / cap_threads)
        n = int(tps) + (1 if random.random() < tps - int(tps) else 0)
        for _ in range(n):
            lab, _, base = random.choices(labels, [l[1] for l in labels])[0]
            el = base * 1000 * (1 + 2.5 * over ** 1.3) * random.lognormvariate(0, .25)
            if 1500 <= sec < 1560:
                el *= 4                          # injected degradation episode
            ok, code, msg = True, "200", "OK"
            if threads > 1000 and random.random() < .06 * over:
                ok, code, msg = False, "Non HTTP response code: java.net.SocketTimeoutException", "Read timed out"
            elif threads > 900 and random.random() < .01 * over:
                ok, code, msg = False, "503", "Service Unavailable"
            rows.append([T0 + sec * 1000 + random.randint(0, 999), int(el), lab, code, msg, str(ok).lower(), "", threads])
    rows.sort()
    hdr = ["timeStamp", "elapsed", "label", "responseCode", "responseMessage", "success", "failureMessage", "allThreads"]
    for name, only_err in (("results.jtl", False), ("errors.jtl", True)):
        with open(os.path.join(d, name), "w", newline="") as f:
            w = csv.writer(f); w.writerow(hdr)
            w.writerows(r for r in rows if not (only_err and r[5] == "true"))
    with open(os.path.join(d, "lg_health.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow("timestamp,host,proc_cpu,sys_cpu,heap_used_mb,heap_max_mb,nonheap_used_mb,gc_count,gc_time_ms,open_fds,threads,rx_mb_s,tx_mb_s".split(","))
        for host, hot in (("gen-1", False), ("gen-2", saturate_generator)):
            gc = 0
            for sec in range(0, 1800, 15):
                cpu = .88 + .08 * random.random() if hot and sec >= 700 else .45 + .1 * random.random()
                gc += int(20 + (200 if hot and sec >= 700 else 0) * random.random())
                w.writerow([T0 + sec * 1000, host, f"{cpu*.9:.3f}", f"{cpu:.3f}", 1200 + random.randint(0, 300), 2048, 150, sec // 5, gc,
                            800 + sec // 10, 300 + sec // 20, f"{random.uniform(5, 20):.3f}", f"{random.uniform(3, 15):.3f}"])
    json.dump({"testrun": "synthetic", "env": "perf", "users": "1500", "workers": "2"}, open(os.path.join(d, "run_info.json"), "w"))


def run(*args, **env):
    e = {**os.environ, "AI_MODE": "offline", **env}
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True, env=e, cwd=ROOT)
    return r.returncode, r.stdout + r.stderr


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    return cond


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--demo":
        make_run(sys.argv[2]); print("demo results in", sys.argv[2]); return
    tmp = tempfile.mkdtemp(prefix="omp_ai_")
    ok = True
    # ---- 1. run analysis
    make_run(os.path.join(tmp, "res"))
    rc, out = run("ai/analyze_run.py", "--results", os.path.join(tmp, "res"), "--skip-sec", "0", "--out", os.path.join(tmp, "ai"))
    a = json.load(open(os.path.join(tmp, "ai", "ai_analysis.json")))
    ev = json.load(open(os.path.join(tmp, "ai", "evidence.json")))
    knee = (ev.get("knee_point") or {}).get("threads_at_peak_throughput", 0)
    ok &= check("analysis runs offline", rc == 0 and a["source"] == "rule-based", out[-200:])
    ok &= check("knee point found near 600 users", 480 <= knee <= 720, str(knee))
    ok &= check("saturated generator detected -> RESULTS_UNRELIABLE", ev["generator_saturated_hosts"] == ["gen-2"] and a["verdict"] == "RESULTS_UNRELIABLE")
    ok &= check("degradation episode detected", len(ev["degradation_episodes"]) >= 1)
    ok &= check("timeout error cluster found", any("timed out" in c["message"] for c in ev["error_clusters"]))
    ok &= check("gate stays deterministic (FAIL)", ev["gate"] == "FAIL")
    make_run(os.path.join(tmp, "res2"), saturate_generator=False)
    rc, _ = run("ai/analyze_run.py", "--results", os.path.join(tmp, "res2"), "--skip-sec", "0", "--out", os.path.join(tmp, "ai2"))
    a2 = json.load(open(os.path.join(tmp, "ai2", "ai_analysis.json")))
    ok &= check("healthy generators -> verdict follows the gate", a2["verdict"] == "FAIL")
    # ---- 2. workload from logs
    rnd = random.Random(11); lines = []
    import datetime as dt
    base = dt.datetime(2026, 11, 2, 9, tzinfo=dt.timezone.utc)
    def emit(ip, t, m, p, st=200):
        ts = (base + dt.timedelta(seconds=t)).strftime("%d/%b/%Y:%H:%M:%S +0000")
        lines.append((t, f'{ip} - - [{ts}] "{m} {p} HTTP/1.1" {st} 512 "-" "UA" rt=0.150'))
    sid = 0
    for minute in range(120):
        for _ in range(int(rnd.gauss(3 + 20 * math.exp(-((minute - 60) / 15) ** 2), 1))):
            sid += 1; ip = f"10.0.{sid % 250}.{sid // 250}"; t = minute * 60 + rnd.uniform(0, 59); r = rnd.random()
            if r < .5: emit(ip, t, "GET", f"/api/products/{rnd.randint(100,9999)}")
            elif r < .8: emit(ip, t, "GET", "/api/search?q=x")
            else:
                emit(ip, t, "GET", "/api/search?q=x"); emit(ip, t + 5, "POST", "/api/cart/items", 201)
                emit(ip, t + 12, "POST", "/api/checkout", 201)
    open(os.path.join(tmp, "access.log"), "w").write("\n".join(l for _, l in sorted(lines)) + "\n")
    out_p = os.path.join(tmp, "from_logs.properties")
    rc, out = run("ai/workload_from_logs.py", os.path.join(tmp, "access.log"), "--out", out_p)
    props = dict(l.strip().split("=", 1) for l in open(out_p) if "=" in l and not l.startswith("#"))
    mix = sum(int(props[k]) for k in props if k.startswith("mix_"))
    ok &= check("workload model: mix sums to 100 and users > 0", mix == 100 and int(props["users"]) > 0, str(props))
    ok &= check("workload model: checkout share ~20%", 10 <= int(props["mix_checkout"]) <= 30, props.get("mix_checkout"))
    # ---- 3. HAR -> fragment
    H = "https://shop.example.com"
    def ent(m, u, body=None, ct=None, resp="", mime="application/json"):
        e = {"request": {"method": m, "url": u, "headers": []}, "response": {"status": 200, "content": {"mimeType": mime, "text": resp}}}
        if body is not None: e["request"]["postData"] = {"mimeType": ct or "application/json", "text": body}
        return e
    har = {"log": {"entries": [
        ent("POST", H + "/api/auth/login", '{"username":"u"}', resp='{"token":"abc"}'),
        ent("GET", H + "/api/search?q=laptop", resp=json.dumps({"results": [{"id": 110210}]})),
        ent("POST", H + "/api/cart/items", '{"productId":"110210"}', resp='{"cartId":"c-Ab12Cd34Ef"}'),
        ent("POST", H + "/api/checkout", '{"cartId":"c-Ab12Cd34Ef"}', resp='{"orderId":"o-9f8e7d6c5b"}'),
        ent("GET", H + "/api/orders/o-9f8e7d6c5b", resp='{"status":"CONFIRMED"}')]}}
    json.dump(har, open(os.path.join(tmp, "r.har"), "w"))
    rc, out = run("ai/har_to_fragment.py", os.path.join(tmp, "r.har"), "--name", "demo", "--out", os.path.join(tmp, "frag"), "--min-digits", "5")
    frag = open(os.path.join(tmp, "frag", "frg_demo.jmx")).read()
    ok &= check("HAR: 3 correlations -> extractors + ${var} substitution",
                frag.count("<JSONPostProcessor") == 3 and "${cart_id}" in frag and "${order_id}" in frag and "${result_id}" in frag, out[-200:])
    ok &= check("HAR: auth call skipped, search term parameterised", "/api/auth/login" not in frag and "q=${term}" in frag)
    import xml.etree.ElementTree as ET
    ET.fromstring(frag.encode()); ok &= check("HAR: generated JMX is valid XML", True)
    # ---- 4. privacy redaction
    sys.path.insert(0, HERE)
    import llm
    red = llm.redact("Bearer abcdefghijklmnop1234 user a.b@corp.com from 10.1.2.3 token=SuperSecret99 eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sigsigsig")
    ok &= check("redaction removes tokens/emails/IPs/JWTs", all(x not in red for x in ("abcdefghijklmnop1234", "a.b@corp.com", "10.1.2.3", "SuperSecret99", "eyJhbGci")), red)
    print("\nALL AI SELF-TESTS PASSED" if ok else "\nSOME AI SELF-TESTS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
