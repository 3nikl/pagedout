"""
PagedOut - Complete LangGraph Graph
Connects all 5 agents with proper routing.
This is the brain of PagedOut.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from langgraph.graph import StateGraph, END
from state import PagedOutState
from triage_agent import triage_agent
from investigator_agent import investigator_agent
from runbook_agent import runbook_rag_agent
from remediation_agent import remediation_agent
from postmortem_escalate_agents import postmortem_agent, escalate_agent


# ── ROUTING FUNCTIONS ─────────────────────────────────────────────────────────

def route_after_triage(state: dict) -> str:
    """Route based on confidence and incident type."""
    confidence = state.get('confidence', 0)
    next_agent = state.get('next_agent', 'escalate')

    # Low confidence always escalates
    if confidence < 0.5:
        print(f"\n⚠️  Low confidence ({confidence:.0%}) — escalating to human")
        return "escalate"

    print(f"\n➡️  Routing to: {next_agent}")
    return next_agent


# ── BUILD THE GRAPH ───────────────────────────────────────────────────────────

def build_graph():
    graph = StateGraph(PagedOutState)

    # Add all 5 agent nodes
    graph.add_node("triage", triage_agent)
    graph.add_node("investigator", investigator_agent)
    graph.add_node("runbook", runbook_rag_agent)
    graph.add_node("remediate", remediation_agent)
    graph.add_node("postmortem", postmortem_agent)
    graph.add_node("escalate", escalate_agent)

    # Entry point
    graph.set_entry_point("triage")

    # Conditional routing after triage
    graph.add_conditional_edges(
        "triage",
        route_after_triage,
        {
            "investigator": "investigator",
            "runbook": "runbook",
            "escalate": "escalate",
        }
    )

    # Linear flow after investigation
    graph.add_edge("investigator", "runbook")
    graph.add_edge("runbook", "remediate")
    graph.add_edge("remediate", "postmortem")
    graph.add_edge("postmortem", END)
    graph.add_edge("escalate", END)

    return graph.compile()


# Export compiled app
app = build_graph()
