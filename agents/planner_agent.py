"""
Planner agent: turns evidence into a validated, structured remediation plan.

This node did not previously exist. The old pipeline read the `steps` list
verbatim out of whichever runbook Qdrant returned and called that a plan —
a dictionary lookup wearing a planner costume. It could not adapt a
procedure to the evidence, could not choose between runbook steps, and
could not decline to act.

What this does instead:

  1. Gives the model a CLOSED VOCABULARY of actions (ACTION_CATALOG).
     An open-ended "what should we do?" produces prose that no executor can
     run. A closed vocabulary makes the output a program.
  2. Validates the model's JSON against a Pydantic schema. Anything that is
     not a well-formed plan over known actions with well-typed parameters is
     rejected, not executed.
  3. Retries once with the validation error fed back, which small models are
     usually able to act on.
  4. Falls back to a deterministic plan derived from the retrieved runbook if
     the model cannot produce a valid plan twice. Failing closed to a known
     procedure beats failing open to nothing.

The schema is the contract between a probabilistic component and a system
that takes real actions. That boundary is the whole point.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

MODEL = "phi3:mini"


# ── Action vocabulary ─────────────────────────────────────────────────────────


class ActionName(str, Enum):
    """Every action the planner is allowed to emit.

    Deliberately small and deliberately matched 1:1 to endpoints the victim
    services actually expose. An action the executor cannot perform is worse
    than no action: it looks like remediation and does nothing.

    Inherits from str as well as Enum so the value serialises straight to
    JSON without a custom encoder.
    """

    DRAIN_POOL = "drain_pool"
    RESIZE_POOL = "resize_pool"
    CLEAR_CACHE = "clear_cache"
    ROLLBACK_VERSION = "rollback_version"
    ESCALATE = "escalate"


# risk is declared here, by us, not by the model. Letting the LLM decide
# whether its own action is dangerous is exactly the wrong place to put that
# judgement.
ACTION_CATALOG: dict[ActionName, dict[str, Any]] = {
    ActionName.DRAIN_POOL: {
        "description": "Release idle database connections and clear pool saturation.",
        "parameters": {},
        "risk": "low",
        "fixes": ["database_connection_exhaustion"],
    },
    ActionName.RESIZE_POOL: {
        "description": "Change connection pool capacity. Requires integer 'size' 1-500.",
        "parameters": {"size": int},
        "risk": "high",
        "fixes": ["database_connection_exhaustion"],
    },
    ActionName.CLEAR_CACHE: {
        "description": "Drop the local cache and release heap held by it.",
        "parameters": {},
        "risk": "low",
        "fixes": ["memory_leak"],
    },
    ActionName.ROLLBACK_VERSION: {
        "description": "Roll the service back to its previous deployed version.",
        "parameters": {},
        "risk": "high",
        "fixes": ["deployment_failure", "pod_crash_loop"],
    },
    ActionName.ESCALATE: {
        "description": "Hand off to a human engineer. Use when no action is safe.",
        "parameters": {},
        "risk": "low",
        "fixes": [],
    },
}


# ── Plan schema ───────────────────────────────────────────────────────────────


class PlannedAction(BaseModel):
    action: ActionName
    target_service: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=400)

    @field_validator("target_service")
    @classmethod
    def _clean_service(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check_parameters(self) -> "PlannedAction":
        """Reject unknown or badly typed parameters.

        Without this, the model can emit resize_pool with size="lots" or
        size=999999, and a downstream executor would either crash or do
        something catastrophic. Validating here means an invalid plan never
        becomes an invalid execution.
        """
        spec = ACTION_CATALOG[self.action]["parameters"]

        unexpected = set(self.parameters) - set(spec)
        if unexpected:
            raise ValueError(
                f"{self.action.value} takes {sorted(spec) or 'no parameters'}, "
                f"got unexpected {sorted(unexpected)}"
            )

        for name, expected_type in spec.items():
            if name not in self.parameters:
                raise ValueError(f"{self.action.value} requires parameter '{name}'")
            value = self.parameters[name]
            if expected_type is int:
                # bool is a subclass of int in Python; True would pass a naive
                # isinstance check and then mean 1 connection.
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"'{name}' must be an integer, got {value!r}")
                if not 1 <= value <= 500:
                    raise ValueError(f"'{name}' must be within 1..500, got {value}")
        return self

    @property
    def risk(self) -> str:
        """Risk comes from the catalog, never from the model."""
        return ACTION_CATALOG[self.action]["risk"]


class RemediationPlan(BaseModel):
    root_cause_summary: str = Field(min_length=1, max_length=600)
    actions: list[PlannedAction] = Field(min_length=1, max_length=6)

    @property
    def requires_approval(self) -> bool:
        return any(a.risk == "high" for a in self.actions)

    def to_state(self) -> dict:
        return {
            "root_cause_summary": self.root_cause_summary,
            "requires_approval": self.requires_approval,
            "actions": [
                {
                    "action": a.action.value,
                    "target_service": a.target_service,
                    "parameters": a.parameters,
                    "risk": a.risk,
                    "rationale": a.rationale,
                }
                for a in self.actions
            ],
        }


# ── Prompt construction ───────────────────────────────────────────────────────


def _catalog_text() -> str:
    lines = []
    for name, spec in ACTION_CATALOG.items():
        params = (
            ", ".join(f"{k}:{v.__name__}" for k, v in spec["parameters"].items())
            or "none"
        )
        lines.append(f'  "{name.value}" — {spec["description"]} parameters: {params}')
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are an SRE remediation planner. You output JSON only.

Allowed actions (you may use ONLY these):
{_catalog_text()}

Rules:
- Order actions least disruptive first.
- Only target services named in the evidence.
- If the evidence says the service is a cascade victim, target the UNHEALTHY
  DEPENDENCY, not the victim.
- If no listed action addresses the problem, emit a single "escalate" action.

Respond with ONLY this JSON and nothing else:
{{
  "root_cause_summary": "<one or two sentences>",
  "actions": [
    {{
      "action": "<action name>",
      "target_service": "<service>",
      "parameters": {{}},
      "rationale": "<why this action>"
    }}
  ]
}}"""


def _build_user_prompt(state: dict) -> str:
    evidence = "\n".join(f"- {e}" for e in state.get("evidence_chain", []))
    runbook_steps = "\n".join(f"- {s}" for s in state.get("remediation_steps", []))
    return f"""Incident:
  service: {state.get('service')}
  severity: {state.get('severity')}
  type: {state.get('incident_type')}
  alert: {state.get('alert_title')}

Investigation evidence:
{evidence or '(none)'}

Suggested runbook '{state.get('matched_runbook', 'none')}':
{runbook_steps or '(none)'}

Produce the remediation plan JSON."""


def _extract_json(text: str) -> dict:
    """Pull the outermost JSON object out of a model response.

    Small models wrap JSON in prose and markdown fences. Scanning for the
    first '{' and last '}' is more robust than json.loads on the raw string,
    and far more robust than regex.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model response")
    return json.loads(text[start : end + 1])


# ── Fallback ──────────────────────────────────────────────────────────────────


def _fallback_plan(state: dict) -> RemediationPlan:
    """Deterministic plan when the model cannot produce a valid one.

    Maps the incident type to the catalog action that declares it can fix it.
    Targets the unhealthy dependency when the evidence identified a cascade,
    since acting on the victim would do nothing.
    """
    incident_type = state.get("incident_type", "unknown")
    target = state.get("service", "unknown")

    for ev in state.get("evidence_values", []):
        unhealthy = (ev or {}).get("unhealthy") or []
        if unhealthy:
            target = unhealthy[0]
            break

    for name, spec in ACTION_CATALOG.items():
        if incident_type in spec["fixes"] and spec["risk"] == "low":
            return RemediationPlan(
                root_cause_summary=(
                    f"Fallback plan: model output failed validation. "
                    f"Applying the catalog's low-risk remedy for {incident_type}."
                ),
                actions=[
                    PlannedAction(
                        action=name,
                        target_service=target,
                        parameters={},
                        rationale=f"Catalog maps {incident_type} to {name.value}.",
                    )
                ],
            )

    return RemediationPlan(
        root_cause_summary=(
            f"No low-risk catalog action addresses {incident_type}; escalating."
        ),
        actions=[
            PlannedAction(
                action=ActionName.ESCALATE,
                target_service=target,
                parameters={},
                rationale="No safe automated remedy is available for this incident type.",
            )
        ],
    )


# ── Node ──────────────────────────────────────────────────────────────────────


def planner_agent(state: dict) -> dict:
    print("\n" + "=" * 50)
    print("🗺️  PLANNER AGENT")
    print("=" * 50)

    llm = ChatOllama(model=MODEL, temperature=0)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=_build_user_prompt(state)),
    ]

    plan: RemediationPlan | None = None
    attempts: list[str] = []

    for attempt in range(2):
        try:
            raw = llm.invoke(messages).content
            plan = RemediationPlan.model_validate(_extract_json(raw))
            print(f"   ✅ valid plan on attempt {attempt + 1}")
            break
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            detail = str(exc)[:300]
            attempts.append(detail)
            print(f"   ⚠️  attempt {attempt + 1} rejected: {detail.splitlines()[0]}")
            if attempt == 0:
                # Feed the error back. Small models correct structural
                # mistakes reliably when told exactly what was wrong.
                messages.append(HumanMessage(
                    content=(
                        f"That response was rejected by the schema validator:\n{detail}\n"
                        f"Return corrected JSON only."
                    )
                ))

    if plan is None:
        print("   ↩️  falling back to deterministic catalog plan")
        plan = _fallback_plan(state)

    payload = plan.to_state()
    print(f"\n   Root cause: {payload['root_cause_summary'][:120]}")
    print(f"   Actions ({len(payload['actions'])}), approval required: "
          f"{payload['requires_approval']}")
    for a in payload["actions"]:
        print(f"     [{a['risk']:>4}] {a['action']} -> {a['target_service']}  "
              f"{a['parameters'] or ''}")

    return {
        **state,
        "plan": payload,
        "remediation_plan_valid": not attempts or plan is not None,
        "planner_attempts": attempts,
        "evidence_chain": state.get("evidence_chain", []) + [
            f"[PLANNER] {len(payload['actions'])} action(s), "
            f"approval_required={payload['requires_approval']}"
        ],
    }
