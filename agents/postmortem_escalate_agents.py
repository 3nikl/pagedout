"""
PagedOut - Postmortem Agent
Generates structured incident report from full state.

PagedOut - Escalate Agent  
Handles incidents that need human intervention.
"""

from datetime import datetime
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage


def postmortem_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("📝 POSTMORTEM AGENT")
    print("="*50)
    print("Generating incident report...")

    llm = ChatOllama(model="phi3:mini", temperature=0)

    evidence_text = "\n".join(state.get('evidence_chain', []))
    actions_taken = "\n".join([f"- {a}" for a in state.get('actions_taken', [])])
    actions_pending = "\n".join([f"- {a}" for a in state.get('actions_pending', [])])

    prompt = f"""Generate a concise incident postmortem report.

Incident Data:
- Service: {state['service']}
- Severity: {state['severity']}
- Type: {state['incident_type']}
- Root Cause: {state.get('root_cause', 'Under investigation')}

Evidence Chain:
{evidence_text}

Actions Taken:
{actions_taken if actions_taken else "None"}

Actions Pending Human Approval:
{actions_pending if actions_pending else "None"}

Write a structured postmortem with sections:
SUMMARY, ROOT CAUSE, IMPACT, ACTIONS TAKEN, PREVENTION"""

    response = llm.invoke([HumanMessage(content=prompt)])
    postmortem = response.content

    print(f"\n✅ Postmortem Generated ({len(postmortem)} chars)")
    print("\n" + "-"*50)
    print(postmortem[:500] + "..." if len(postmortem) > 500 else postmortem)
    print("-"*50)

    return {
        **state,
        "postmortem": postmortem,
    }


def escalate_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("🚨 ESCALATE AGENT")
    print("="*50)
    print(f"Escalating to human engineer...")
    print(f"Service: {state['service']}")
    print(f"Severity: {state['severity']}")
    print(f"Reason: Low confidence classification or unknown incident type")
    print(f"\n📱 [SIMULATED] Slack notification sent to #incidents channel")
    print(f"📟 [SIMULATED] PagerDuty alert triggered for on-call engineer")

    return {
        **state,
        "postmortem": f"Incident escalated to human engineer at {datetime.now().isoformat()}. Manual investigation required.",
        "evidence_chain": state.get('evidence_chain', []) + [
            f"[ESCALATE] Incident escalated at {datetime.now().isoformat()}"
        ]
    }
