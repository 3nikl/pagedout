"""
Remediation agent: splits a validated plan into auto-executable and
approval-gated actions.

Phase 3 scope. The plan arriving here has already been schema-validated by
the planner, so risk is read from the action catalog rather than guessed
from a string prefix — the previous implementation classified risk with
`step.startswith("SAFE:")`, which meant a runbook author's typo became a
security boundary.

EXECUTION IS STILL SIMULATED. The victim services expose four real admin
endpoints that these actions map onto, but actually calling them requires
the Phase 4 machinery: idempotency keys, a dry-run pass, and rollback on
regression. Wiring execution without those would be the dangerous version
of this feature, so it is deliberately left for Phase 4.
"""

from __future__ import annotations

from planner_agent import ACTION_CATALOG, ActionName

# Where each action would be sent. Declared now so Phase 4 has an explicit
# mapping to implement against rather than inventing one.
ACTION_ENDPOINTS: dict[ActionName, tuple[str, str]] = {
    ActionName.DRAIN_POOL: ("POST", "/admin/pool/drain"),
    ActionName.RESIZE_POOL: ("POST", "/admin/pool/resize"),
    ActionName.CLEAR_CACHE: ("POST", "/admin/cache/clear"),
    ActionName.ROLLBACK_VERSION: ("POST", "/admin/version/rollback"),
    ActionName.ESCALATE: ("", ""),
}


def remediation_agent(state: dict) -> dict:
    print("\n" + "=" * 50)
    print("🔧 REMEDIATION AGENT")
    print("=" * 50)

    plan = state.get("plan") or {}
    actions = plan.get("actions", [])

    if not actions:
        print("   no actions in plan — nothing to do")
        return {
            **state,
            "actions_taken": [],
            "actions_pending": [],
            "evidence_chain": state.get("evidence_chain", []) + [
                "[REMEDIATION] plan contained no actions"
            ],
        }

    print(f"Processing {len(actions)} planned action(s)")

    taken: list[dict] = []
    pending: list[dict] = []

    for action in actions:
        name = action["action"]
        target = action["target_service"]
        risk = action["risk"]

        try:
            method, path = ACTION_ENDPOINTS[ActionName(name)]
            endpoint = f"{method} {target}{path}" if path else "(human handoff)"
        except (ValueError, KeyError):
            # Unknown action name. The schema should make this impossible;
            # treating it as unsafe rather than assuming is the right default.
            print(f"   ⛔ unknown action {name!r} — routing to approval")
            pending.append({**action, "reason": "unknown action"})
            continue

        record = {**action, "endpoint": endpoint}

        if name == ActionName.ESCALATE.value:
            pending.append({**record, "reason": "planner chose escalation"})
            print(f"   🚨 ESCALATE -> {target}: {action['rationale'][:70]}")
        elif risk == "low":
            # Phase 4 replaces this print with a real, idempotent call.
            print(f"   ⚡ [SIMULATED] {name} -> {target}  ({endpoint})")
            taken.append(record)
        else:
            pending.append({**record, "reason": "high-risk action requires approval"})
            print(f"   ⏳ AWAITING APPROVAL {name} -> {target}  (risk={risk})")

    print(f"\n✅ Remediation triage complete")
    print(f"   auto-executable : {len(taken)}")
    print(f"   needs approval  : {len(pending)}")

    return {
        **state,
        "actions_taken": taken,
        "actions_pending": pending,
        "evidence_chain": state.get("evidence_chain", []) + [
            f"[REMEDIATION] {len(taken)} auto-executable (simulated), "
            f"{len(pending)} awaiting approval"
        ],
    }
