#!/usr/bin/env bash
# Master + N workers on separate machines (VMs / bare metal) over SSH.
#   ./scripts/vm_cluster.sh bootstrap   # install Java + JMeter + plugins + libs on every worker (Debian/Ubuntu)
#   ./scripts/vm_cluster.sh deploy      # split data, copy framework, apply tuning + per-worker settings
#   ./scripts/vm_cluster.sh start       # start jmeter-server on every worker
#   ./scripts/vm_cluster.sh status      # check RMI port 1099 on every worker
#   ENV=perf PROFILE=load TESTRUN=b245 ./scripts/vm_cluster.sh run [-Jany=x]   # run from THIS machine (controller)
#   ./scripts/vm_cluster.sh collect     # pull worker-side files (lg_health.csv, gc.log, server logs) to the master
#   ./scripts/vm_cluster.sh stop
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CFG="${CLUSTER_CFG:-$ROOT/config/cluster.env}"
[[ -f "$CFG" ]] || { echo "Create $CFG from config/cluster.env.example"; exit 64; }
# shellcheck disable=SC1090
source "$CFG"
: "${MASTER_IP:?}" "${WORKERS:?}" "${SSH_USER:?}"
REMOTE_DIR="${REMOTE_DIR:-/opt/omp}"
JMETER_VERSION="${JMETER_VERSION:-5.6.3}"
JM_HOME="/opt/apache-jmeter-${JMETER_VERSION}"
WORKER_HEAP="${WORKER_HEAP:--Xms2g -Xmx2g -XX:MaxMetaspaceSize=256m}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10)
[[ -n "${SSH_KEY:-}" ]] && SSH_OPTS+=(-i "${SSH_KEY/#\~/$HOME}")
read -ra W <<<"$WORKERS"
N=${#W[@]}
ssh_w() { local ip=$1; shift; ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"; }

cmd_bootstrap() {
  for ip in "${W[@]}"; do
    echo ">> bootstrap $ip"
    ssh_w "$ip" "JV=$JMETER_VERSION bash -s" <<'REMOTE'
set -e
HOME_J=/opt/apache-jmeter-$JV; M=https://repo1.maven.org/maven2
sudo apt-get update -y >/dev/null
sudo apt-get install -y openjdk-17-jre-headless curl rsync netcat-openbsd >/dev/null
[ -d "$HOME_J" ] || curl -fsSL https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-$JV.tgz | sudo tar -xz -C /opt
sudo chown -R "$USER" "$HOME_J"; sudo mkdir -p /opt/omp; sudo chown "$USER" /opt/omp
cd "$HOME_J/lib/ext"
[ -f jmeter-plugins-manager.jar ] || curl -fsSLO $M/kg/apc/jmeter-plugins-manager/1.10/jmeter-plugins-manager-1.10.jar && mv -f jmeter-plugins-manager-1.10.jar jmeter-plugins-manager.jar 2>/dev/null || true
[ -f ../cmdrunner-2.3.jar ] || curl -fsSL -o ../cmdrunner-2.3.jar $M/kg/apc/cmdrunner/2.3/cmdrunner-2.3.jar
java -cp jmeter-plugins-manager.jar org.jmeterplugins.repository.PluginManagerCMDInstaller >/dev/null
"$HOME_J/bin/PluginsManagerCMD.sh" install jpgc-casutg jpgc-tst jpgc-perfmon jpgc-graphs-basic
cd "$HOME_J/lib"
for a in org/postgresql/postgresql/42.7.3/postgresql-42.7.3.jar org/apache/kafka/kafka-clients/3.7.0/kafka-clients-3.7.0.jar org/apache/activemq/activemq-all/5.18.4/activemq-all-5.18.4.jar; do
  [ -f "$(basename $a)" ] || curl -fsSLO "$M/$a"
done
echo "bootstrap OK: $(java -version 2>&1 | head -1)"
REMOTE
  done
}

cmd_deploy() {
  python3 "$ROOT/scripts/split_csv.py" "$ROOT/data/users.csv" "$N"       # one slice of accounts per worker
  local i=0
  for ip in "${W[@]}"; do
    echo ">> deploy $ip (worker_id=$i)"
    ssh_w "$ip" "mkdir -p $REMOTE_DIR/results"
    rsync -az --delete -e "ssh ${SSH_OPTS[*]}" --exclude results --exclude .git --exclude config/cluster.env \
          "$ROOT/" "$SSH_USER@$ip:$REMOTE_DIR/"
    # idempotent block in the worker's user.properties: tuning + identity + data slice
    ssh_w "$ip" "f=$JM_HOME/bin/user.properties; sed -i '/# OMP-BEGIN/,/# OMP-END/d' \$f; \
      { echo '# OMP-BEGIN'; cat $REMOTE_DIR/config/jmeter/tuning.properties; \
        echo 'worker_id=$i'; echo 'users_file=$REMOTE_DIR/data/split/users_$i.csv'; echo 'base_dir=$REMOTE_DIR'; \
        echo '# OMP-END'; } >> \$f"
    i=$((i+1))
  done
}

cmd_start() {
  for ip in "${W[@]}"; do
    echo ">> start worker $ip"
    ssh_w "$ip" "pkill -f jmeter-server.log || true; sleep 1; cd $REMOTE_DIR && mkdir -p results; \
      [ -f ~/.omp_secrets ] && . ~/.omp_secrets; \
      RMI_HOST_DEF='-Djava.rmi.server.hostname=$ip' HEAP='$WORKER_HEAP' \
      GC_ALGO='-XX:+UseG1GC -XX:MaxGCPauseMillis=100 -XX:G1ReservePercent=20' \
      JVM_ARGS='-Xlog:gc*:file=$REMOTE_DIR/results/gc.log:time,uptime,level,tags:filecount=5,filesize=20m' \
      nohup $JM_HOME/bin/jmeter-server -Dserver_port=1099 -Dserver.rmi.localport=50000 -Dserver.rmi.ssl.disable=true \
        -Djava.rmi.server.hostname=$ip -Jbase_dir=$REMOTE_DIR > results/jmeter-server.out 2>&1 < /dev/null &"
  done
  sleep 6; cmd_status
}

cmd_status() {
  local bad=0
  for ip in "${W[@]}"; do
    if timeout 3 bash -c "echo > /dev/tcp/$ip/1099" 2>/dev/null; then echo "UP    $ip:1099"; else echo "DOWN  $ip:1099"; bad=1; fi
  done
  return $bad
}

cmd_stop() { for ip in "${W[@]}"; do echo ">> stop $ip"; ssh_w "$ip" "pkill -f jmeter-server.log || true"; done; }

cmd_run() {
  [[ "$ROOT" == "$REMOTE_DIR" ]] || echo "WARNING: master path $ROOT != REMOTE_DIR $REMOTE_DIR - keep the SAME absolute path on all machines."
  cmd_status || { echo "Some workers are down - run '$0 start'"; exit 1; }
  local hosts; hosts=$(IFS=,; echo "${W[*]}")
  # the controller must advertise an address the workers can reach (they call back over RMI)
  export RMI_HOST_DEF="-Djava.rmi.server.hostname=$MASTER_IP"
  export JVM_ARGS="${JVM_ARGS:-} -Djava.rmi.server.hostname=$MASTER_IP -Dclient.rmi.localport=7000 -Dserver.rmi.ssl.disable=true"
  REMOTE_HOSTS="$hosts" "$ROOT/scripts/run_test.sh" "$@"
}

cmd_collect() {
  for ip in "${W[@]}"; do
    d="$ROOT/results/workers/$ip"; mkdir -p "$d"
    for f in results/lg_health.csv results/gc.log results/jmeter-server.out jmeter-server.log; do
      scp "${SSH_OPTS[@]}" -q "$SSH_USER@$ip:$REMOTE_DIR/$f" "$d/" 2>/dev/null || true
    done
    echo ">> collected $ip -> $d"
  done
}

case "${1:-}" in
  bootstrap) cmd_bootstrap ;; deploy) cmd_deploy ;; start) cmd_start ;; status) cmd_status ;;
  stop) cmd_stop ;; run) shift; cmd_run "$@" ;; collect) cmd_collect ;;
  *) sed -n '2,11p' "$0"; exit 64 ;;
esac
