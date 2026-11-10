# Master + multiple workers on separate machines

```
            ┌────────── MASTER / controller (10.0.0.10) ──────────┐
            │ scripts/run_test.sh  -R w1,w2,w3                    │
            │  • sends omp_main.jmx to every worker (RMI :1099)   │
            │  • RECEIVES all samples  ─► results/results.jtl     │
            │  • builds HTML report, runs quality gate, junit     │
            └──────▲──────────────▲──────────────▲────────────────┘
        samples back│ (RMI)       │              │
         ┌──────────┴──┐   ┌──────┴──────┐   ┌───┴─────────┐
         │ worker 1    │   │ worker 2    │   │ worker 3    │   jmeter-server (1099, 50000)
         │ users/3     │   │ users/3     │   │ users/3     │   each runs the FULL plan
         └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
                └────────► target application ◄─────┘
         each worker also streams live metrics ─► InfluxDB ─► Grafana
```
The plan divides `users` by `workers` automatically: `-Jusers=600` on 3 workers = 200 users per worker.

## One-time setup (from the master)
```bash
cp config/cluster.env.example config/cluster.env     # edit MASTER_IP, WORKERS, SSH_USER, SSH_KEY
ssh-copy-id perf@10.0.0.11 ...                       # key-based SSH to every worker (+ sudo for bootstrap)
./scripts/vm_cluster.sh bootstrap                    # Java 17 + JMeter + plugins + drivers on each worker
./scripts/vm_cluster.sh deploy                       # splits users.csv, rsyncs framework, applies tuning
./scripts/vm_cluster.sh start && ./scripts/vm_cluster.sh status
```
Secrets: put `export OAUTH_CLIENT_SECRET=...` in `~/.omp_secrets` on each worker (chmod 600); `start` sources it.

## Run and collect
```bash
ENV=perf PROFILE=load TESTRUN=build-245 ./scripts/vm_cluster.sh run
./scripts/vm_cluster.sh collect        # worker-side files -> results/workers/<ip>/
```

## Network / firewall
| From → To | Port | Why |
|---|---|---|
| master → workers | 1099, 50000 | RMI registry and data |
| workers → master | 7000 | RMI callback (`client.rmi.localport`) |
| workers → target app | 80/443 | load |
| workers → InfluxDB | 8086 | live metrics (use the master/Influx IP in `influx_url`, NOT localhost) |
| Prometheus → workers | 9404 / 9100 | optional JVM + node metrics |

## What comes back to the master
| Data | Where it ends up |
|---|---|
| All samples from all workers | `results/results.jtl` (merged) → `results/report/index.html`, gate, `junit.xml`, `summary.json` |
| Failed samples with detail | `results/errors.jtl` (Error Writer listener runs on the master) |
| Live metrics | InfluxDB/Grafana (sent directly by each worker) |
| Worker CPU/heap/GC/network, logs | stay on workers → `vm_cluster.sh collect` |

## Rules that prevent most problems
1. Same JMeter version, Java version, plugins and **same absolute framework path** on every machine.
2. `-G` (not `-J`) for properties that workers must see; `run_test.sh` already does this.
3. RMI SSL: either disabled everywhere (lab, `server.rmi.ssl.disable=true`) or the same `rmi_keystore.jks` on all nodes.
4. Keep generator CPU < 70-80% (Load-Generator dashboard / `lg_health.csv`); add workers if above.
5. Percentiles shown in Grafana are averaged across workers (approximate); the **authoritative** percentiles are the master's JTL / HTML report.
