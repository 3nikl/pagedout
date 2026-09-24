# Flink with the Kafka SQL connector baked in.
#
# Deliberately the plain Java image rather than PyFlink: this project is
# developed on arm64, where apache-flink's Python wheels are unreliable.
# The whole ingestion job is expressed in Flink SQL, so no Python runtime
# is needed inside the cluster.

FROM flink:1.20-java17

ARG KAFKA_CONNECTOR_VERSION=3.3.0-1.20

# SQL connector is an uber-jar: it bundles the Kafka client, so nothing else
# is needed for the source and sink tables.
RUN curl -fsSL -o /opt/flink/lib/flink-sql-connector-kafka.jar \
      "https://repo1.maven.org/maven2/org/apache/flink/flink-sql-connector-kafka/${KAFKA_CONNECTOR_VERSION}/flink-sql-connector-kafka-${KAFKA_CONNECTOR_VERSION}.jar" \
 && chown flink:flink /opt/flink/lib/flink-sql-connector-kafka.jar

# RocksDB state backend ships in the distribution's opt/ directory; move it
# onto the classpath so `state.backend.type: rocksdb` resolves.
RUN cp /opt/flink/opt/flink-statebackend-rocksdb*.jar /opt/flink/lib/ 2>/dev/null || true

WORKDIR /opt/flink
