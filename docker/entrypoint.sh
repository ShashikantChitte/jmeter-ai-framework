#!/usr/bin/env bash
# MODE=standalone | controller | worker
set -euo pipefail
mkdir -p /opt/omp/results
cd /opt/omp
MODE="${MODE:-standalone}"
POD_IP="${POD_IP:-$(hostname -i | awk '{print $1}')}"

case "$MODE" in
  standalone)
    exec ./scripts/run_test.sh "$@" ;;

  worker)
    # Statefulset ordinal -> private slice of the user data (no duplicate logins across generators)
    ORD="${HOSTNAME##*-}"; [[ "$ORD" =~ ^[0-9]+$ ]] || ORD=0
    SPLIT="/opt/omp/data/split/users_${ORD}.csv"
    {
      echo "worker_id=$ORD"
      [[ -f "$SPLIT" ]] && echo "users_file=$SPLIT"
      echo "base_dir=/opt/omp"
    } >> "$JMETER_HOME/bin/user.properties"
    export RMI_HOST_DEF="-Djava.rmi.server.hostname=${POD_IP}"
    echo ">> worker $ORD on $POD_IP (data: ${SPLIT})"
    exec "$JMETER_HOME/bin/jmeter-server" \
      -Dserver_port=1099 -Dserver.rmi.localport=50000 -Dserver.rmi.ssl.disable=true \
      -Djava.rmi.server.hostname="${POD_IP}" -Jbase_dir=/opt/omp ;;

  controller)
    export RMI_HOST_DEF="-Djava.rmi.server.hostname=${POD_IP}"
    export JVM_ARGS="${JVM_ARGS} -Djava.rmi.server.hostname=${POD_IP} -Dclient.rmi.localport=7000 -Dserver.rmi.ssl.disable=true"
    exec ./scripts/run_distributed.sh "$@" ;;

  *) echo "Unknown MODE=$MODE"; exit 64 ;;
esac
