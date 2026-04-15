"""
PagedOut - Complete State Definition
Shared state that flows through all 5 agents.
Every agent reads from and writes to this state.
"""

from typing import TypedDict, Annotated, Literal
from langgraph.graph.message import add_messages


class PagedOutState(TypedDict):
    # ── INPUT (set when incident arrives) ────────────────────────────────────
    event_id: str
    timestamp: str
    service: str
    severity: str
    raw_logs: list
    raw_metrics: dict
    alert_title: str

    # ── TRIAGE AGENT output ───────────────────────────────────────────────────
    incident_type: str
    confidence: float
    triage_summary: str
    next_agent: str

    # ── INVESTIGATOR AGENT output ─────────────────────────────────────────────
    evidence_chain: list
    root_cause: str

    # ── RUNBOOK RAG AGENT output ──────────────────────────────────────────────
    matched_runbook: str
    remediation_steps: list

    # ── REMEDIATION AGENT output ──────────────────────────────────────────────
    actions_taken: list
    actions_pending: list

    # ── POSTMORTEM AGENT output ───────────────────────────────────────────────
    postmortem: str

    # ── SHARED across all agents ──────────────────────────────────────────────
    messages: Annotated[list, add_messages]
