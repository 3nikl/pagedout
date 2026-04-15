"""
PagedOut - Investigator Agent
Investigates the incident using tool calls.
Queries metrics, logs, and deployment history.
Uses ReAct pattern - reasons then acts then observes.
"""

import json
import random
from datetime import datetime
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool


# ── TOOLS (simulated for now - replace with real APIs later) ──────────────────

@tool
def query_prometheus(service: str, metric: str) -> str:
    """Query Prometheus for current metric value for a service.
    Use this to get live metrics like error_rate, db_connections, latency_p99, memory_usage, cpu_usage."""
    # Simulated metrics - replace with real Prometheus API call later
    metrics = {
        "error_rate": round(random.uniform(0.3, 0.9), 2),
        "db_connections": random.randint(85, 100),
        "latency_p99": random.randint(2000, 8000),
        "memory_usage": random.randint(80, 99),
        "cpu_usage": random.randint(70, 95),
    }
    value = metrics.get(metric, random.uniform(0, 100))
    return f"{service} {metric}: {value} (queried at {datetime.now().strftime('%H:%M:%S')})"


@tool
def query_recent_logs(service: str, minutes: int = 5) -> str:
    """Query recent error logs for a service.
    Use this to find error patterns and when they started."""
    # Simulated logs - replace with real Elasticsearch/log API later
    log_patterns = {
        "database_connection_exhaustion": [
            f"ERROR: Connection pool exhausted 99/100 on {service}",
            f"WARN: DB connection wait time 4500ms on {service}",
            f"ERROR: Unable to acquire connection after timeout on {service}",
        ],
        "memory_leak": [
            f"WARN: Heap usage at 94% on {service}",
            f"ERROR: GC overhead limit exceeded on {service}",
            f"WARN: Memory growing 50MB/min on {service}",
        ],
        "high_latency_spike": [
            f"WARN: p99 latency 6500ms exceeds SLA 500ms on {service}",
            f"ERROR: Request timeout after 30s on {service}",
            f"WARN: Circuit breaker open on {service}",
        ],
    }
    # Return generic error logs
    logs = [
        f"ERROR: Anomaly detected on {service} at {datetime.now().strftime('%H:%M:%S')}",
        f"WARN: Error rate elevated on {service}",
        f"INFO: Health check failing on {service}",
    ]
    return f"Recent logs for {service} (last {minutes} mins):\n" + "\n".join(logs)


@tool
def check_recent_deployments(service: str) -> str:
    """Check recent deployments for a service in the last hour.
    Use this to correlate incidents with deployment timing."""
    # Simulated deployment history - replace with real GitHub/ArgoCD API later
    return f"""Recent deployments for {service}:
- v2.3.1 deployed 4 minutes ago by engineer@company.com
  Changed: connection pool config, timeout settings
- v2.3.0 deployed 2 days ago
  Changed: database query optimization"""


@tool
def get_service_dependencies(service: str) -> str:
    """Get the downstream dependencies of a service.
    Use this to understand blast radius and cascade failures."""
    deps = {
        "payment-service": ["postgres", "redis", "auth-service"],
        "order-service": ["payment-service", "inventory-service", "postgres"],
        "auth-service": ["redis", "postgres"],
        "api-gateway": ["auth-service", "order-service", "payment-service"],
    }
    service_deps = deps.get(service, ["postgres", "redis"])
    return f"{service} depends on: {', '.join(service_deps)}"


# ── INVESTIGATOR AGENT ────────────────────────────────────────────────────────

def investigator_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("🔎 INVESTIGATOR AGENT")
    print("="*50)
    print(f"Investigating: {state['incident_type']} on {state['service']}")

    llm = ChatOllama(model="phi3:mini", temperature=0)
    tools = [query_prometheus, query_recent_logs, check_recent_deployments, get_service_dependencies]
    llm_with_tools = llm.bind_tools(tools)

    system_prompt = """You are an expert SRE investigator. Your job is to gather evidence about an incident.

You have these tools:
- query_prometheus: get live metrics (error_rate, db_connections, latency_p99, memory_usage, cpu_usage)
- query_recent_logs: get recent error logs
- check_recent_deployments: check if a recent deployment caused the incident
- get_service_dependencies: understand which services are affected

Steps:
1. Query the most relevant metric for this incident type
2. Check recent logs
3. Check if a recent deployment correlates with the incident
4. Conclude with root cause

After investigation respond with:
ROOT CAUSE: <one sentence>
EVIDENCE: <what you found>"""

    user_message = f"""Investigate this incident:
Incident Type: {state['incident_type']}
Service: {state['service']}
Severity: {state['severity']}
Initial logs: {state.get('raw_logs', [])[:2]}
Initial metrics: {state.get('raw_metrics', {})}

Use your tools to investigate. Start with the most relevant metric."""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message)
    ]

    evidence = list(state.get('evidence_chain', []))
    root_cause = "Under investigation"

    # ReAct loop - agent reasons and calls tools
    max_iterations = 4
    for i in range(max_iterations):
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        # Check if agent wants to call tools
        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tool_call in response.tool_calls:
                tool_name = tool_call['name']
                tool_args = tool_call['args']

                print(f"\n   🔧 Tool call: {tool_name}({tool_args})")

                # Execute the tool
                tool_result = None
                for t in tools:
                    if t.name == tool_name:
                        try:
                            tool_result = t.invoke(tool_args)
                        except Exception as e:
                            tool_result = f"Tool error: {e}"
                        break

                if tool_result:
                    print(f"   📊 Result: {str(tool_result)[:100]}...")
                    evidence.append(f"[INVESTIGATOR] {tool_name}: {str(tool_result)[:150]}")

                    # Add tool result to messages
                    from langchain_core.messages import ToolMessage
                    messages.append(ToolMessage(
                        content=str(tool_result),
                        tool_call_id=tool_call.get('id', f'tool_{i}')
                    ))
        else:
            # Agent finished investigating
            content = response.content
            if "ROOT CAUSE:" in content:
                lines = content.split('\n')
                for line in lines:
                    if "ROOT CAUSE:" in line:
                        root_cause = line.replace("ROOT CAUSE:", "").strip()
                        break
            else:
                root_cause = content[:200] if content else "Root cause under investigation"
            break

    print(f"\n✅ Investigation Complete:")
    print(f"   Root Cause: {root_cause}")
    print(f"   Evidence points: {len(evidence)}")

    return {
        **state,
        "evidence_chain": evidence,
        "root_cause": root_cause,
    }
