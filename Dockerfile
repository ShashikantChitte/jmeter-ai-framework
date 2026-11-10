# Consistent, pinned JMeter image used for standalone, controller and worker roles.
ARG JAVA_IMAGE=eclipse-temurin:17-jre-jammy
FROM ${JAVA_IMAGE}

ARG JMETER_VERSION=5.6.3
ARG PLUGINS_MANAGER_VERSION=1.10
ARG CMDRUNNER_VERSION=2.3
ARG KAFKA_CLIENTS_VERSION=3.7.0
ARG POSTGRES_JDBC_VERSION=42.7.3
ARG ACTIVEMQ_VERSION=5.18.4
ARG JMX_EXPORTER_VERSION=0.20.0
ARG MAVEN=https://repo1.maven.org/maven2

ENV JMETER_HOME=/opt/apache-jmeter-${JMETER_VERSION} \
    PATH=/opt/apache-jmeter-${JMETER_VERSION}/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates python3 procps iputils-ping dnsutils netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# JMeter
RUN curl -fsSL https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz | tar -xz -C /opt

# Plugins: Concurrency Thread Group, Throughput Shaping Timer, PerfMon, basic graphs
RUN curl -fsSL -o ${JMETER_HOME}/lib/ext/jmeter-plugins-manager.jar ${MAVEN}/kg/apc/jmeter-plugins-manager/${PLUGINS_MANAGER_VERSION}/jmeter-plugins-manager-${PLUGINS_MANAGER_VERSION}.jar \
    && curl -fsSL -o ${JMETER_HOME}/lib/cmdrunner-${CMDRUNNER_VERSION}.jar ${MAVEN}/kg/apc/cmdrunner/${CMDRUNNER_VERSION}/cmdrunner-${CMDRUNNER_VERSION}.jar \
    && java -cp ${JMETER_HOME}/lib/ext/jmeter-plugins-manager.jar org.jmeterplugins.repository.PluginManagerCMDInstaller \
    && ${JMETER_HOME}/bin/PluginsManagerCMD.sh install jpgc-casutg jpgc-tst jpgc-perfmon jpgc-graphs-basic

# Protocol libraries: Kafka, JDBC (PostgreSQL), JMS (ActiveMQ)
RUN curl -fsSL -o ${JMETER_HOME}/lib/kafka-clients-${KAFKA_CLIENTS_VERSION}.jar ${MAVEN}/org/apache/kafka/kafka-clients/${KAFKA_CLIENTS_VERSION}/kafka-clients-${KAFKA_CLIENTS_VERSION}.jar \
    && curl -fsSL -o ${JMETER_HOME}/lib/postgresql-${POSTGRES_JDBC_VERSION}.jar ${MAVEN}/org/postgresql/postgresql/${POSTGRES_JDBC_VERSION}/postgresql-${POSTGRES_JDBC_VERSION}.jar \
    && curl -fsSL -o ${JMETER_HOME}/lib/activemq-all-${ACTIVEMQ_VERSION}.jar ${MAVEN}/org/apache/activemq/activemq-all/${ACTIVEMQ_VERSION}/activemq-all-${ACTIVEMQ_VERSION}.jar

# JMX Prometheus exporter: exposes the JMeter JVM (heap, GC, threads, CPU) on :9404
RUN mkdir -p /opt/jmx_exporter \
    && curl -fsSL -o /opt/jmx_exporter/jmx_prometheus_javaagent.jar ${MAVEN}/io/prometheus/jmx/jmx_prometheus_javaagent/${JMX_EXPORTER_VERSION}/jmx_prometheus_javaagent-${JMX_EXPORTER_VERSION}.jar

# Framework (scripts, fragments, data, config) baked into the image => identical on every worker
COPY . /opt/omp
WORKDIR /opt/omp
RUN cat config/jmeter/tuning.properties >> ${JMETER_HOME}/bin/user.properties \
    && chmod +x scripts/*.sh docker/entrypoint.sh

# ---- JVM tuning for high load (override with -e HEAP=... at runtime)
#  * fixed heap (Xms=Xmx) avoids resize pauses
#  * G1 with a pause goal keeps GC from distorting latency
#  * GC log + JMX exporter let you SEE generator CPU / memory / GC
ENV HEAP="-Xms2g -Xmx2g -XX:MaxMetaspaceSize=256m" \
    GC_ALGO="-XX:+UseG1GC -XX:MaxGCPauseMillis=100 -XX:G1ReservePercent=20" \
    JVM_ARGS="-javaagent:/opt/jmx_exporter/jmx_prometheus_javaagent.jar=9404:/opt/omp/monitoring/jmx_exporter/jmeter-jvm.yml -Xlog:gc*:file=/opt/omp/results/gc.log:time,uptime,level,tags:filecount=5,filesize=20m -Djava.net.preferIPv4Stack=true -Dnetworkaddress.cache.ttl=60" \
    MODE=standalone

EXPOSE 1099 50000 7000 9404
ENTRYPOINT ["/opt/omp/docker/entrypoint.sh"]
