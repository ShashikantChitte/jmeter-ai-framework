# AI in the OMP Framework

## Design principles
1. **Advisory, never authoritative.** The pass/fail gate (`scripts/check_thresholds.py`) stays deterministic. The AI verdict is *derived from the gate*; an LLM cannot change it.
2. **Offline-first.** Every tool works with no API key (statistics + rules). An LLM only *refines* the result when `ANTHROPIC_API_KEY` is set.
3. **Never in the measured path.** No model call runs inside a load test (it would add latency, cost and noise). AI works **before** (design, scripting, workload) and **after** (analysis, thresholds) the run.
4. **Privacy by construction.** Only aggregated evidence is sent (never raw JTL, bodies, headers or credentials). JWTs, bearer tokens, e-mails, IPs, long hex secrets and `password=`-style values are redacted; host names are replaced by `generator-N`. See `ai/llm.py::redact` and `AI_ANON_HOSTS`.
5. **Hypotheses are labelled.** Every cause is a *hypothesis* with a confidence and a concrete verification step (thread dumps, GC log, query plan, trace).
6. **Never breaks the pipeline.** API errors, bad JSON or timeouts fall back to the rule-based analysis; the step is non-fatal.

## Where AI fits
| Phase | Tool | What it does | Method |
|---|---|---|---|
| Plan | `ai/workload_from_logs.py` | Production access log -> peak window, concurrent sessions, transaction mix, think time, checkout rate -> `from_logs.properties` | Deterministic; optional LLM classifies unmapped endpoints |
| Script | `ai/har_to_fragment.py` | Browser HAR -> fragment `.jmx` with **auto-correlation** (JSON/Regex extractors) and parameterisation | Deterministic detection; optional LLM renames variables / flags false positives |
| Calibrate | `ai/learn_thresholds.py` | History of passing runs -> **suggested** SLA thresholds (median + robust spread) | Statistics, human approves |
| Analyse | `ai/analyze_run.py` | Knee/saturation point, latency episodes, drift, error clustering, generator validity -> findings, hypotheses, next steps, executive summary, Slack text, Grafana annotation | Statistics + rules; optional LLM refinement |
| Operate | `run_test.sh` (`RUN_AI_ANALYSIS`, `SAVE_HISTORY`), Jenkins/GitLab | Runs the above automatically and publishes `results/ai/` | - |

Deliberately **not** done: LLM-generated load, AI-chosen pass/fail, auto-editing thresholds or scripts without review, sending raw data.

## Usage
```bash
python3 ai/selftest.py                                   # 13 checks, no JMeter/API key needed (also a demo)

# 1) after a run (automatic when RUN_AI_ANALYSIS=true)
python3 ai/analyze_run.py --results results --profile config/load_profiles/load.properties \
        --baseline baseline/summary.json --out results/ai
#    -> ai_analysis.md/.html/.json, evidence.json, summary.txt     (add --influx-url to annotate Grafana)

# 2) workload from production logs
python3 ai/workload_from_logs.py /var/log/nginx/access.log --growth 1.3 --buffer 1.2
ENV=perf PROFILE=from_logs ./scripts/run_test.sh

# 3) script from a recording
python3 ai/har_to_fragment.py recording.har --name wishlist [--ai-review]
#    -> tests/fragments/frg_wishlist.jmx + frg_wishlist.correlation_report.md  (then add an Include Controller)

# 4) learn thresholds from history (SAVE_HISTORY=true collects passing runs in baseline/history/)
python3 ai/learn_thresholds.py baseline/history        # writes thresholds.suggested.json, never overwrites
```

## Configuration
| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables LLM refinement (keep it in CI secrets, never in files) |
| `AI_MODE` | `auto` | `offline` = never call an LLM, `llm` = fail if no key |
| `AI_MODEL` | `claude-sonnet-5-5` | Model id - check the current names in Anthropic's docs |
| `AI_API_URL` | Anthropic Messages API | Override for a gateway/proxy |
| `AI_MAX_INPUT_CHARS` | 60000 | Cost guard (typical prompt is ~15k chars) |
| `AI_ANON_HOSTS` | `true` | Replace host names by aliases before sending |
| `RUN_AI_ANALYSIS`, `ANNOTATE_GRAFANA`, `SAVE_HISTORY` | false | `run_test.sh` switches |

## What the statistics detect
* **Knee point:** first window reaching the throughput plateau while users keep rising and p95/errors climb -> capacity estimate.
* **Degradation episodes:** p95 above median + 4 x robust sigma (MAD) during steady state (GC, failover, batch, autoscaling).
* **Drift:** linear trend of steady-state p95 (>= 5%/h, r2 >= 0.5, >= 30 min) -> leak/accumulation hypothesis; also the generator's heap floor.
* **Error clusters:** messages normalised (numbers/UUIDs stripped) and counted per transaction.
* **Generator validity:** CPU > 80% in >= 10% of samples or GC > 10% -> verdict `RESULTS_UNRELIABLE`.

## Limitations (read before trusting)
* Heuristics need enough data: >= 8 windows for knee/episodes, >= 5 runs for threshold learning, steady state >= 5 min.
* Server-side metrics (CPU, pools, DB) are not in the JTL, so hypotheses about causes are *leads*, not proof. Feed APM/Prometheus data into the evidence for stronger conclusions.
* LLM output can be wrong or generic; it is constrained to the supplied evidence and the verdict is fixed, but always verify before acting.
* The LLM path was tested against a local stub of the API (request format, parsing, fallback), not against a live model.
* `har_to_fragment.py` detects values that *re-appear*; tokens used only in headers/cookies are skipped by design, and constants (non-dynamic IDs) still need CSV parameterisation.
