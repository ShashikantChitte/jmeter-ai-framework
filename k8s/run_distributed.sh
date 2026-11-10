#!/usr/bin/env bash
# One-shot distributed run on Kubernetes:
#   WORKERS=4 ENV=perf PROFILE=load TESTRUN=build-245 ./k8s/run_distributed.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${WORKERS:=3}" "${ENV:=perf}" "${PROFILE:=load}" "${TESTRUN:=k8s-$(date +%Y%m%d-%H%M%S)}" "${OUT:=results}"
NS=jmeter
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/20-jmeter-workers.yaml
kubectl -n $NS scale sts/jmeter-worker --replicas="$WORKERS"
kubectl -n $NS rollout status sts/jmeter-worker --timeout=300s

kubectl -n $NS delete job jmeter-controller --ignore-not-found
sed -e "s/__WORKERS__/$WORKERS/" -e "s/__ENV__/$ENV/" -e "s/__PROFILE__/$PROFILE/" -e "s/__TESTRUN__/$TESTRUN/" \
    k8s/30-jmeter-controller-job.yaml | kubectl apply -f -
kubectl -n $NS wait --for=condition=ready pod -l app=jmeter-controller --timeout=300s || true
kubectl -n $NS logs -f job/jmeter-controller || true

# collect results (controller pod is kept by ttlSecondsAfterFinished)
POD=$(kubectl -n $NS get pod -l app=jmeter-controller -o jsonpath='{.items[0].metadata.name}')
mkdir -p "$OUT"; kubectl -n $NS cp "$POD:/opt/omp/results" "$OUT" || true
RC=$(kubectl -n $NS get pod "$POD" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || echo 1)
[[ "${KEEP_WORKERS:-false}" == "true" ]] || kubectl -n $NS scale sts/jmeter-worker --replicas=0   # cost control
echo "controller exit code: $RC (0 pass | 1 SLA breach | 2 regression warning)"
exit "${RC:-1}"
