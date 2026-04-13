"""
PagedOut - Triage Agent
First agent in the pipeline. Classifies incident type, severity,
and routes to the appropriate specialist agent.
"""

import json
from typing import TypedDict, Annotated, Literal
from datetime import datetime

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_openai import AzureChatOpenAI
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages


# ── State ─────────────────────────────────────────────────────────────────────

class IncidentState(TypedDict):
    # Input
    event_id: str
    timestamp: str
    service: str
    raw_logs: list[str]
    raw_metrics: dict
    alert_title: str
    severity: str

    # Triage output
    incident_type: str
    confidence: float
    triage_summary: str
    next_agent: Literal["investigator", "runbook", "escalate"]

    # Shared across agents
    messages: Annotated[list, add_messages]
    evidence_chain: list[str]
    remediation_steps: list[str]
    postmortem: str


# ── LLM ──────────────────────────────────────────────────────────────────────

def get_llm():
    """
    Initialize Azure OpenAI LLM.
    Replace with your Azure OpenAI endpoint and deployment.
    """
    return AzureChatOpenAI(
        azure_deployment="gpt-4o",
        api_version="2024-02-01",
        temperature=0,
        max_tokens=1000,
    )


# ── Triage Prompt ─────────────────────────────────────────────────────────────

TRIAGE_SYSTEM_PROMPT = """
You are an expert SRE triage agent. Your job is to analyze incoming incident signals
and classify them accurately so the right specialist agent can investigate.

Given log messages, metrics, and alert context, you will:
1. Identify the incident type from the known taxonomy
2. Assess severity (P1/P2/P3)
3. Provide a brief triage summary
4. Decide which agent should handle next

Known incident types:
- database_connection_exhaustion
- memory_leak
- high_latency_spike
- pod_crash_loop
- disk_space_critical
- network_partition
- cpu_throttling

Routing rules:
- P1 incidents -> investigator (immediate deep investigation)
- P2 with known runbook -> runbook (retrieve and apply standard fix)
- Unknown or ambiguous -> escalate (human required)

Respond in JSON only:
{
  "incident_type": "<type>",
  "confidence": <0.0-1.0>,
  "triage_summary": "<1-2 sentence summary>",
  "next_agent": "<investigator|runbook|escalate>"
}
"""


# ── Nodes ─────────────────────────────────────────────────────────────────────

def triage_node(state: IncidentState) -> IncidentState:
    """
    Core triage logic. Analyzes incident signals and classifies the incident.
    """
    llm = get_llm()

    # Build context for LLM
    context = f"""
Incident Alert: {state.get('alert_title', 'Unknown')}
Service: {state['service']}
Severity: {state['severity']}
Timestamp: {state['timestamp']}

Recent Log Messages:
{chr(10).join(state.get('raw_logs', [])[:5])}

Current Metrics:
{json.dumps(state.get('raw_metrics', {}), indent=2)}
"""

    messages = [
        SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ]

    response = llm.invoke(messages)

    # Parse JSON response
    try:
        result = json.loads(response.content)
    except json.JSONDecodeError:
        # Fallback if LLM doesn't return clean JSON
        result = {
            "incident_type": "unknown",
            "confidence": 0.0,
            "triage_summary": "Failed to parse triage response. Escalating to human.",
            "next_agent": "escalate",
        }

    # Update state
    return {
        **state,
        "incident_type": result.get("incident_type", "unknown"),
        "confidence": result.get("confidence", 0.0),
        "triage_summary": result.get("triage_summary", ""),
        "next_agent": result.get("next_agent", "escalate"),
        "messages": [
            HumanMessage(content=context),
            AIMessage(content=response.content),
        ],
        "evidence_chain": [
            f"[TRIAGE] {datetime.now().isoformat()} - Classified as {result.get('incident_type')} "
            f"with {result.get('confidence', 0)*100:.0f}% confidence"
        ],
    }


def routing_node(state: IncidentState) -> str:
    """
    Conditional routing based on triage result.
    Returns the name of the next node to execute.
    """
    next_agent = state.get("next_agent", "escalate")
    confidence = state.get("confidence", 0.0)

    # Low confidence always escalates
    if confidence < 0.5:
        return "escalate"

    return next_agent


def escalate_node(state: IncidentState) -> IncidentState:
    """
    Escalation handler. Posts to Slack and creates PagerDuty alert.
    TODO: Integrate with Slack API and PagerDuty.
    """
    print(f"[ESCALATE] Incident {state['event_id']} requires human attention.")
    print(f"  Service: {state['service']}")
    print(f"  Type: {state.get('incident_type', 'unknown')}")
    print(f"  Summary: {state.get('triage_summary', '')}")

    return {
        **state,
        "evidence_chain": state.get("evidence_chain", []) + [
            f"[ESCALATE] {datetime.now().isoformat()} - Escalated to on-call engineer"
        ],
    }


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_triage_graph() -> StateGraph:
    """
    Builds the triage agent graph.
    """
    graph = StateGraph(IncidentState)

    # Add nodes
    graph.add_node("triage", triage_node)
    graph.add_node("escalate", escalate_node)

    # Entry point
    graph.set_entry_point("triage")

    # Conditional routing after triage
    graph.add_conditional_edges(
        "triage",
        routing_node,
        {
            "investigator": END,   # TODO: wire to investigator agent
            "runbook": END,        # TODO: wire to runbook RAG agent
            "escalate": "escalate",
        },
    )

    graph.add_edge("escalate", END)

    return graph.compile()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test with a sample incident
    sample_incident = IncidentState(
        event_id="test-001",
        timestamp=datetime.now().isoformat(),
        service="payment-service",
        raw_logs=[
            "FATAL: Connection pool exhausted. Active connections: 99/100",
            "ERROR: Unable to acquire database connection after 30s timeout",
            "WARN: Connection wait time exceeding threshold: 4500ms",
        ],
        raw_metrics={
            "db_connections": 99,
            "error_rate": 0.78,
            "latency_p99": 6500,
        },
        alert_title="[P1] database_connection_exhaustion - payment-service",
        severity="P1",
        incident_type="",
        confidence=0.0,
        triage_summary="",
        next_agent="escalate",
        messages=[],
        evidence_chain=[],
        remediation_steps=[],
        postmortem="",
    )

    graph = build_triage_graph()
    result = graph.invoke(sample_incident)

    print("\n--- Triage Result ---")
    print(f"Incident Type: {result['incident_type']}")
    print(f"Confidence: {result['confidence']:.0%}")
    print(f"Summary: {result['triage_summary']}")
    print(f"Next Agent: {result['next_agent']}")
    print(f"Evidence Chain: {result['evidence_chain']}")
