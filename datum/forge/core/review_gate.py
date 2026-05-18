"""
FORGE Core — Human Review Gate
================================
For controlled documents, the agent holds repairs for human approval
before committing to Git. Admins review via a simple web UI or CLI.

Also handles rollback: if a human rejects a committed repair,
this module reverts the Git commit and teaches the agent not to
make the same mistake.

Review queue lives in ./review_queue/ as individual JSON files.
Simple enough to work with a shared folder, email, or a future web UI.
"""

import json
import shutil
import asyncio
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

from .logger import get_logger
from .ledger import DecisionLedger, DecisionType, Confidence, Outcome

log = get_logger("review_gate")


class ReviewStatus(str, Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    REJECTED  = "rejected"
    EXPIRED   = "expired"   # No response within deadline


class ReviewPriority(str, Enum):
    CRITICAL = "critical"   # Safety-related, ITAR-adjacent
    HIGH     = "high"       # Quality manual, controlled SOP
    MEDIUM   = "medium"     # General SOP
    LOW      = "low"        # Reference documents, training


@dataclass
class ReviewItem:
    """A document repair waiting for human approval."""
    id:             str  = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    created_at:     str  = field(default_factory=lambda: datetime.now().isoformat())
    expires_at:     str  = ""    # ISO datetime — auto-approve or reject if no response

    # Document info
    doc_path:       str  = ""
    doc_domain:     str  = ""
    doc_type:       str  = ""
    itar_flagged:   bool = False

    # Scores
    score_before:   int  = 0
    score_after:    int  = 0
    score_delta:    int  = 0

    # What changed
    summary:        str  = ""    # Human-readable summary of changes
    diff:           str  = ""    # Full unified diff
    changes_made:   list = field(default_factory=list)
    failures_fixed: list = field(default_factory=list)
    warnings:       list = field(default_factory=list)

    # Backup for rollback
    content_before: str  = ""
    content_after:  str  = ""
    git_commit_hash:str  = ""    # Commit to revert if rejected

    # Review state
    status:         str  = ReviewStatus.PENDING
    priority:       str  = ReviewPriority.MEDIUM
    reviewed_by:    str  = ""
    reviewed_at:    str  = ""
    review_notes:   str  = ""

    # Decision tracking
    decision_id:    str  = ""    # Ledger decision ID

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewItem":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def age_hours(self) -> float:
        try:
            created = datetime.fromisoformat(self.created_at)
            return (datetime.now() - created).total_seconds() / 3600
        except Exception:
            return 0.0


class HumanReviewGate:
    """
    Manages the human review queue for agent repairs.

    Docs requiring review (based on domain rules or ITAR flags)
    are held here until a human approves or rejects them.
    Approved → committed to Git.
    Rejected → rolled back, agent learns from failure.
    """

    # Doc types that always require human review
    ALWAYS_REVIEW = {
        "sop", "quality_manual", "g_code_reference"
    }

    # Domains where everything needs review
    REVIEW_DOMAINS = set()  # e.g., {"aerospace_itar"}

    def __init__(self, config: dict, ledger: DecisionLedger,
                 repo_path: str = ".", review_dir: str = "./review_queue"):
        self.cfg        = config
        self.ledger     = ledger
        self.repo_path  = Path(repo_path)
        self.review_dir = Path(review_dir)
        self.review_dir.mkdir(parents=True, exist_ok=True)

        # Config: require_review_for types
        self.require_review_types = set(
            config.get("human_review", {}).get("require_for_doc_types", list(self.ALWAYS_REVIEW))
        )
        self.require_review_itar = config.get("human_review", {}).get("always_review_itar", True)
        self.auto_approve_hours = config.get("human_review", {}).get("auto_approve_hours", 0)

    def requires_review(self, doc_type: str, domain: str,
                         itar_flagged: bool, score_delta: int) -> tuple[bool, str]:
        """
        Decide if this repair needs human review before committing.
        Returns (requires_review, reason).
        """
        if itar_flagged and self.require_review_itar:
            return True, "ITAR-flagged document — human review required before commit"

        if doc_type in self.require_review_types:
            return True, f"Doc type '{doc_type}' requires human review per policy"

        if domain in self.REVIEW_DOMAINS:
            return True, f"Domain '{domain}' requires human review for all changes"

        if score_delta < 0:
            return True, f"Score decreased after repair ({score_delta:+d}) — review needed"

        return False, ""

    def submit_for_review(self, repair_result, verification_before,
                           verification_after, priority: ReviewPriority = None) -> ReviewItem:
        """Submit a repair for human review instead of auto-committing."""
        from datetime import timedelta

        score_delta = repair_result.score_after - repair_result.score_before

        # Determine priority
        if priority is None:
            if getattr(verification_before, "itar_flagged", False):
                priority = ReviewPriority.CRITICAL
            elif repair_result.score_before < 50:
                priority = ReviewPriority.HIGH
            else:
                priority = ReviewPriority.MEDIUM

        # Set expiry
        expires_hours = self.auto_approve_hours or 72  # 72h default
        expires_at = (datetime.now() + timedelta(hours=expires_hours)).isoformat()

        item = ReviewItem(
            doc_path=repair_result.path,
            doc_domain=getattr(verification_before, "domain", "general"),
            doc_type=getattr(verification_before, "doc_type", "document"),
            itar_flagged=getattr(verification_before, "itar_flagged", False),
            score_before=repair_result.score_before,
            score_after=repair_result.score_after,
            score_delta=score_delta,
            summary=self._build_summary(repair_result, verification_before, score_delta),
            diff=repair_result.diff[:5000],  # Truncate for storage
            changes_made=repair_result.changes_made,
            failures_fixed=[f.get("rule_id") for f in getattr(verification_before, "failures", [])],
            warnings=[w.get("message") for w in getattr(verification_before, "warnings", [])[:3]],
            content_before=repair_result.content_before[:10000],
            content_after=repair_result.content_after[:10000],
            priority=priority.value,
            expires_at=expires_at,
        )

        # Record in ledger
        decision = self.ledger.record(
            decision_type=DecisionType.HELD_FOR_REVIEW,
            doc_path=repair_result.path,
            doc_domain=item.doc_domain,
            doc_type=item.doc_type,
            decision=f"Held for human review: {item.doc_path}",
            reasoning=item.summary,
            confidence=Confidence.HIGH,
            outcome=Outcome.PENDING,
            score_before=repair_result.score_before,
            score_after=repair_result.score_after,
            requires_human_review=True,
        )
        item.decision_id = decision.id

        # Save to queue directory
        queue_file = self.review_dir / f"{item.id}.json"
        queue_file.write_text(json.dumps(item.to_dict(), indent=2))

        log.info(f"📋 Queued for review [{priority.value}]: {repair_result.path} "
                 f"(score {repair_result.score_before}→{repair_result.score_after})")

        return item

    def _build_summary(self, repair_result, verification_before, score_delta: int) -> str:
        failures = getattr(verification_before, "failures", [])
        changes  = repair_result.changes_made[:3]
        delta_str = f"+{score_delta}" if score_delta > 0 else str(score_delta)

        lines = [
            f"Score: {repair_result.score_before} → {repair_result.score_after} ({delta_str})",
        ]
        if failures:
            lines.append(f"Fixed: {'; '.join(f.get('message','')[:50] for f in failures[:2])}")
        if changes:
            lines.append(f"Changes: {'; '.join(c[:50] for c in changes)}")
        return "\n".join(lines)

    def get_pending(self) -> list[ReviewItem]:
        """Get all pending review items, sorted by priority."""
        items = []
        priority_order = {
            ReviewPriority.CRITICAL.value: 0,
            ReviewPriority.HIGH.value: 1,
            ReviewPriority.MEDIUM.value: 2,
            ReviewPriority.LOW.value: 3,
        }

        for f in self.review_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                item = ReviewItem.from_dict(data)
                if item.status == ReviewStatus.PENDING.value:
                    items.append(item)
            except Exception:
                pass

        items.sort(key=lambda x: priority_order.get(x.priority, 99))
        return items

    def approve(self, item_id: str, reviewed_by: str = "admin",
                 notes: str = "") -> tuple[bool, str]:
        """Approve a repair — content is already written, just commit it."""
        queue_file = self.review_dir / f"{item_id}.json"
        if not queue_file.exists():
            return False, f"Review item {item_id} not found"

        try:
            item = ReviewItem.from_dict(json.loads(queue_file.read_text()))
            item.status      = ReviewStatus.APPROVED.value
            item.reviewed_by = reviewed_by
            item.reviewed_at = datetime.now().isoformat()
            item.review_notes= notes

            queue_file.write_text(json.dumps(item.to_dict(), indent=2))

            self.ledger.record(
                decision_type=DecisionType.APPROVED_BY_HUMAN,
                doc_path=item.doc_path,
                decision=f"Human approved repair: {item.doc_path}",
                reasoning=notes or "Approved without notes",
                confidence=Confidence.CERTAIN,
                outcome=Outcome.SUCCESS,
                parent_decision_id=item.decision_id,
            )

            log.info(f"✅ Approved by {reviewed_by}: {item.doc_path}")
            return True, "Approved"

        except Exception as e:
            return False, str(e)

    def reject(self, item_id: str, reviewed_by: str = "admin",
                reason: str = "", memory=None) -> tuple[bool, str]:
        """
        Reject a repair — restore content from before, teach the agent.
        """
        queue_file = self.review_dir / f"{item_id}.json"
        if not queue_file.exists():
            return False, f"Review item {item_id} not found"

        try:
            item = ReviewItem.from_dict(json.loads(queue_file.read_text()))

            # Restore original content
            doc_path = self.repo_path / item.doc_path
            if item.content_before and doc_path.exists():
                doc_path.write_text(item.content_before, encoding="utf-8")
                log.info(f"Restored original content: {item.doc_path}")

            item.status      = ReviewStatus.REJECTED.value
            item.reviewed_by = reviewed_by
            item.reviewed_at = datetime.now().isoformat()
            item.review_notes= reason
            queue_file.write_text(json.dumps(item.to_dict(), indent=2))

            self.ledger.record(
                decision_type=DecisionType.REJECTED_BY_HUMAN,
                doc_path=item.doc_path,
                decision=f"Human rejected repair: {item.doc_path}",
                reasoning=reason or "No reason given",
                confidence=Confidence.CERTAIN,
                outcome=Outcome.FAILURE,
                parent_decision_id=item.decision_id,
            )

            # Teach the agent
            if memory:
                memory.learn_from_human_rejection(
                    doc_path=item.doc_path,
                    domain=item.doc_domain,
                    doc_type=item.doc_type,
                    rejection_reason=reason
                )

            log.info(f"❌ Rejected by {reviewed_by}: {item.doc_path} — {reason}")
            return True, "Rejected and content restored"

        except Exception as e:
            return False, str(e)

    def rollback_commit(self, item_id: str, reason: str = "",
                         memory=None) -> tuple[bool, str]:
        """Roll back a Git commit for a rejected repair."""
        queue_file = self.review_dir / f"{item_id}.json"
        if not queue_file.exists():
            return False, "Item not found"

        try:
            item = ReviewItem.from_dict(json.loads(queue_file.read_text()))

            if not item.git_commit_hash:
                return False, "No commit hash stored — cannot rollback"

            import git
            repo = git.Repo(self.repo_path)
            repo.git.revert(item.git_commit_hash, no_edit=True)

            self.ledger.record(
                decision_type=DecisionType.ROLLBACK_COMPLETED,
                doc_path=item.doc_path,
                decision=f"Rolled back commit {item.git_commit_hash[:8]}",
                reasoning=reason or "Human-requested rollback",
                confidence=Confidence.CERTAIN,
                outcome=Outcome.REVERTED,
            )

            if memory:
                memory.learn_from_human_rejection(
                    doc_path=item.doc_path,
                    domain=item.doc_domain,
                    doc_type=item.doc_type,
                    rejection_reason=reason
                )

            log.info(f"↩ Rolled back {item.git_commit_hash[:8]}: {item.doc_path}")
            return True, f"Rolled back commit {item.git_commit_hash[:8]}"

        except Exception as e:
            return False, str(e)

    def print_queue(self):
        """CLI: print the review queue in a readable format."""
        items = self.get_pending()
        if not items:
            print("✅ No items pending review.")
            return

        print(f"\n{'='*60}")
        print(f"FORGE Review Queue — {len(items)} pending")
        print(f"{'='*60}")
        for item in items:
            print(f"\n[{item.priority.upper()}] {item.id}")
            print(f"  Doc:    {item.doc_path}")
            print(f"  Score:  {item.score_before} → {item.score_after} ({item.score_delta:+d})")
            print(f"  Age:    {item.age_hours:.1f} hours")
            print(f"  ITAR:   {'YES ⚠' if item.itar_flagged else 'No'}")
            print(f"  Summary: {item.summary.splitlines()[0] if item.summary else ''}")
        print()
