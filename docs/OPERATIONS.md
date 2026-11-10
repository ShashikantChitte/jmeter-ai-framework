# Operations Guide

## Execution order of a JMeter element (per sampler)
`Config elements → Pre-processors → Timers → Sampler → Post-processors → Assertions → Listeners`
Elements are applied by **scope** (parent branch), not by position.

## Sizing a load generator (calibration)
1. Run `PROFILE=load` with `USERS=N` on ONE generator, watching `lg_health.csv` / the Load-Generator dashboard.
2. Increase N until **system CPU ≈ 70%** or GC time > 5%. That N is `users_per_worker`.
3. `workers = ceil(target_users / users_per_worker)`; keep 20% spare.
4. If client-side latency is higher than server-side (APM) latency → the generator is the bottleneck.

## High-load tuning checklist
| Area | Setting |
|---|---|
| Mode | Non-GUI only (`-n`), run in the background (`nohup`/container) |
| Heap | `HEAP="-Xms4g -Xmx4g"`, ≤ 70% of container memory |
| GC | G1, `MaxGCPauseMillis=100`; ZGC for very large heaps |
| Listeners | Backend Listener + `-l` file only. **No** View Results Tree / Aggregate in load runs |
| Results | CSV, no response data, no headers (see `tuning.properties`); error-only writer keeps failure details |
| Scripts | Groovy JSR223 with cache key; no BeanShell; avoid heavy regex on big bodies |
| HTTP | Retries off, keep-alive on, `httpclient.reset_state_on_thread_group_iteration=true` |
| Distributed | `mode=StrippedAsynch`, fixed RMI ports, same JMeter/Java/plugins everywhere, split CSV |
| OS | `ulimit -n 65535`, wider ephemeral port range, `net.ipv4.tcp_tw_reuse=1` on generators |
| K8s | requests = limits (Guaranteed QoS), spread workers across nodes, watch CPU throttling |

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| `Connection refused` on workers | Firewall/RMI ports 1099, 50000, 7000; wrong `java.rmi.server.hostname` |
| Load is N× expected | Each worker runs the full plan – use `-Gusers` + `workers` (done by `run_test.sh`) |
| Property ignored on workers | Passed with `-J` instead of `-G` |
| Everything 401 | `auth_mode` mismatch, token expired, or secret env var missing |
| Many `NOT_FOUND` values | Correlation failed – check extractor expression and the previous response in `errors.jtl` |
| Test stops at once | `/health` failed in setUp (`fail_fast=true`) |
| Controller OOM | Too much result traffic – `mode=StrippedAsynch`, remove listeners, fewer saved fields |
| Include path not found | `base_dir` not absolute; run via `scripts/run_test.sh` |
| p99 spikes, CPU low | Check generator GC, CPU throttling (cAdvisor panel), server-side GC |

## Adding a new scenario (3 steps)
1. Create `tests/fragments/frg_<flow>.jmx` (copy an existing one; name transactions `TXnn_<Name>`).
2. In the master plan add a thread group, a Module Controller → `FRG - Authenticate`, and an Include Controller for the fragment.
3. Add the SLA to `thresholds/thresholds.json` and a `mix_<flow>` property to the profiles.

## Naming conventions (the gate, Grafana and the report filter on these)
`AUTH_*` authentication · `TXnn_*` business transactions · `PROTO_*` non-HTTP protocols · `SETUP_*` / `TEARDOWN_*` housekeeping (excluded from SLAs).
