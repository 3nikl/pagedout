"""
PagedOut - Triage Agent
Classifies the incident type and routes to the right next agent.
Uses phi3:mini via Ollama locally.
"""

import json
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage


def triage_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("🔍 TRIAGE AGENT")
    print("="*50)
    print(f"Service: {state['service']}")
    print(f"Severity: {state['severity']}")
    print(f"Alert: {state['alert_title']}")

    llm = ChatOllama(model="phi3:mini", temperature=0)

    system_prompt = """You are an expert SRE triage agent. Analyze the incident and respond in JSON only.

Known incident types:
- database_connection_exhaustion
- memory_leak
- high_latency_spike
- pod_crash_loop
- disk_space_critical
- network_partition
- cpu_throttling
- deployment_failure
- cascade_failure

Routing rules:
- confidence > 0.7 -> next_agent: "investigator"
- confidence 0.5-0.7 -> next_agent: "runbook"
- confidence < 0.5 -> next_agent: "escalate"

Respond with ONLY this JSON, nothing else:
{
  "incident_type": "<type from list above>",
  "confidence": <0.0 to 1.0>,
  "triage_summary": "<one sentence summary>",
  "next_agent": "<investigator|runbook|escalate>"
}"""

    user_message = f"""Incident Details:
Service: {state['service']}
Severity: {state['severity']}
Alert: {state['alert_title']}

Recent Logs:
{chr(10).join(state.get('raw_logs', [])[:3])}

Metrics:
{json.dumps(state.get('raw_metrics', {}), indent=2)}"""

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message)
    ])

    # Parse JSON response
    try:
        content = response.content.strip()
        start = content.find('{')
        end = content.rfind('}') + 1
        if start != -1 and end != 0:
            json_str = content[start:end]
            result = json.loads(json_str)
        else:
            raise ValueError("No JSON found")
    except Exception as e:
        print(f"Parse error: {e}. Using fallback.")
        result = {
            "incident_type": "unknown",
            "confidence": 0.3,
            "triage_summary": "Could not classify incident. Escalating to human.",
            "next_agent": "escalate"
        }

    print(f"\n✅ Triage Result:")
    print(f"   Type: {result['incident_type']}")
    print(f"   Confidence: {result['confidence']:.0%}")
    print(f"   Summary: {result['triage_summary']}")
    print(f"   Routing to: {result['next_agent']}")

    return {
        **state,
        "incident_type": result.get("incident_type", "unknown"),
        "confidence": result.get("confidence", 0.0),
        "triage_summary": result.get("triage_summary", ""),
        "next_agent": result.get("next_agent", "escalate"),
        "evidence_chain": [f"[TRIAGE] Classified as {result.get('incident_type')} with {result.get('confidence', 0):.0%} confidence"],
        "messages": [HumanMessage(content=user_message)],
    }
