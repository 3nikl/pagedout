"""
Shared state for the PagedOut agent graph.

A TypedDict rather than a Pydantic model because LangGraph merges node
return values into this dict directly; a validating model would reject the
partial updates that nodes legitimately return.

Every node reads this and returns a (possibly partial) update. Fields are
grouped by which node produces them, so it is obvious where a value came
from when debugging a run.
"""

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class PagedOutState(TypedDict, total=False):
    # ── INPUT (set when the incident arrives) ─────────────────────────────────
    event_id: str
    timestamp: str
    service: str
    severity: str            # P1..P4
    raw_logs: list
    raw_metrics: dict
    alert_title: str

    # ── TRIAGE ────────────────────────────────────────────────────────────────
    incident_type: str
    confidence: float
    triage_summary: str
    next_agent: str

    # ── INVESTIGATOR ──────────────────────────────────────────────────────────
    evidence_chain: list          # human/LLM readable lines
    evidence_values: list         # machine readable dicts, for the planner
    root_cause: str
    cascade_origin: str           # downstream service that is the real origin

    # ── RUNBOOK RETRIEVAL ─────────────────────────────────────────────────────
    matched_runbook: str
    remediation_steps: list
    retrieved_sources: list
    retrieval_ms: float

    # ── PLANNER ───────────────────────────────────────────────────────────────
    # Schema-validated plan. See agents/planner_agent.RemediationPlan.
    plan: dict[str, Any]
    remediation_plan_valid: bool
    planner_attempts: list        # validation errors, kept for debugging

    # ── REMEDIATION ───────────────────────────────────────────────────────────
    actions_taken: list
    actions_pending: list

    # ── POSTMORTEM ────────────────────────────────────────────────────────────
    postmortem: str

    # ── SHARED ────────────────────────────────────────────────────────────────
    # add_messages is a reducer: it APPENDS rather than replacing, so each
    # node contributes to one conversation instead of clobbering it.
    messages: Annotated[list, add_messages]
