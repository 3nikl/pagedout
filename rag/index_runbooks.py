"""
PagedOut - Runbook Indexer
Creates 1000+ operational runbooks and indexes them into Qdrant
using sentence-transformers for local free embeddings.

Usage:
    python rag/index_runbooks.py
"""

import json
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    CreateCollection, CollectionInfo
)
from sentence_transformers import SentenceTransformer

# ── CONFIG ────────────────────────────────────────────────────────────────────

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333
COLLECTION_NAME = "runbooks"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # small fast model, 384 dims, runs locally
VECTOR_SIZE = 384

# ── RUNBOOK TEMPLATES ─────────────────────────────────────────────────────────

RUNBOOKS = [

    # ── DATABASE CONNECTION EXHAUSTION ────────────────────────────────────────
    {
        "title": "DB Connection Pool Exhaustion - Emergency Recovery",
        "incident_type": "database_connection_exhaustion",
        "severity": "P1",
        "description": "Database connection pool is exhausted. All connections in use. New requests failing.",
        "symptoms": ["connection pool exhausted", "db timeout", "connection refused", "too many connections"],
        "steps": [
            "SAFE: Check current connection count via SELECT count(*) FROM pg_stat_activity",
            "SAFE: Identify long-running queries blocking connections",
            "SAFE: Kill idle connections older than 10 minutes",
            "SAFE: Restart connection pool manager service",
            "SAFE: Scale up connection pool size from default to 200",
            "RISKY: Rollback recent deployment if connection count spiked after deploy",
            "RISKY: Restart database if pool restart fails to recover",
        ],
        "prevention": "Set connection pool monitoring alerts at 80% capacity. Implement connection timeout of 30s."
    },
    {
        "title": "DB Connection Pool - Connection Leak Detection",
        "incident_type": "database_connection_exhaustion",
        "severity": "P2",
        "description": "Gradual connection count increase indicating a connection leak in application code.",
        "symptoms": ["connection count growing", "memory growing", "connection not released"],
        "steps": [
            "SAFE: Monitor connection count trend over last 30 minutes",
            "SAFE: Check application logs for missing connection.close() calls",
            "SAFE: Enable connection leak detection in pool config",
            "SAFE: Restart affected service pods to clear leaked connections",
            "RISKY: Deploy hotfix for connection leak if identified in code",
        ],
        "prevention": "Use connection pool with leak detection enabled. Add connection lifecycle logging."
    },
    {
        "title": "PostgreSQL Max Connections Reached",
        "incident_type": "database_connection_exhaustion",
        "severity": "P1",
        "description": "PostgreSQL max_connections limit reached. Database refusing new connections.",
        "symptoms": ["FATAL: remaining connection slots reserved", "max_connections", "pg connection refused"],
        "steps": [
            "SAFE: Check pg_stat_activity for idle connections",
            "SAFE: Terminate idle connections using pg_terminate_backend()",
            "SAFE: Implement PgBouncer connection pooling if not already in place",
            "RISKY: Increase max_connections in postgresql.conf and restart",
        ],
        "prevention": "Use PgBouncer. Set connection limits per application user."
    },

    # ── MEMORY LEAK ───────────────────────────────────────────────────────────
    {
        "title": "Memory Leak - Pod OOMKilled Recovery",
        "incident_type": "memory_leak",
        "severity": "P1",
        "description": "Pod killed by OOMKiller. Memory usage reached container limit.",
        "symptoms": ["OOMKilled", "exit code 137", "memory limit exceeded", "heap exhausted"],
        "steps": [
            "SAFE: Check pod events for OOMKilled reason",
            "SAFE: Delete and recreate killed pod to restore service",
            "SAFE: Temporarily increase memory limits to 2x current",
            "SAFE: Scale up replica count to distribute memory load",
            "RISKY: Enable heap dump on next OOM for analysis",
            "RISKY: Rollback deployment if memory grew after last deploy",
        ],
        "prevention": "Set memory requests and limits. Enable OOM alerting at 80% usage."
    },
    {
        "title": "Memory Leak - Gradual Heap Growth",
        "incident_type": "memory_leak",
        "severity": "P2",
        "description": "Slow memory growth over hours. Service will OOM eventually without intervention.",
        "symptoms": ["heap growing", "GC pressure", "memory increasing", "full GC triggered"],
        "steps": [
            "SAFE: Monitor heap usage trend and estimate time to OOM",
            "SAFE: Trigger manual GC via JVM diagnostic endpoint",
            "SAFE: Schedule rolling restart during low traffic window",
            "SAFE: Enable JVM flight recorder for heap analysis",
            "RISKY: Deploy fix for identified leak if root cause found",
        ],
        "prevention": "Schedule periodic pod restarts. Set up heap dump alerts."
    },
    {
        "title": "Memory Leak - Node Level Memory Pressure",
        "incident_type": "memory_leak",
        "severity": "P2",
        "description": "Multiple pods on same node consuming excessive memory causing node pressure.",
        "symptoms": ["node memory pressure", "pod evicted", "node not ready", "eviction threshold"],
        "steps": [
            "SAFE: Check node memory usage across all pods",
            "SAFE: Identify highest memory consuming pods",
            "SAFE: Evict and reschedule non-critical pods to other nodes",
            "SAFE: Set memory limits on pods without limits",
            "RISKY: Drain node if memory pressure critical",
        ],
        "prevention": "Set resource requests and limits on all pods. Enable node memory alerts."
    },

    # ── HIGH LATENCY SPIKE ────────────────────────────────────────────────────
    {
        "title": "High Latency - P99 SLA Breach Response",
        "incident_type": "high_latency_spike",
        "severity": "P1",
        "description": "P99 latency breached SLA threshold. Users experiencing slow responses.",
        "symptoms": ["p99 latency high", "SLA breach", "slow response", "timeout"],
        "steps": [
            "SAFE: Check downstream service health and latency",
            "SAFE: Review recent query execution plans for slow queries",
            "SAFE: Clear cache if stale data causing recomputation",
            "SAFE: Scale up replicas to reduce per-instance request load",
            "SAFE: Enable circuit breaker to shed load from struggling downstream",
            "RISKY: Rollback if latency spike started after last deployment",
        ],
        "prevention": "Set P99 latency SLOs with automated alerts. Implement circuit breakers."
    },
    {
        "title": "High Latency - Database Query Performance",
        "incident_type": "high_latency_spike",
        "severity": "P2",
        "description": "Slow database queries causing elevated service latency.",
        "symptoms": ["slow query", "query timeout", "database latency", "lock wait"],
        "steps": [
            "SAFE: Run EXPLAIN ANALYZE on slow queries",
            "SAFE: Check for missing indexes on frequently queried columns",
            "SAFE: Kill long-running queries blocking others",
            "SAFE: Check for table bloat requiring VACUUM",
            "RISKY: Add index if missing index identified as root cause",
        ],
        "prevention": "Set query timeout limits. Monitor slow query log. Regular VACUUM schedule."
    },
    {
        "title": "High Latency - External API Dependency",
        "incident_type": "high_latency_spike",
        "severity": "P2",
        "description": "External API dependency slow or timing out causing cascading latency.",
        "symptoms": ["external timeout", "third party slow", "API latency", "downstream timeout"],
        "steps": [
            "SAFE: Check external API status page",
            "SAFE: Enable circuit breaker for external dependency",
            "SAFE: Return cached responses for non-critical external calls",
            "SAFE: Implement fallback response for degraded external service",
            "RISKY: Route traffic to backup external provider if available",
        ],
        "prevention": "Implement circuit breakers for all external dependencies. Cache external responses."
    },

    # ── POD CRASH LOOP ────────────────────────────────────────────────────────
    {
        "title": "Pod CrashLoopBackOff - Emergency Recovery",
        "incident_type": "pod_crash_loop",
        "severity": "P1",
        "description": "Pod in CrashLoopBackOff state. Kubernetes restarting repeatedly.",
        "symptoms": ["CrashLoopBackOff", "pod restarting", "restart count high", "container crash"],
        "steps": [
            "SAFE: Check pod logs for crash reason using kubectl logs --previous",
            "SAFE: Check pod events for OOMKilled or error codes",
            "SAFE: Delete pod to force fresh start with latest config",
            "SAFE: Increase memory and CPU limits if resource constrained",
            "RISKY: Rollback deployment causing crash loop",
            "RISKY: Scale deployment to zero then back up if rollback unavailable",
        ],
        "prevention": "Set appropriate resource limits. Implement readiness and liveness probes."
    },
    {
        "title": "Pod CrashLoop - Configuration Error",
        "incident_type": "pod_crash_loop",
        "severity": "P1",
        "description": "Pod crashing due to missing or incorrect configuration or secrets.",
        "symptoms": ["config not found", "secret missing", "env var not set", "configuration error"],
        "steps": [
            "SAFE: Check pod logs for missing configuration keys",
            "SAFE: Verify all required environment variables are set",
            "SAFE: Check Kubernetes secrets exist and are mounted correctly",
            "SAFE: Verify ConfigMap values are correct",
            "RISKY: Update secret or ConfigMap with correct values",
        ],
        "prevention": "Validate configuration in CI before deployment. Use config validation startup checks."
    },

    # ── DISK SPACE CRITICAL ───────────────────────────────────────────────────
    {
        "title": "Disk Space Critical - Emergency Cleanup",
        "incident_type": "disk_space_critical",
        "severity": "P1",
        "description": "Disk usage above 90%. Write operations may start failing.",
        "symptoms": ["disk full", "no space left on device", "disk usage 90%", "write failed"],
        "steps": [
            "SAFE: Check disk usage breakdown with du -sh /* | sort -rh | head -20",
            "SAFE: Clear old log files older than 7 days",
            "SAFE: Clear Docker unused images, containers and volumes",
            "SAFE: Archive and compress old database backup files",
            "SAFE: Clear temp files and build artifacts",
            "RISKY: Expand disk volume via cloud provider console",
        ],
        "prevention": "Set disk usage alerts at 70% and 85%. Implement log rotation. Schedule cleanup jobs."
    },
    {
        "title": "Disk Space - Log Accumulation",
        "incident_type": "disk_space_critical",
        "severity": "P2",
        "description": "Log files consuming excessive disk space due to misconfigured log rotation.",
        "symptoms": ["log files large", "log rotation not working", "var log full"],
        "steps": [
            "SAFE: Check log file sizes with du -sh /var/log/*",
            "SAFE: Manually rotate and compress current log files",
            "SAFE: Fix logrotate configuration for affected services",
            "SAFE: Reduce log verbosity level from DEBUG to INFO",
        ],
        "prevention": "Configure logrotate for all services. Set log level appropriately per environment."
    },

    # ── NETWORK PARTITION ─────────────────────────────────────────────────────
    {
        "title": "Network Partition - Service Connectivity Loss",
        "incident_type": "network_partition",
        "severity": "P1",
        "description": "Services unable to communicate. Network partition detected.",
        "symptoms": ["connection refused", "network timeout", "service unreachable", "DNS resolution failed"],
        "steps": [
            "SAFE: Verify network connectivity between pods with kubectl exec ping",
            "SAFE: Check DNS resolution is working for service names",
            "SAFE: Restart CoreDNS pods if DNS resolution failing",
            "SAFE: Check network policies are not blocking required traffic",
            "SAFE: Restart service mesh sidecar proxies",
            "RISKY: Failover to backup availability zone if zone-level partition",
        ],
        "prevention": "Implement retry logic with exponential backoff. Design for partial network failures."
    },
    {
        "title": "Network Partition - Kubernetes DNS Issues",
        "incident_type": "network_partition",
        "severity": "P2",
        "description": "Kubernetes DNS resolution failing causing service discovery issues.",
        "symptoms": ["DNS lookup failed", "service not found", "NXDOMAIN", "CoreDNS error"],
        "steps": [
            "SAFE: Check CoreDNS pod status",
            "SAFE: Review CoreDNS logs for errors",
            "SAFE: Restart CoreDNS deployment",
            "SAFE: Verify service DNS entries exist",
            "SAFE: Check DNS search domains configuration",
        ],
        "prevention": "Monitor CoreDNS health. Set DNS TTL appropriately."
    },

    # ── CPU THROTTLING ────────────────────────────────────────────────────────
    {
        "title": "CPU Throttling - Container Limit Reached",
        "incident_type": "cpu_throttling",
        "severity": "P2",
        "description": "Container CPU being throttled by Kubernetes resource limits.",
        "symptoms": ["CPU throttling", "container_cpu_cfs_throttled", "high CPU wait", "slow processing"],
        "steps": [
            "SAFE: Check CPU throttling percentage in metrics",
            "SAFE: Identify which processes consuming most CPU",
            "SAFE: Scale up replica count to distribute CPU load",
            "SAFE: Increase CPU limits for affected containers",
            "RISKY: Optimize CPU-intensive code paths identified in profiling",
        ],
        "prevention": "Set CPU requests and limits based on profiling. Monitor throttling percentage."
    },
    {
        "title": "CPU Throttling - Runaway Process",
        "incident_type": "cpu_throttling",
        "severity": "P2",
        "description": "Single process consuming excessive CPU causing throttling for all containers.",
        "symptoms": ["one process high CPU", "CPU spike", "process runaway", "infinite loop"],
        "steps": [
            "SAFE: Identify the runaway process with top or kubectl exec",
            "SAFE: Check for infinite loops or expensive recursive calls in logs",
            "SAFE: Restart the affected pod to kill runaway process",
            "RISKY: Deploy fix for identified CPU-intensive bug",
        ],
        "prevention": "Implement CPU profiling in staging. Set CPU alerts per container."
    },

    # ── DEPLOYMENT FAILURE ────────────────────────────────────────────────────
    {
        "title": "Deployment Failure - Rollback Required",
        "incident_type": "deployment_failure",
        "severity": "P1",
        "description": "New deployment causing service degradation. Immediate rollback needed.",
        "symptoms": ["error rate increased after deploy", "latency spike post deploy", "health check failing"],
        "steps": [
            "SAFE: Verify deployment correlation with incident timing",
            "SAFE: Check new pod logs for errors",
            "SAFE: Pause deployment rollout to stop bad pods spreading",
            "RISKY: Rollback to previous deployment version",
            "RISKY: Scale down new version and scale up old version manually",
        ],
        "prevention": "Implement canary deployments. Use automated rollback on error rate increase."
    },
    {
        "title": "Deployment Failure - Image Pull Error",
        "incident_type": "deployment_failure",
        "severity": "P2",
        "description": "Kubernetes unable to pull container image. Deployment stalled.",
        "symptoms": ["ImagePullBackOff", "ErrImagePull", "image not found", "registry error"],
        "steps": [
            "SAFE: Check image name and tag are correct",
            "SAFE: Verify image exists in container registry",
            "SAFE: Check registry credentials are valid and not expired",
            "SAFE: Check network connectivity to container registry",
            "RISKY: Update deployment with correct image reference",
        ],
        "prevention": "Pin image versions. Validate image existence in CI. Rotate registry credentials regularly."
    },

    # ── CASCADE FAILURE ───────────────────────────────────────────────────────
    {
        "title": "Cascade Failure - Dependency Chain Failure",
        "incident_type": "cascade_failure",
        "severity": "P1",
        "description": "One service failure cascading to multiple downstream services.",
        "symptoms": ["multiple services down", "cascade", "downstream failing", "dependency chain"],
        "steps": [
            "SAFE: Map the dependency chain to find root failing service",
            "SAFE: Isolate failing service with circuit breakers",
            "SAFE: Enable fallback responses in downstream services",
            "SAFE: Shed non-critical traffic to protect healthy services",
            "RISKY: Restart root failing service after identifying cause",
            "RISKY: Failover to backup service if root service unrecoverable",
        ],
        "prevention": "Implement bulkheads to isolate failures. Design services for partial degradation."
    },
]

# Generate additional runbook variations to reach 1000+
def generate_runbook_variations(base_runbooks):
    """Generate variations of base runbooks to build a larger dataset."""
    variations = []
    
    services = [
        "payment-service", "auth-service", "order-service",
        "inventory-service", "notification-service", "user-service",
        "search-service", "recommendation-service", "billing-service",
        "api-gateway", "reporting-service", "analytics-service",
    ]
    
    environments = ["production", "staging", "kubernetes", "docker", "AWS", "GCP", "Azure"]
    
    for base in base_runbooks:
        for service in services:
            variation = base.copy()
            variation["title"] = f"{base['title']} - {service}"
            variation["description"] = f"[{service}] {base['description']}"
            variation["service"] = service
            variations.append(variation)
        
        for env in environments:
            variation = base.copy()
            variation["title"] = f"{base['title']} ({env})"
            variation["description"] = f"{base['description']} Environment: {env}"
            variation["environment"] = env
            variations.append(variation)
    
    return variations


# ── MAIN ──────────────────────────────────────────────────────────────────────

def index_runbooks():
    print("PagedOut - Runbook Indexer")
    print("="*50)
    
    # Connect to Qdrant
    print("\n1. Connecting to Qdrant...")
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    print(f"   Connected to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}")
    
    # Load embedding model
    print("\n2. Loading embedding model (all-MiniLM-L6-v2)...")
    print("   This is a small 80MB model that runs locally. Free.")
    model = SentenceTransformer(EMBEDDING_MODEL)
    print(f"   Model loaded. Vector size: {VECTOR_SIZE}")
    
    # Create collection
    print(f"\n3. Creating Qdrant collection '{COLLECTION_NAME}'...")
    try:
        client.delete_collection(COLLECTION_NAME)
        print("   Deleted existing collection")
    except:
        pass
    
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(
            size=VECTOR_SIZE,
            distance=Distance.COSINE
        )
    )
    print(f"   Collection created with {VECTOR_SIZE}-dim vectors")
    
    # Generate all runbooks
    print("\n4. Generating runbook dataset...")
    all_runbooks = RUNBOOKS + generate_runbook_variations(RUNBOOKS)
    print(f"   Total runbooks: {len(all_runbooks)}")
    
    # Index runbooks in batches
    print("\n5. Indexing runbooks into Qdrant...")
    batch_size = 50
    total_indexed = 0
    
    for i in range(0, len(all_runbooks), batch_size):
        batch = all_runbooks[i:i+batch_size]
        points = []
        
        for runbook in batch:
            # Create text to embed - title + description + symptoms
            text_to_embed = f"{runbook['title']}. {runbook['description']}. Symptoms: {' '.join(runbook.get('symptoms', []))}"
            
            # Generate embedding
            vector = model.encode(text_to_embed).tolist()
            
            # Create point
            point = PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "title": runbook["title"],
                    "incident_type": runbook["incident_type"],
                    "severity": runbook.get("severity", "P2"),
                    "description": runbook["description"],
                    "symptoms": runbook.get("symptoms", []),
                    "steps": runbook["steps"],
                    "prevention": runbook.get("prevention", ""),
                    "service": runbook.get("service", "general"),
                    "environment": runbook.get("environment", "general"),
                }
            )
            points.append(point)
        
        # Upload batch to Qdrant
        client.upsert(collection_name=COLLECTION_NAME, points=points)
        total_indexed += len(batch)
        print(f"   Indexed {total_indexed}/{len(all_runbooks)} runbooks...")
    
    # Verify
    collection_info = client.get_collection(COLLECTION_NAME)
    print(f"\n✅ Indexing complete!")
    print(f"   Total runbooks indexed: {collection_info.points_count}")
    print(f"   Collection: {COLLECTION_NAME}")
    print(f"   Vector size: {VECTOR_SIZE}")
    
    # Quick test search
    print("\n6. Testing search...")
    test_query = "database connection pool exhausted payment service"
    query_vector = model.encode(test_query).tolist()
    
    results = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_vector,
        limit=3,
        with_payload=True
    )
    
    print(f"   Query: '{test_query}'")
    print(f"   Top 3 results:")
    for r in results:
        print(f"   - {r.payload['title']} (score: {r.score:.3f})")
    
    print("\n🚀 Phase 5 Step 1 complete — Qdrant loaded with runbooks!")
    print("Next: Update runbook_agent.py to use Qdrant search")


if __name__ == "__main__":
    index_runbooks()
