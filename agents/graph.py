"""
The PagedOut agent graph.

    triage ──confidence < 0.5──────────────────► escalate ──► END
       │
       ├──0.5 to 0.7──► runbook ─┐
       │                          │
       └──> 0.7───────► investigator
                                  │
                                  ▼
                              runbook  (hybrid retrieval)
                                  │
                                  ▼
                              planner  (schema-validated JSON plan)
                                  │
                                  ▼
                             remediate
                                  │
                                  ▼
                             postmortem ──► END

Two things changed in Phase 3:

  1. A planner node now sits between retrieval and remediation. Previously
     the "plan" was the retrieved runbook's steps list, read verbatim.
  2. The graph is compiled WITH A CHECKPOINTER. Every super-step writes
     state to PostgreSQL, so a crash mid-incident resumes from the last
     completed node instead of starting over. Without it, a process death
     during a 30-second LLM call loses the entire investigation.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from langgraph.graph import END, StateGraph

from investigator_agent import investigator_agent
from planner_agent import planner_agent
from postmortem_escalate_agents import escalate_agent, postmortem_agent
from remediation_agent import remediation_agent
from runbook_agent import runbook_rag_agent
from state import PagedOutState
from triage_agent import triage_agent

POSTGRES_URI = os.getenv(
    "PAGEDOUT_POSTGRES_URI",
    "postgresql://pagedout:pagedout123@localhost:5432/pagedout?sslmode=disable",
)

# Below this, we do not trust the classification enough to act on it.
CONFIDENCE_ESCALATE = 0.5
# Between the two, we skip investigation and go straight to a runbook: the
# type is probably right but not certain enough to justify a full probe run.
CONFIDENCE_INVESTIGATE = 0.7


def route_after_triage(state: dict) -> str:
    """Conditional edge. This is why the pipeline is a graph and not a chain."""
    confidence = state.get("confidence", 0.0)

    if confidence < CONFIDENCE_ESCALATE:
        print(f"\n⚠️  Low confidence ({confidence:.0%}) — escalating to a human")
        return "escalate"

    destination = "investigator" if confidence >= CONFIDENCE_INVESTIGATE else "runbook"
    print(f"\n➡️  Confidence {confidence:.0%} — routing to {destination}")
    return destination


def build_graph():
    """Assemble the node/edge topology. Compilation happens separately."""
    graph = StateGraph(PagedOutState)

    graph.add_node("triage", triage_agent)
    graph.add_node("investigator", investigator_agent)
    graph.add_node("runbook", runbook_rag_agent)
    graph.add_node("planner", planner_agent)
    graph.add_node("remediate", remediation_agent)
    graph.add_node("postmortem", postmortem_agent)
    graph.add_node("escalate", escalate_agent)

    graph.set_entry_point("triage")
    graph.add_conditional_edges(
        "triage",
        route_after_triage,
        {
            "investigator": "investigator",
            "runbook": "runbook",
            "escalate": "escalate",
        },
    )

    graph.add_edge("investigator", "runbook")
    graph.add_edge("runbook", "planner")
    graph.add_edge("planner", "remediate")
    graph.add_edge("remediate", "postmortem")
    graph.add_edge("postmortem", END)
    graph.add_edge("escalate", END)

    return graph


@contextmanager
def checkpointed_app(uri: str = POSTGRES_URI):
    """Yield a graph compiled with a PostgreSQL checkpointer.

    A context manager because PostgresSaver owns a connection pool that must
    be closed; letting it leak means the pipeline holds Postgres connections
    open for the life of the process.

    `.setup()` creates the checkpoint tables if absent. Safe to call every
    time — it is idempotent DDL.
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(uri) as saver:
        saver.setup()
        yield build_graph().compile(checkpointer=saver)


def memory_app():
    """Compile with an in-memory checkpointer.

    Used by tests and by the load-free demo path, so neither needs Postgres
    running. Same graph, different durability.
    """
    from langgraph.checkpoint.memory import MemorySaver

    return build_graph().compile(checkpointer=MemorySaver())


# Uncheckpointed compile, kept for callers that only want a single shot.
app = build_graph().compile()
