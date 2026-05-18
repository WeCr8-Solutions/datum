"""
FORGE Core — Decision Ledger
============================
Every significant decision the agent makes is written here:
  - WHY it chose a domain
  - WHY it flagged something as needing repair
  - WHY a repair was accepted or reverted
  - HOW confident it was
  - WHAT happened as a result

This is the "reasoning audit trail" — separate from logs (which are
operational) and state (which tracks document status). The ledger
answers the question: "Why did the agent do that?"

Agents that can't explain their decisions can't be trusted, improved,
or debugged. This ledger is the foundation for all of that.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
from enum import Enum

from .logger import get_logger

log = get_logger("ledger")


# ── Decision types ────────────────────────────────────────────────────────

class DecisionType(str, Enum):
    # Classification decisions
    DOMAIN_DETECTED       = "domain_detected"
    DOCTYPE_DETECTED      = "doc_type_detected"
    ITAR_FLAGGED          = "itar_flagged"

    # Queue decisions
    QUEUED_NEW            = "queued_new"
    QUEUED_CHANGED        = "queued_changed"
    QUEUED_LOW_SCORE      = "queued_low_score"
    SKIPPED_MAX_ATTEMPTS  = "skipped_max_attempts"
    SKIPPED_TOO_SHORT     = "skipped_too_short"

    # Verification decisions
    PASSED_VERIFICATION   = "passed_verification"
    FAILED_VERIFICATION   = "failed_verification"
    RULE_VIOLATION        = "rule_violation"
    WEB_ENRICHMENT_USED   = "web_enrichment_used"
    WEB_ENRICHMENT_SKIPPED= "web_enrichment_skipped"

    # Repair decisions
    REPAIR_TRIGGERED      = "repair_triggered"
    REPAIR_ACCEPTED       = "repair_accepted"
    REPAIR_REVERTED       = "repair_reverted"    # score got worse
    REPAIR_SKIPPED_DRYRUN = "repair_skipped_dryrun"
    REPAIR_FAILED         = "repair_failed"
    REPAIR_NO_CHANGE      = "repair_no_change"   # AI returned same content

    # Human gate decisions
    HELD_FOR_REVIEW       = "held_for_review"
    APPROVED_BY_HUMAN     = "approved_by_human"
    REJECTED_BY_HUMAN     = "rejected_by_human"
    ROLLBACK_REQUESTED    = "rollback_requested"
    ROLLBACK_COMPLETED    = "rollback_completed"

    # Cross-document decisions
    REFERENCE_BROKEN      = "reference_broken"
    CONTRADICTION_FOUND   = "contradiction_found"
    DEPENDENCY_MISSING    = "dependency_missing"

    # Commit decisions
    COMMITTED             = "committed"
    COMMIT_SKIPPED        = "commit_skipped"
    REINDEXED             = "reindexed"

    # Pattern learning
    PATTERN_LEARNED       = "pattern_learned"
    PATTERN_APPLIED       = "pattern_applied"


class Confidence(str, Enum):
    CERTAIN   = "certain"    # Rule-based, deterministic
    HIGH      = "high"       # AI confident, >85% score agreement
    MEDIUM    = "medium"     # AI moderately confident, 65-85%
    LOW       = "low"        # AI uncertain, <65% or conflicting signals
    UNKNOWN   = "unknown"    # Could not assess


class Outcome(str, Enum):
    SUCCESS   = "success"
    FAILURE   = "failure"
    PENDING   = "pending"    # Human review required
    SKIPPED   = "skipped"
    REVERTED  = "reverted"


# ── Decision record ───────────────────────────────────────────────────────

@dataclass
class Decision:
    """A single agent decision with full reasoning and outcome."""

    id:           str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp:    str   = field(default_factory=lambda: datetime.now().isoformat())
    run_id:       str   = ""

    # What the decision was about
    decision_type: str  = ""
    doc_path:     str   = ""
    doc_domain:   str   = ""
    doc_type:     str   = ""

    # The decision itself
    decision:     str   = ""       # Human-readable: "Repaired SOP-001.md"
    reasoning:    str   = ""       # WHY: "Failed 2 safety rules + score 58/100"
    evidence:     list  = field(default_factory=list)  # Supporting facts
    alternatives: list  = field(default_factory=list)  # What else was considered

    # Confidence and outcome
    confidence:   str   = Confidence.UNKNOWN
    confidence_score: Optional[float] = None  # 0.0-1.0
    outcome:      str   = Outcome.PENDING
    outcome_detail: str = ""

    # Score tracking
    score_before: Optional[int] = None
    score_after:  Optional[int] = None
    score_delta:  Optional[int] = None

    # AI provider used
    ai_provider:  str   = ""
    ai_model:     str   = ""
    ai_tokens:    int   = 0

    # Human review
    requires_human_review: bool = False
    reviewed_by:  Optional[str] = None
    reviewed_at:  Optional[str] = None
    review_notes: Optional[str] = None

    # Links
    parent_decision_id: Optional[str] = None  # For chains of decisions
    related_doc_paths:  list = field(default_factory=list)

    def resolve(self, outcome: Outcome, detail: str = ""):
        self.outcome = outcome
        self.outcome_detail = detail

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.score_before is not None and self.score_after is not None:
            d["score_delta"] = self.score_after - self.score_before
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Ledger ────────────────────────────────────────────────────────────────

class DecisionLedger:
    """
    Append-only decision log. Persisted as newline-delimited JSON (NDJSON)
    for efficient streaming and query without loading the whole file.
    Also maintains an in-memory index for fast lookups this session.
    """

    def __init__(self, ledger_dir: str = "./logs", run_id: str = ""):
        self.ledger_dir  = Path(ledger_dir)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.run_id      = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.ledger_file = self.ledger_dir / "decisions.ndjson"
        self.run_file    = self.ledger_dir / f"run_{self.run_id}.ndjson"
        self._index: dict[str, Decision] = {}    # id → Decision
        self._doc_index: dict[str, list] = {}    # doc_path → [Decision ids]
        self._session_decisions: list[Decision] = []

    # ── Core record method ────────────────────────────────────────────────

    def record(
        self,
        decision_type: DecisionType,
        doc_path: str = "",
        decision: str = "",
        reasoning: str = "",
        confidence: Confidence = Confidence.UNKNOWN,
        confidence_score: Optional[float] = None,
        outcome: Outcome = Outcome.SUCCESS,
        outcome_detail: str = "",
        evidence: list = None,
        alternatives: list = None,
        score_before: Optional[int] = None,
        score_after: Optional[int] = None,
        ai_provider: str = "",
        ai_model: str = "",
        ai_tokens: int = 0,
        requires_human_review: bool = False,
        parent_decision_id: Optional[str] = None,
        related_doc_paths: list = None,
        doc_domain: str = "",
        doc_type: str = "",
        **kwargs
    ) -> Decision:
        """Record a decision. Returns the Decision object (use its .id for chaining)."""

        d = Decision(
            run_id=self.run_id,
            decision_type=decision_type.value if isinstance(decision_type, DecisionType) else decision_type,
            doc_path=doc_path,
            doc_domain=doc_domain,
            doc_type=doc_type,
            decision=decision,
            reasoning=reasoning,
            confidence=confidence.value if isinstance(confidence, Confidence) else confidence,
            confidence_score=confidence_score,
            outcome=outcome.value if isinstance(outcome, Outcome) else outcome,
            outcome_detail=outcome_detail,
            evidence=evidence or [],
            alternatives=alternatives or [],
            score_before=score_before,
            score_after=score_after,
            ai_provider=ai_provider,
            ai_model=ai_model,
            ai_tokens=ai_tokens,
            requires_human_review=requires_human_review,
            parent_decision_id=parent_decision_id,
            related_doc_paths=related_doc_paths or [],
        )

        if score_before is not None and score_after is not None:
            d.score_delta = score_after - score_before

        # Write to disk immediately (append-only)
        line = json.dumps(d.to_dict()) + "\n"
        with open(self.ledger_file, "a") as f:
            f.write(line)
        with open(self.run_file, "a") as f:
            f.write(line)

        # Update in-memory indexes
        self._index[d.id] = d
        if doc_path:
            self._doc_index.setdefault(doc_path, []).append(d.id)
        self._session_decisions.append(d)

        log.debug(f"Decision [{d.decision_type}] {doc_path}: {decision[:60]}")
        return d

    # ── Update existing decision outcome ──────────────────────────────────

    def update_outcome(self, decision_id: str, outcome: Outcome,
                       detail: str = "", reviewed_by: str = None,
                       review_notes: str = None):
        """Update the outcome of a previous decision (e.g., after human review)."""
        if decision_id not in self._index:
            log.warning(f"Decision {decision_id} not in session index — cannot update")
            return

        d = self._index[decision_id]
        d.outcome = outcome.value
        d.outcome_detail = detail
        if reviewed_by:
            d.reviewed_by = reviewed_by
            d.reviewed_at = datetime.now().isoformat()
            d.review_notes = review_notes

        # Append an update record
        update = d.to_dict()
        update["_update_of"] = decision_id
        update["_updated_at"] = datetime.now().isoformat()
        with open(self.ledger_file, "a") as f:
            f.write(json.dumps(update) + "\n")

    # ── Query ─────────────────────────────────────────────────────────────

    def get_decisions_for_doc(self, doc_path: str) -> list[Decision]:
        """All decisions made about a specific document, this session."""
        ids = self._doc_index.get(doc_path, [])
        return [self._index[i] for i in ids if i in self._index]

    def get_pending_reviews(self) -> list[Decision]:
        """Decisions awaiting human review."""
        return [
            d for d in self._session_decisions
            if d.requires_human_review and d.outcome == Outcome.PENDING.value
        ]

    def get_session_summary(self) -> dict:
        decisions = self._session_decisions
        by_type   = {}
        for d in decisions:
            by_type[d.decision_type] = by_type.get(d.decision_type, 0) + 1

        outcomes = {}
        for d in decisions:
            outcomes[d.outcome] = outcomes.get(d.outcome, 0) + 1

        repairs    = [d for d in decisions if d.decision_type == DecisionType.REPAIR_ACCEPTED.value]
        reversions = [d for d in decisions if d.decision_type == DecisionType.REPAIR_REVERTED.value]

        avg_confidence = None
        conf_scored = [d.confidence_score for d in decisions if d.confidence_score is not None]
        if conf_scored:
            avg_confidence = round(sum(conf_scored) / len(conf_scored), 2)

        return {
            "run_id":          self.run_id,
            "total_decisions": len(decisions),
            "by_type":         by_type,
            "by_outcome":      outcomes,
            "repairs":         len(repairs),
            "reversions":      len(reversions),
            "pending_reviews": len(self.get_pending_reviews()),
            "avg_confidence":  avg_confidence,
            "ledger_file":     str(self.ledger_file),
        }

    @classmethod
    def load_history(cls, ledger_file: str, last_n: int = 500) -> list[dict]:
        """Load recent decisions from the NDJSON ledger file."""
        path = Path(ledger_file)
        if not path.exists():
            return []
        lines = path.read_text().strip().split("\n")
        recent = lines[-last_n:]
        results = []
        for line in recent:
            try:
                results.append(json.loads(line))
            except Exception:
                pass
        return results
