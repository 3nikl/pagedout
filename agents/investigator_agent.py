"""
Investigator agent: gathers real evidence, then asks the LLM to name a cause.

Previously this file queried `random.uniform(0.3, 0.9)` and returned a
hardcoded string asserting that a deploy had happened four minutes ago. The
LLM then "reasoned" over that. Every root cause it produced was fiction.

Now every probe hits live infrastructure — Prometheus, the services' own
/health endpoints, and the declared dependency topology. See agents/tools.py.

Design note on why tools are called directly in Python rather than through
LLM tool calling: phi3:mini is unreliable at emitting well-formed tool call
structures, and a failed tool call means no evidence at all. Selecting
probes deterministically by incident type and using the model only for the
final summary puts the model where it is strong (language) and keeps it out
of where it is weak (structure).
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama

from tools import gather_evidence

MODEL = "phi3:mini"


def investigator_agent(state: dict) -> dict:
    print("\n" + "=" * 50)
    print("🔎 INVESTIGATOR AGENT")
    print("=" * 50)

    service = state["service"]
    incident_type = state.get("incident_type", "unknown")
    print(f"Investigating: {incident_type} on {service}")

    findings = gather_evidence(service, incident_type)

    evidence_lines = list(state.get("evidence_chain", []))
    evidence_values: list[dict] = list(state.get("evidence_values", []))

    for ev in findings:
        mark = "📊" if ev.ok else "⚠️ "
        print(f"   {mark} {ev.summary}")
        evidence_lines.append(str(ev))
        evidence_values.append(ev.values)

    # A dependency probe that found unhealthy downstream services is the
    # strongest single signal available: it reclassifies the incident from
    # "this service is broken" to "this service is a victim". Surfacing it
    # explicitly means the planner does not have to infer it from prose.
    cascade_origin = None
    for ev in findings:
        unhealthy = ev.values.get("unhealthy") or []
        if unhealthy:
            cascade_origin = unhealthy[0]
            break

    usable = [e for e in findings if e.ok]
    if not usable:
        root_cause = (
            f"Investigation inconclusive: no telemetry available for {service}."
        )
        print(f"\n   ⚠️  {root_cause}")
    else:
        print(f"\n   🧠 Summarising root cause with {MODEL}...")
        llm = ChatOllama(model=MODEL, temperature=0)
        prompt = f"""You are an SRE. Based ONLY on this evidence, state the root cause.

Incident: {incident_type} on {service}

Evidence:
{chr(10).join('- ' + e.summary for e in usable)}

Answer in ONE sentence beginning "Root cause is". If the evidence shows a
downstream dependency is unhealthy, say that the dependency is the origin."""
        root_cause = llm.invoke([HumanMessage(content=prompt)]).content.strip()[:300]

    print(f"\n✅ Investigation complete")
    print(f"   Root cause: {root_cause[:140]}")
    if cascade_origin:
        print(f"   Cascade origin identified: {cascade_origin}")

    return {
        **state,
        "evidence_chain": evidence_lines,
        "evidence_values": evidence_values,
        "root_cause": root_cause,
        "cascade_origin": cascade_origin or "",
    }
