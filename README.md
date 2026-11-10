# OMP JMeter Performance Framework (dummy, scalable, runnable)

A reference framework that demonstrates **every pattern a senior performance architect is expected to know**,
wired end-to-end against a **mock application (WireMock)** so you can run it on a laptop in minutes.

```
Git ─► Jenkins / GitLab / Maven ─► Docker image (JMeter 5.6.3 + plugins + drivers)
                                      │ standalone  ──────────────┐
                                      │ controller ─► K8s workers ┤──► target app
                                      ▼                           │
                     Backend Listener ─► InfluxDB ─► Grafana      │
                     Graphite listener ─► graphite_exporter ─► Prometheus ─► Grafana
                     JMX exporter / node_exporter / cAdvisor ─► Prometheus  (CPU, memory, GC, network)
                                      ▼
              results.jtl + HTML report ─► quality gate (exit 0/1/2) ─► JUnit + Slack
```

## 1. Quick start (5 minutes)

```bash
# lab: mock app + InfluxDB + Grafana + Prometheus
docker compose up -d wiremock influxdb grafana prometheus node-exporter cadvisor graphite-exporter

# run a smoke test from your machine (needs local JMeter 5.6.x + Java 17)  ...
ENV=local PROFILE=smoke ./scripts/run_test.sh
# ... or fully containerised
docker compose --profile run run --rm jmeter

open http://localhost:3000      # Grafana  -> folder "OMP Performance" (2 dashboards, anonymous admin in the lab)
open results/report/index.html  # JMeter HTML report
```
Add JDBC / JMS / Kafka / WebSocket traffic:
```bash
docker compose --profile protocols up -d
ENV=local PROFILE=protocols ./scripts/run_test.sh
```

## 2. Repository layout

| Path | Purpose |
|---|---|
| `tests/main/omp_main.jmx` | **Master plan**: config, auth, listeners, all thread groups |
| `tests/fragments/*.jmx` | **Reusable flows** (one Test Fragment each) pulled in by Include Controllers |
| `config/env/*.properties` | Per-environment values (host, URLs). **No secrets** |
| `config/load_profiles/*.properties` | smoke / load / stress / spike / soak / protocols |
| `config/jmeter/tuning.properties` | High-load JMeter tuning (result fields, HTTP client, report) |
| `data/` , `scripts/split_csv.py` | Test data, per-worker data splitting |
| `scripts/` | `run_test.sh`, `run_distributed.sh`, `check_thresholds.py` (gate), `preflight.sh`, `jmx_toggle.py`, `generate_data.py` |
| `thresholds/thresholds.json` | Per-transaction SLAs (reviewed like code) |
| `Dockerfile`, `docker/entrypoint.sh` | One image, three roles: `standalone`, `controller`, `worker` |
| `k8s/` | Worker StatefulSet + headless Service, controller Job, run script, PodMonitor |
| `Jenkinsfile`, `.gitlab-ci.yml`, `pom.xml`, `build.gradle` | CI/CD and build-tool entry points |
| `monitoring/` | Prometheus config + alerts, graphite mapping, JMX exporter, Grafana provisioning + 2 dashboards |
| `mock/`, `db/` | WireMock stubs of the dummy app, PostgreSQL seed |
| `ai/`, `docs/AI.md` | **AI toolkit**: run analysis, workload-from-logs, HAR-to-script correlation, threshold learning (offline-first, optional LLM) |
| `docs/OPERATIONS.md` | Tuning, sizing, troubleshooting, runbook |
| `scripts/vm_cluster.sh`, `docs/DISTRIBUTED_VMS.md` | Master + workers on separate VMs: bootstrap, deploy, start, run, collect |

## 3. How each requirement is covered

| Requirement | Where / how |
|---|---|
| **Modular scripts: Test Fragments, Module and Include Controllers** | 11 fragment files in `tests/fragments/` included with **Include Controllers**. `FRG - Authenticate` is a Test Fragment inside the master plan reused by every thread group through a **Module Controller**. Add a new flow = add one `.jmx`, one Include. |
| **Externalised parameters (`-J`, `${__P()}`)** | Zero hard-coded values: host, users, ramp-up, duration, mix %, TPS, think times, URLs, flags all use `${__P(name,default)}`. Layering: `tuning < env file < profile file < -J`. |
| **Git repo, folder structure, Maven/Gradle** | Layout above; `pom.xml` (JMeter Maven Plugin: `mvn verify -Denv=qa -Dprofile=load`, fails on error rate), `build.gradle` wrapper. |
| **Backend Listener to InfluxDB / Prometheus + Grafana** | InfluxDB listener enabled; Graphite listener (→ `graphite_exporter` → Prometheus) disabled by default, enable with `python3 scripts/jmx_toggle.py tests/main/omp_main.jmx "Backend Listener - Graphite" on`. Runs are tagged `application=omp_<TESTRUN>` so Grafana can compare builds. |
| **Dockerised JMeter + Kubernetes distributed** | `Dockerfile` (pinned JMeter, plugins, Kafka/JDBC/JMS libs, JMX exporter, tuned JVM). `k8s/`: workers = StatefulSet + headless Service, controller = Job. `WORKERS=4 ./k8s/run_distributed.sh` |
| **Jenkins / GitLab pipelines** | `Jenkinsfile` (parameters, secrets, gate, JUnit, HTML report, Slack) and `.gitlab-ci.yml` (MR smoke, nightly load, manual stress/soak/distributed, Pages report). |
| **Correlation & parameterisation** | **JSON Extractor** (tokens, ids, random list element `match=0`), **Regex Extractor** (CSRF), **Boundary Extractor** (totalHits), JSR223 pre/post processors, **CSV Data Set Config** (users/products/terms), `__UUID`, `__Random`, `__RandomString`, defaults `NOT_FOUND` + assertions to catch failed correlation. |
| **Distributed testing** | Every worker runs the **full plan**; the plan computes `users × mix% ÷ workers` automatically (`-Gworkers=N`). `-G` sends env/profile files to workers, CSVs split per worker ordinal, `mode=StrippedAsynch` keeps the controller light. |
| **Tuning JMeter for high load** | `config/jmeter/tuning.properties` + `Dockerfile` ENV: fixed heap, G1, GC log, no response data stored, no heavy listeners (View Results Tree is disabled), Groovy with script cache, HTTP retries off, connection reuse, error-only writer. |
| **Grafana integration** | `monitoring/grafana/dashboards/jmeter-results.json` (users, TPS, errors, p95/p99, bandwidth per transaction) + `load-generator-health.json`; datasources provisioned; test start/end annotations. |
| **OAuth2 & JWT** | `auth_mode` switch: `jwt` (login → JWT, re-login when expired), `oauth2` (client-credentials, **one shared token** refreshed under a **Critical Section Controller**), `jwt_local` (HS256 token built in Groovy, no auth server), `form` (CSRF form login). Secrets only from env vars. |
| **WebSocket, JDBC, JMS, Kafka** | `TG-06..09` (0 users by default; enable with `-Jjdbc_users=5` …). JDBC (pool in master plan), JMS Publisher (ActiveMQ/JNDI), Kafka producer (JSR223 + kafka-clients), WebSocket (`java.net.http`, no plugin). |
| **CPU, memory, GC, network** | (a) `TG-10` probe writes `results/lg_health.csv` (CPU, heap, GC count/time, threads, FDs, rx/tx MB/s) and warns above 80% CPU; (b) JMX exporter on `:9404` (heap, GC, threads); (c) `node_exporter` + `cAdvisor` (host/container CPU, memory, network, **CPU throttling**); (d) Prometheus alerts. |

## 4. Load model & key properties

`users` = **total** virtual users. Thread groups: Browse 40%, Search 30%, Cart 15%, Checkout 10% (rate-controlled by `checkout_tps`), Order history 5%.
`duration` = **total seconds including ramp-up** (steady state = `duration − rampup`).

| Property | Default | Meaning |
|---|---|---|
| `host`, `port`, `protocol` | localhost / 8080 / http | Target |
| `auth_mode` | jwt | `jwt` \| `oauth2` \| `jwt_local` \| `form` |
| `users`, `rampup`, `duration` | 50 / 60 / 300 | Load shape (total) |
| `mix_browse/search/cart/checkout/history` | 40/30/15/10/5 | Workload mix % |
| `checkout_tps` | 5 | Target checkout rate |
| `think_min_ms`, `think_range_ms` | 1000 / 3000 | Uniform random think time |
| `workers` | 1 | Set automatically in distributed mode |
| `jdbc_users`, `jms_users`, `kafka_users`, `ws_users` | 0 | Enable protocol traffic |
| `lg_health`, `lg_health_interval_ms` | 1 / 5000 | Generator health probe |
| `influx_url`, `graphite_host/port` | see env files | Metrics targets |
| `testrun`, `app` | local / omp | Tag runs in Grafana |
| `fail_fast` | true | Stop test if `/health` fails in setUp |

Override anything: `./scripts/run_test.sh -Jusers=800 -Jcheckout_tps=40` or `USERS=800 PROFILE=load ./scripts/run_test.sh`.

## 5. Distributed run (Kubernetes)

```bash
python3 scripts/split_csv.py data/users.csv 4          # one slice per worker
docker build -t registry.example.com/perf/omp-jmeter:1.0.0 . && docker push ...
kubectl -n jmeter create secret generic omp-secrets --from-literal=OAUTH_CLIENT_SECRET=...
WORKERS=4 ENV=perf PROFILE=load TESTRUN=build-245 ./k8s/run_distributed.sh
```
Sizing: calibrate ONE worker first (largest users where generator CPU stays < 70%), then `workers = ceil(target_users / users_per_worker)`.

## 6. Quality gate (fail the build on performance)

`scripts/check_thresholds.py` (called automatically by `run_test.sh`): ignores ramp-up, evaluates p95/p99/error% **per transaction** from `thresholds/thresholds.json`, optional baseline regression (`BASELINE=baseline/summary.json`), writes `junit.xml` and `summary.json`.
Exit code **0** pass · **1** SLA breach · **2** regression warning (Jenkins → UNSTABLE, GitLab → allowed failure).
Promote a good run to baseline: `cp results/summary.json baseline/summary.json`.

## 7. Authentication modes in 30 seconds

| `auth_mode` | Flow | When |
|---|---|---|
| `jwt` | POST `/api/auth/login` per user → `Authorization: Bearer <jwt>`; refreshed when `expires_in` elapsed or user changes | Most REST APIs |
| `oauth2` | setUp fetches a client-credentials token; threads re-fetch under a lock only after expiry | Service-to-service / API gateway |
| `jwt_local` | HS256 token signed in Groovy (`JWT_SECRET`) | Services validating signature only |
| `form` | GET `/login` → regex CSRF → POST form | Classic web apps |

## 9. AI integration (advisory, offline-first)

| When | Tool | Result |
|---|---|---|
| Before | `ai/workload_from_logs.py` | Production logs -> users, mix, think time, checkout rate (`from_logs.properties`) |
| Before | `ai/har_to_fragment.py` | Browser recording -> fragment JMX with **auto-correlation** + report |
| After | `ai/analyze_run.py` | Knee point, latency episodes, drift, error clusters, generator validity -> findings with *hypotheses + how to verify*, executive summary, Slack text, Grafana annotation |
| Ongoing | `ai/learn_thresholds.py` | Suggested SLA thresholds learned from history (never auto-applied) |

Switch on in the pipeline: `RUN_AI_ANALYSIS=true` (statistics + rules, no key). Add `ANTHROPIC_API_KEY` to let an LLM refine the narrative. The pass/fail gate stays deterministic and no AI runs during the load test. Try it without JMeter: `python3 ai/selftest.py`. Details, privacy rules and limitations: `docs/AI.md`.

## 8. Honest notes

* The JMX files were generated programmatically and verified as well-formed, structurally valid XML, but **not executed here** (no JMeter available in the authoring sandbox). Run `ENV=local PROFILE=smoke` once; if a **Module Controller** shows *no module selected*, re-pick `FRG - Authenticate` in the GUI (module paths are name-based). Verify the **JMS Publisher** settings in the GUI for your broker.
* Always give JMeter an absolute `base_dir` (the scripts, Docker and K8s do this for you) – Include Controller and CSV paths depend on it.
* Prometheus-via-Graphite metric names depend on sampler names; adjust `monitoring/graphite/mapping.yml` if your labels differ.
* Default creds, anonymous Grafana admin and disabled RMI SSL are **lab settings** – harden for shared environments.
