"""Structured data models for the Autonomous Feature Orchestrator V1.

Only safe, structured state is modelled here. Secrets (tokens, credentials,
OAuth values, API keys) are never accepted into these models: input text is
redacted before persistence by feature_utils.redact_sensitive.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from jarvis_core.timezone_utils import jarvis_now


class FeatureStatus(str, Enum):
    # ── Phase 1 state machine (Telegram Feature Development) ─────────────────
    # AWAITING_MODE_SELECTION → RESOURCE_SCANNING → RESOURCE_REVIEW →
    # RESOURCE_REVIEW_COMPLETE (or CANCELLED). Resource-detail/action screens
    # are UI substates of RESOURCE_REVIEW and MUST NOT advance the phase.
    awaiting_mode_selection = "awaiting_mode_selection"
    resource_scanning = "resource_scanning"
    resource_review = "resource_review"
    resource_review_complete = "resource_review_complete"

    # ── Legacy / post-review pipeline states ────────────────────────────────
    received = "received"
    resource_review_pending = "resource_review_pending"
    analyzing = "analyzing"
    researching = "researching"
    chatgpt_review_pending = "chatgpt_review_pending"
    planning = "planning"
    approval_pending = "approval_pending"
    approved = "approved"
    cancelled = "cancelled"
    cline_ready = "cline_ready"
    implementation_in_progress = "implementation_in_progress"
    completed = "completed"
    failed = "failed"


class FeatureSource(str, Enum):
    web = "Web"
    telegram = "Telegram"


class RiskLevel(str, Enum):
    low = "Low"
    medium = "Medium"
    high = "High"


def _new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return jarvis_now().isoformat()


class FeatureEvent(BaseModel):
    at: str = Field(default_factory=_now)
    stage: str
    message: str


class FeatureRequest(BaseModel):
    """One autonomous feature-development request and its full workflow state."""

    feature_id: str = Field(default_factory=_new_id)
    original_request: str
    source: str = FeatureSource.web.value
    created_at: str = Field(default_factory=_now)
    interpreted_objective: str = ""
    status: str = FeatureStatus.received.value
    updated_at: str = Field(default_factory=_now)

    # Telegram routing (safe: only numeric ids, no chat content/secrets)
    telegram_user_id: Optional[int] = None
    telegram_chat_id: Optional[int] = None

    # LIVE TASKS integration
    task_id: Optional[str] = None

    # When the current workflow stage began (ISO timestamp). Set on every real
    # stage transition so the UI can show per-stage elapsed time. Safe field.
    current_stage_started_at: Optional[str] = None

    # ── Research (observed facts vs model recommendations) ──────────────
    observed_facts: list[str] = Field(default_factory=list)
    model_recommendations: list[str] = Field(default_factory=list)
    research_incomplete: bool = False
    research_error: Optional[str] = None
    affected_modules: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    security_implications: list[str] = Field(default_factory=list)
    failure_risks: list[str] = Field(default_factory=list)
    testing_requirements: list[str] = Field(default_factory=list)
    ui_changes_needed: Optional[bool] = None
    telegram_integration_needed: Optional[bool] = None

    # ── Verified evidence model (Phase 1) ─────────────────────────────────
    # Paths are REAL and exist in this repository (relative to BASE_DIR).
    # A path may only appear here after an actual filesystem check.
    verified_existing_files: list[str] = Field(default_factory=list)
    # Proposed (not yet existing) new paths — clearly separated from verified.
    proposed_new_files: list[str] = Field(default_factory=list)
    # Statements that could not be confirmed by repository inspection.
    unverified_assumptions: list[str] = Field(default_factory=list)

    # True when a RESEARCH-REQUIRED revision re-runs repository analysis and
    # should continue straight to the rebuilt plan (no second ChatGPT gate).
    rerun_research: bool = False

    # ── ChatGPT consultation (user-provider, not OpenAI API) ─────────────
    # chatgpt_prompt holds the concise REQUEST Jarvis asked the user to paste
    # into ChatGPT; its purpose is to obtain a Cline-ready implementation
    # prompt, not a general review.
    chatgpt_prompt: str = ""
    chatgpt_review: str = ""          # EXACT user-submitted ChatGPT output (never rewritten)
    chatgpt_reviewed: bool = False
    chatgpt_skipped: bool = False
    chatgpt_analysis: Optional[dict] = None

    # Provenance / proof chain for the submitted ChatGPT output.
    # {source, feature_id, submitted_at, text_length, sha256}
    chatgpt_provenance: Optional[dict] = None

    # Candidate Cline implementation prompt (from ChatGPT, user edit, or local).
    cline_candidate_prompt: str = ""
    prompt_source: Optional[str] = None      # ChatGPT | User Edited | Jarvis Local
    prompt_validation: Optional[dict] = None  # {valid, issues, missing_paths, unsafe}
    prompt_versions: list[dict] = Field(default_factory=list)  # [{version, source, at}]
    prompt_version: int = 0                   # P-number (current)
    chatgpt_request_revision: int = 0         # G-number
    research_revision: int = 0                # R-number

    # NEED_MORE_RESEARCH loop
    needs_more_research: bool = False
    missing_evidence: list[str] = Field(default_factory=list)

    # Proof/evidence snapshots (safe metadata only — never secrets/screenshots
    # of sensitive surfaces). See jarvis_core.feature_proof.
    proof_history: list[dict] = Field(default_factory=list)

        # ── Resource review gate (Telegram Feature Development) ───────────────
    # Before ANY research begins, the user reviews the REAL applications and
    # Chrome tabs open on the laptop and explicitly presses Continue Feature.
    # Only safe structured metadata is stored - never browser sessions/cookies.
    resource_review_completed: bool = False
    resource_review_started_at: Optional[str] = None
    # Part 9 bridge idempotency: set True the first time ▶️ Continue enqueues
    # the RESEARCH stage. Persisted (source of truth) so a double Continue tap,
    # a duplicate Telegram callback, a retry, or a restart after a successful
    # Continue can NEVER enqueue research more than once for this feature.
    research_enqueued: bool = False
    # Persisted snapshot of the resource scan (apps + Chrome tabs) captured
    # during resource review. Stored on the feature so Apps/Tabs remains
    # available long after the original scan thread has exited.
    resource_snapshot: Optional[dict] = None

    # ── Final plan / approval / Cline handoff ────────────────────────────
    plan: Optional[dict] = None
    risk: str = RiskLevel.medium.value
    # User revisions are stored SEPARATELY and never mutate original identity
    # fields (original_request / interpreted_objective).
    change_requests: list[dict] = Field(default_factory=list)  # [{at, text, kind}]
    approval_decision: Optional[str] = None
    approved_at: Optional[str] = None
    high_risk_confirmed: bool = False
    cline_task: Optional[dict] = None
    cline_handed_off: bool = False
    # Part 11 execution bridge (Approval → Implementation → Completed/Failed).
    # implementation_started is the persisted idempotency guard: it is set True
    # the first time execution starts, so a double approval, a duplicate
    # Telegram callback, a retry, or a restart can NEVER start a second Cline
    # implementation job for this feature.
    implementation_started: bool = False
    implementation_started_at: Optional[str] = None
    # Verbatim result reported back from the Cline implementation run (safe
    # structured metadata only). {success, summary, error, verified, at}.
    implementation_result: Optional[dict] = None

    history: list[FeatureEvent] = Field(default_factory=list)

    def record(self, stage: str, message: str) -> None:
        self.history.append(FeatureEvent(stage=stage, message=message))
        self.history = self.history[-40:]
        self.updated_at = _now()

    @property
    def research_label(self) -> str:
        return f"R{max(1, self.research_revision)}"

    @property
    def request_label(self) -> str:
        return f"G{max(1, self.chatgpt_request_revision)}"

    @property
    def prompt_label(self) -> str:
        return f"P{max(1, self.prompt_version)}"

    def public(self) -> dict:
        """Safe public view — no history internals or raw prompts unless needed."""
        return {
            "feature_id": self.feature_id,
            "original_request": self.original_request,
            "source": self.source,
            "created_at": self.created_at,
            "interpreted_objective": self.interpreted_objective,
            "status": self.status,
            "updated_at": self.updated_at,
            "resource_review_completed": self.resource_review_completed,
            "research_enqueued": self.research_enqueued,
            "risk": self.risk,
            "research_incomplete": self.research_incomplete,
            "research_error": self.research_error,
            "affected_modules": list(self.affected_modules),
            "verified_existing_files": list(self.verified_existing_files),
            "proposed_new_files": list(self.proposed_new_files),
            "unverified_assumptions": list(self.unverified_assumptions),
            "rerun_research": bool(self.rerun_research),
            "chatgpt_reviewed": self.chatgpt_reviewed,
            "chatgpt_skipped": self.chatgpt_skipped,
            "approval_decision": self.approval_decision,
            "cline_handed_off": self.cline_handed_off,
            "implementation_started": self.implementation_started,
            "implementation_result": dict(self.implementation_result or {}),
            "task_id": self.task_id,
            # V1.5 additions (safe structured metadata)
            "research_revision": self.research_revision,
            "chatgpt_request_revision": self.chatgpt_request_revision,
            "prompt_version": self.prompt_version,
            "prompt_source": self.prompt_source,
            "needs_more_research": self.needs_more_research,
            "missing_evidence": list(self.missing_evidence),
            "chatgpt_provenance": dict(self.chatgpt_provenance or {}),
            "prompt_validation": dict(self.prompt_validation or {}),
            "has_candidate_prompt": bool(self.cline_candidate_prompt),
            "proof_history": list(self.proof_history)[-10:],
            "change_requests": [
                {"at": c.get("at"), "kind": c.get("kind"),
                 "text": str(c.get("text", ""))[:160]}
                for c in self.change_requests[-5:]
            ],
        }


class ResearchFindings(BaseModel):
    """Output of the free auto-research stage."""

    observed_facts: list[str] = Field(default_factory=list)
    model_recommendations: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    security_implications: list[str] = Field(default_factory=list)
    failure_risks: list[str] = Field(default_factory=list)
    testing_requirements: list[str] = Field(default_factory=list)
    ui_changes_needed: Optional[bool] = None
    telegram_integration_needed: Optional[bool] = None
    incomplete: bool = False
    error: Optional[str] = None