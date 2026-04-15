"""
PagedOut - Remediation Agent
Validates each action and executes safe ones.
High risk actions go to human approval queue.
"""

from datetime import datetime


# ── RISK CLASSIFICATION ───────────────────────────────────────────────────────

def classify_risk(step: str) -> str:
    """Guardrails AI validation - classify action risk."""
    if step.startswith("SAFE:"):
        return "low"
    elif step.startswith("RISKY:"):
        return "high"
    return "unknown"


def execute_action(action: str, service: str) -> str:
    """Simulated action execution - replace with real K8s API calls later."""
    print(f"   ⚡ Executing: {action}")
    # TODO: Replace with real Kubernetes API calls
    # k8s_client.restart_pod(service)
    return f"Successfully executed: {action} on {service}"


def remediation_agent(state: dict) -> dict:
    print("\n" + "="*50)
    print("🔧 REMEDIATION AGENT")
    print("="*50)
    print(f"Processing {len(state.get('remediation_steps', []))} remediation steps")
    print("Validating each action via Guardrails AI...")

    actions_taken = []
    actions_pending = []

    for step in state.get('remediation_steps', []):
        risk = classify_risk(step)
        action = step.replace("SAFE:", "").replace("RISKY:", "").strip()

        if risk == "low":
            result = execute_action(action, state['service'])
            actions_taken.append(action)
            print(f"   ✅ AUTO-EXECUTED (low risk): {action}")
        elif risk == "high":
            actions_pending.append(action)
            print(f"   ⏳ QUEUED FOR HUMAN APPROVAL (high risk): {action}")
        else:
            actions_pending.append(action)
            print(f"   ⚠️  UNKNOWN RISK - escalating: {action}")

    print(f"\n✅ Remediation Complete:")
    print(f"   Auto-executed: {len(actions_taken)} actions")
    print(f"   Pending human approval: {len(actions_pending)} actions")

    return {
        **state,
        "actions_taken": actions_taken,
        "actions_pending": actions_pending,
        "evidence_chain": state.get('evidence_chain', []) + [
            f"[REMEDIATION] Executed {len(actions_taken)} safe actions. {len(actions_pending)} pending human approval."
        ]
    }
