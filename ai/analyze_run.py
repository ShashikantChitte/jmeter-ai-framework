#!/usr/bin/env python3
"""AI-assisted run analysis (advisory). The pass/fail GATE stays deterministic - AI never changes it.

  python3 ai/analyze_run.py --results results --profile config/load_profiles/load.properties \
        --thresholds thresholds/thresholds.json --baseline baseline/summary.json --out results/ai
Outputs: ai_analysis.md / .html / .json, evidence.json, summary.txt (Slack-ready).  Works offline (rules + statistics);
with ANTHROPIC_API_KEY the rule-based analysis is refined by an LLM that only sees redacted, aggregated evidence."""
import argparse, html, json, os, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence, llm, rule_based

SYSTEM = """You are a senior performance engineer reviewing the result of a JMeter load test for engineers and managers.
RULES: (1) Use ONLY the evidence JSON; never invent numbers, hosts or metrics. (2) Separate facts (cite the evidence values)
from hypotheses; give each hypothesis a confidence and a concrete verification step (thread dumps, GC logs, query plans,
traces). (3) You receive a deterministic rule-based analysis: confirm, merge, re-prioritise or add to it, but do NOT contradict
its numbers and do NOT change its verdict. (4) If load generators look saturated, say results may be unreliable.
(5) Keep executive_summary under 110 words, plain language, no jargon. (6) At most 8 findings, most important first.
Respond with ONE JSON object only, no prose, exactly this schema:
{"executive_summary": str,
 "findings": [{"id": "F1", "severity": "high|medium|low", "title": str, "evidence": [str],
               "hypotheses": [{"cause": str, "confidence": "high|medium|low", "verify": str}]}],
 "recommended_next_steps": [str], "test_validity_notes": [str]}"""


def valid(a):
    try:
        return (isinstance(a["executive_summary"], str) and isinstance(a["findings"], list)
                and all({"title", "severity", "evidence", "hypotheses"} <= set(f) for f in a["findings"])
                and isinstance(a["recommended_next_steps"], list))
    except Exception:
        return False


def refine_with_llm(ev, base):
    prompt = ("EVIDENCE:\n" + json.dumps(ev, indent=1) + "\n\nRULE_BASED_ANALYSIS (deterministic):\n" +
              json.dumps({k: base[k] for k in ("verdict", "findings", "recommended_next_steps", "test_validity_notes")}, indent=1))
    # privacy: host names never leave the machine (aliases are mapped back in the answer)
    alias = {h: f"generator-{i}" for i, h in enumerate(sorted(ev.get("load_generators", {})), 1)}
    if os.environ.get("AI_ANON_HOSTS", "true").lower() == "true":
        for h, al in alias.items():
            prompt = prompt.replace(h, al)
    raw = llm.call(SYSTEM, prompt)
    if os.environ.get("AI_ANON_HOSTS", "true").lower() == "true":
        for h, al in alias.items():
            raw = raw.replace(al, h)
    ans = llm.extract_json(raw)
    if not valid(ans):
        raise RuntimeError("LLM answer did not match the schema")
    for i, f in enumerate(ans["findings"], 1):
        f.setdefault("id", f"F{i}")
    ans.setdefault("test_validity_notes", base["test_validity_notes"])
    ans["verdict"] = base["verdict"]            # verdict is deterministic
    ans["source"] = f"llm:{llm.MODEL} (refined rule-based analysis)"
    return ans


def render_md(a, ev):
    L = [f"# AI-assisted performance analysis", "", f"**Verdict:** `{a['verdict']}`  |  Gate: `{ev['gate']}`  |  Source: {a['source']}", "",
         "## Executive summary", a["executive_summary"], "", "## SLA table", "",
         "| Transaction | p95 ms | threshold | headroom | error % | tps | status |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in ev["sla_table"]:
        L.append(f"| {r['transaction']} | {r['p95_ms']} | {r['p95_threshold_ms']} | {r['p95_headroom_pct']}% | {r['error_pct']} | {r['tps']} | {r['status']} |")
    L += ["", "## Findings"]
    if not a["findings"]:
        L.append("No anomalies detected.")
    for f in a["findings"]:
        L += ["", f"### {f.get('id','')} [{f['severity'].upper()}] {f['title']}", "**Evidence**"] + [f"- {e}" for e in f["evidence"]]
        if f["hypotheses"]:
            L += ["", "**Hypotheses (not confirmed facts)**", "", "| Likely cause | Confidence | How to verify |", "|---|---|---|"]
            L += [f"| {h['cause']} | {h['confidence']} | {h['verify']} |" for h in f["hypotheses"]]
    L += ["", "## Recommended next steps"] + [f"{i}. {s}" for i, s in enumerate(a["recommended_next_steps"], 1)]
    if a["test_validity_notes"]:
        L += ["", "## Test validity notes"] + [f"- {n}" for n in a["test_validity_notes"]]
    L += ["", "---", "*Advisory output. The quality gate (check_thresholds.py) is the authoritative pass/fail. "
          "Hypotheses must be verified with the listed evidence before acting.*"]
    return "\n".join(L) + "\n"


def render_html(md_text):
    # minimal, dependency-free rendering: keep markdown readable inside <pre> with a title
    return ("<!doctype html><meta charset=utf-8><title>AI analysis</title><body style='font-family:system-ui;max-width:1000px;margin:2em auto'>"
            f"<pre style='white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:13px'>{html.escape(md_text)}</pre></body>")


def influx_event(url, application, title, text):
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    line = f'events,application={application} title="{esc(title)}",text="{esc(text)[:900]}"'
    req = urllib.request.Request(url, data=line.encode(), method="POST")
    urllib.request.urlopen(req, timeout=10).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--thresholds", default="thresholds/thresholds.json")
    ap.add_argument("--baseline"); ap.add_argument("--profile")
    ap.add_argument("--skip-sec", type=int)
    ap.add_argument("--out"); ap.add_argument("--influx-url"); ap.add_argument("--application", default="omp")
    a = ap.parse_args()
    out = a.out or os.path.join(a.results, "ai")
    os.makedirs(out, exist_ok=True)

    ev = evidence.build_evidence(a.results, a.thresholds, a.baseline, a.profile, a.skip_sec)
    json.dump(ev, open(os.path.join(out, "evidence.json"), "w"), indent=1)
    result = rule_based.analyze(ev)
    if llm.available():
        try:
            result = refine_with_llm(ev, result)
        except Exception as e:                      # never break the pipeline because of AI
            result["test_validity_notes"].append(f"LLM refinement skipped ({e}); showing rule-based analysis.")
            print(f"[ai] LLM unavailable, using rule-based analysis: {e}", file=sys.stderr)
    md = render_md(result, ev)
    json.dump(result, open(os.path.join(out, "ai_analysis.json"), "w"), indent=1)
    open(os.path.join(out, "ai_analysis.md"), "w").write(md)
    open(os.path.join(out, "ai_analysis.html"), "w").write(render_html(md))
    open(os.path.join(out, "summary.txt"), "w").write(f"[{result['verdict']}] {result['executive_summary']}\n")
    if a.influx_url:
        try:
            influx_event(a.influx_url, a.application, f"AI analysis: {result['verdict']}", result["executive_summary"])
        except Exception as e:
            print(f"[ai] influx annotation failed: {e}", file=sys.stderr)
    print(md)
    print(f"[ai] written to {out} (source: {result['source']})")


if __name__ == "__main__":
    main()
