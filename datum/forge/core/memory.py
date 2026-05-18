"""
FORGE Core — Agent Memory
==========================
The agent learns from what it has done before.

Two types of memory:
1. PATTERN MEMORY — repair patterns that worked. If the agent successfully
   fixed "missing PPE requirement in coolant-handling SOPs" three times,
   it remembers the pattern and applies it confidently on the fourth.

2. FAILURE MEMORY — repairs that were reverted or rejected by humans.
   The agent remembers what NOT to do, avoids the same mistake.

This is NOT a vector database or complex embedding system. It's a structured
JSON library of patterns, updated after every run. Simple, auditable,
and works with zero additional infrastructure.
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
import difflib

from .logger import get_logger

log = get_logger("memory")


@dataclass
class RepairPattern:
    """A pattern the agent learned from a successful repair."""
    id:           str   = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created_at:   str   = field(default_factory=lambda: datetime.now().isoformat())
    last_used:    str   = ""
    use_count:    int   = 0
    success_count:int   = 0
    failure_count:int   = 0

    # Pattern description
    domain:       str   = "general"
    doc_type:     str   = "document"
    trigger:      str   = ""    # What situation triggers this pattern
    rule_violated:str   = ""    # Which verification rule this fixes
    fix_strategy: str   = ""    # How to fix it (instructions for the AI)
    example_before: str = ""    # Short example of bad content
    example_after:  str = ""    # Short example of fixed content

    # Quality metrics
    avg_score_improvement: float = 0.0
    confidence:   float = 0.5   # 0.0-1.0, increases with successful uses

    # Tags for retrieval
    tags:         list  = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RepairPattern":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def record_use(self, succeeded: bool, score_improvement: int = 0):
        self.use_count += 1
        self.last_used = datetime.now().isoformat()
        if succeeded:
            self.success_count += 1
            # Running average of score improvement
            n = self.success_count
            self.avg_score_improvement = (
                (self.avg_score_improvement * (n - 1) + score_improvement) / n
            )
            # Confidence increases with successful use, up to 0.95
            self.confidence = min(0.95, self.confidence + 0.05)
        else:
            self.failure_count += 1
            # Confidence decreases on failure
            self.confidence = max(0.05, self.confidence - 0.15)

    @property
    def success_rate(self) -> float:
        if self.use_count == 0:
            return 0.0
        return self.success_count / self.use_count

    @property
    def is_reliable(self) -> bool:
        return self.use_count >= 3 and self.success_rate >= 0.7 and self.confidence >= 0.6


@dataclass
class FailurePattern:
    """A pattern the agent learned NOT to do."""
    id:         str   = field(default_factory=lambda: uuid.uuid4().hex[:10])
    created_at: str   = field(default_factory=lambda: datetime.now().isoformat())
    domain:     str   = "general"
    doc_type:   str   = "document"
    description:str   = ""   # What went wrong
    trigger:    str   = ""   # What situation caused this failure
    what_not_to_do: str = "" # Specific instruction to avoid
    rejection_reason: str = ""  # Human's reason for rejecting (if any)
    rejection_count:  int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FailurePattern":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class AgentMemory:
    """
    Persistent agent memory. Learns repair patterns from successful runs,
    avoids failure patterns from reversions and human rejections.
    """

    def __init__(self, memory_dir: str = "./memory"):
        self.memory_dir    = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.patterns_file = self.memory_dir / "repair_patterns.json"
        self.failures_file = self.memory_dir / "failure_patterns.json"
        self.context_file  = self.memory_dir / "domain_context.json"

        self.repair_patterns: dict[str, RepairPattern]  = {}
        self.failure_patterns: dict[str, FailurePattern] = {}
        self.domain_context: dict[str, dict] = {}  # domain-specific learned facts

        self._load()

    def _load(self):
        if self.patterns_file.exists():
            try:
                data = json.loads(self.patterns_file.read_text())
                self.repair_patterns = {
                    k: RepairPattern.from_dict(v) for k, v in data.items()
                }
                log.info(f"Memory: {len(self.repair_patterns)} repair patterns loaded")
            except Exception as e:
                log.warning(f"Repair patterns load failed: {e}")

        if self.failures_file.exists():
            try:
                data = json.loads(self.failures_file.read_text())
                self.failure_patterns = {
                    k: FailurePattern.from_dict(v) for k, v in data.items()
                }
                log.info(f"Memory: {len(self.failure_patterns)} failure patterns loaded")
            except Exception as e:
                log.warning(f"Failure patterns load failed: {e}")

        if self.context_file.exists():
            try:
                self.domain_context = json.loads(self.context_file.read_text())
            except Exception:
                pass

    def save(self):
        self.patterns_file.write_text(
            json.dumps({k: v.to_dict() for k, v in self.repair_patterns.items()}, indent=2)
        )
        self.failures_file.write_text(
            json.dumps({k: v.to_dict() for k, v in self.failure_patterns.items()}, indent=2)
        )
        self.context_file.write_text(json.dumps(self.domain_context, indent=2))

    # ── Pattern retrieval ──────────────────────────────────────────────────

    def get_relevant_patterns(self, domain: str, doc_type: str,
                               rule_violated: str = "",
                               tags: list = None) -> list[RepairPattern]:
        """Find repair patterns relevant to this situation, sorted by confidence."""
        relevant = []
        for p in self.repair_patterns.values():
            score = 0
            if p.domain == domain:         score += 3
            if p.domain == "general":      score += 1  # General patterns always relevant
            if p.doc_type == doc_type:     score += 2
            if p.doc_type == "document":   score += 1
            if rule_violated and p.rule_violated == rule_violated: score += 4
            if tags:
                matching_tags = set(tags) & set(p.tags)
                score += len(matching_tags) * 2

            if score >= 2:  # Minimum relevance threshold
                relevant.append((score * p.confidence, p))

        relevant.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in relevant[:5]]  # Top 5

    def get_failure_warnings(self, domain: str, doc_type: str) -> list[str]:
        """Get "don't do this" instructions relevant to this situation."""
        warnings = []
        for f in self.failure_patterns.values():
            if f.domain in (domain, "general") and f.doc_type in (doc_type, "document"):
                warnings.append(f.what_not_to_do)
        return warnings[:5]

    def format_for_prompt(self, domain: str, doc_type: str,
                           rule_violated: str = "") -> str:
        """Format memory as context for the AI repair prompt."""
        patterns  = self.get_relevant_patterns(domain, doc_type, rule_violated)
        warnings  = self.get_failure_warnings(domain, doc_type)
        lines     = []

        if patterns:
            reliable = [p for p in patterns if p.is_reliable]
            if reliable:
                lines.append("PROVEN REPAIR PATTERNS (apply these confidently):")
                for p in reliable[:3]:
                    lines.append(
                        f"  [{p.domain}/{p.doc_type}] {p.trigger}\n"
                        f"  → Fix: {p.fix_strategy}\n"
                        f"  → Success rate: {p.success_rate:.0%} ({p.use_count} uses, "
                        f"+{p.avg_score_improvement:.0f} avg score improvement)"
                    )

        if warnings:
            lines.append("\nKNOWN FAILURE PATTERNS (avoid these):")
            for w in warnings:
                lines.append(f"  ✗ {w}")

        return "\n".join(lines) if lines else ""

    # ── Learning ──────────────────────────────────────────────────────────

    def learn_from_repair(self, repair_result, verification_before,
                           verification_after) -> Optional[RepairPattern]:
        """
        Extract a pattern from a successful repair and store it.
        Called after each successful repair.
        """
        if not repair_result.success or not repair_result.improved:
            return None

        score_delta = repair_result.score_after - repair_result.score_before
        if score_delta < 5:  # Only learn from meaningful improvements
            return None

        failures = verification_before.failures
        if not failures:
            return None

        # Use the most severe failure as the pattern trigger
        main_failure = failures[0]

        # Check if we already have this pattern
        existing = self._find_similar_pattern(
            verification_before.domain,
            verification_before.doc_type,
            main_failure.get("rule_id", "")
        )

        if existing:
            existing.record_use(True, score_delta)
            log.debug(f"Pattern reinforced: {existing.id} (use #{existing.use_count})")
            self.save()
            return existing

        # Create new pattern
        # Extract example from diff
        diff_lines = repair_result.diff.split("\n")
        removed = [l[1:].strip() for l in diff_lines if l.startswith("-") and not l.startswith("---")][:2]
        added   = [l[1:].strip() for l in diff_lines if l.startswith("+") and not l.startswith("+++")][:2]

        pattern = RepairPattern(
            domain=verification_before.domain,
            doc_type=verification_before.doc_type,
            trigger=main_failure.get("message", ""),
            rule_violated=main_failure.get("rule_id", ""),
            fix_strategy=f"Address: {main_failure.get('message', '')}",
            example_before="\n".join(removed),
            example_after="\n".join(added),
            avg_score_improvement=float(score_delta),
            confidence=0.5,
            tags=[
                verification_before.domain,
                verification_before.doc_type,
                main_failure.get("category", ""),
            ],
        )

        pattern.record_use(True, score_delta)
        self.repair_patterns[pattern.id] = pattern

        log.info(f"New pattern learned: [{pattern.domain}/{pattern.doc_type}] "
                 f"{pattern.trigger[:50]} (score +{score_delta})")

        self.save()
        return pattern

    def learn_from_reversion(self, repair_result, reason: str = ""):
        """Learn what NOT to do from a repair that made things worse."""
        failures = []
        if hasattr(repair_result, "content_before") and hasattr(repair_result, "content_after"):
            diff = repair_result.diff
            removed = [l[1:] for l in diff.split("\n") if l.startswith("-") and not l.startswith("---")]
            added   = [l[1:] for l in diff.split("\n") if l.startswith("+") and not l.startswith("+++")]

            what_not_to_do = (
                f"Do not make changes like: {added[0][:80]}"
                if added else "Avoid aggressive restructuring of this doc type"
            )
        else:
            what_not_to_do = "Avoid overwriting this document type without strong evidence"

        fp = FailurePattern(
            domain=getattr(repair_result, "domain", "general"),
            doc_type=getattr(repair_result, "doc_type", "document"),
            description=f"Repair reverted: score worsened on {repair_result.path}",
            trigger=f"Repair of {repair_result.path}",
            what_not_to_do=what_not_to_do,
            rejection_reason=reason,
            rejection_count=1,
        )
        self.failure_patterns[fp.id] = fp
        log.info(f"Failure pattern recorded: {what_not_to_do[:60]}")
        self.save()

    def learn_from_human_rejection(self, doc_path: str, domain: str,
                                    doc_type: str, rejection_reason: str):
        """Called when a human rejects an AI repair via the review gate."""
        fp = FailurePattern(
            domain=domain,
            doc_type=doc_type,
            description=f"Human rejected repair of {doc_path}",
            trigger=f"Repair of {doc_type} in {domain} domain",
            what_not_to_do=rejection_reason[:200] if rejection_reason else
                           "Avoid this type of change — human reviewer rejected it",
            rejection_reason=rejection_reason,
            rejection_count=1,
        )
        self.failure_patterns[fp.id] = fp
        self.save()

    def _find_similar_pattern(self, domain: str, doc_type: str,
                               rule_id: str) -> Optional[RepairPattern]:
        for p in self.repair_patterns.values():
            if (p.domain == domain and
                p.doc_type == doc_type and
                p.rule_violated == rule_id):
                return p
        return None

    # ── Domain context ────────────────────────────────────────────────────

    def update_domain_context(self, domain: str, key: str, value):
        """Store a domain-specific learned fact."""
        if domain not in self.domain_context:
            self.domain_context[domain] = {}
        self.domain_context[domain][key] = {
            "value": value,
            "updated": datetime.now().isoformat()
        }
        self.save()

    def get_domain_context(self, domain: str) -> dict:
        return {k: v["value"] for k, v in self.domain_context.get(domain, {}).items()}

    # ── Stats ─────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        reliable = [p for p in self.repair_patterns.values() if p.is_reliable]
        return {
            "total_patterns":      len(self.repair_patterns),
            "reliable_patterns":   len(reliable),
            "failure_patterns":    len(self.failure_patterns),
            "total_pattern_uses":  sum(p.use_count for p in self.repair_patterns.values()),
            "avg_success_rate":    (
                sum(p.success_rate for p in self.repair_patterns.values()) /
                max(len(self.repair_patterns), 1)
            ),
        }
