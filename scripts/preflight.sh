#!/usr/bin/env bash
# Pre-flight: environment health + files + java. Fails fast so no one wastes an hour on a broken env.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
ENV="${ENV:-local}"
get() { grep -hE "^$1=" "config/env/$ENV.properties" | tail -1 | cut -d= -f2-; }
H=$(get host); P=$(get port); S=$(get protocol)
echo "Java:   $(java -version 2>&1 | head -1)"
echo "JMeter: $(jmeter --version 2>&1 | grep -m1 -i 'version' || echo 'not found')"
for f in tests/main/omp_main.jmx data/users.csv data/products.csv data/search_terms.csv thresholds/thresholds.json; do
  [[ -f $f ]] || { echo "MISSING $f"; exit 1; }
done
echo "Disk free: $(df -h . | awk 'NR==2{print $4}')"
URL="$S://$H:$P/health"
CODE=$(curl -ks -o /dev/null -w '%{http_code}' --max-time 10 "$URL" || true)
echo "Health $URL -> $CODE"
[[ "$CODE" == "200" ]] || { echo "Environment unhealthy"; exit 1; }
echo "Pre-flight OK"
