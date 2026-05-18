"""
ANVIL Core — Shared Types
All data structures used across ANVIL modules.
Keeping types central prevents circular imports and
makes the data model explicit and auditable.
"""

from __future__ import annotations
import uuid
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum


# ── Code unit — the atom ANVIL works with ─────────────────────────────────

class UnitKind(str, Enum):
    FUNCTION    = "function"
    CLASS       = "class"
    METHOD      = "method"
    CONSTANT    = "constant"
    CONFIG      = "config"
    ROUTE       = "route"        # API endpoint
    SCHEMA      = "schema"       # DB schema, type definition
    IMPORT      = "import"
    MODULE      = "module"       # Whole file as a unit
    BLOCK       = "block"        # Generic code block


@dataclass
class CodeUnit:
    """
    A semantically meaningful piece of code extracted from a file.
    The atom of ANVIL's analysis — everything else operates on CodeUnits.
    """
    id:           str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    repo_id:      str   = ""
    file_path:    str   = ""       # Relative to repo root
    language:     str   = ""
    kind:         str   = UnitKind.FUNCTION
    name:         str   = ""       # Function/class/variable name
    signature:    str   = ""       # Full signature line
    body:         str   = ""       # The actual code
    docstring:    str   = ""       # Inline documentation
    line_start:   int   = 0
    line_end:     int   = 0
    imports:      list  = field(default_factory=list)
    calls:        list  = field(default_factory=list)   # Functions this calls
    tags:         list  = field(default_factory=list)   # Semantic tags
    complexity:   int   = 0        # Cyclomatic complexity estimate
    last_modified:str   = ""

    @property
    def display(self) -> str:
        return f"{self.language}/{self.kind}:{self.name} ({self.file_path}:{self.line_start})"

    @property
    def full_text(self) -> str:
        parts = []
        if self.docstring:
            parts.append(f"# {self.docstring}")
        if self.signature:
            parts.append(self.signature)
        if self.body:
            parts.append(self.body)
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Doc section — what FORGE verified ────────────────────────────────────

@dataclass
class DocSection:
    """A section of a FORGE-verified document used as a specification."""
    id:             str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    doc_path:       str   = ""
    doc_domain:     str   = ""
    doc_type:       str   = ""
    doc_quality:    int   = 0      # FORGE quality score — only use if ≥ threshold
    section_title:  str   = ""
    content:        str   = ""
    line_start:     int   = 0
    line_end:       int   = 0
    forge_verified: bool  = False  # Was this doc verified by FORGE?
    forge_score:    int   = 0
    last_forge_run: str   = ""
    tags:           list  = field(default_factory=list)
    # Extracted specifications from this section
    specs:          list  = field(default_factory=list)  # [{type, value, unit}]

    def to_dict(self) -> dict:
        return asdict(self)


# ── Binding — the link between a doc section and code unit ────────────────

@dataclass
class Binding:
    """
    A mapping between a DocSection (the spec) and a CodeUnit (the implementation).
    The core relationship ANVIL works with — everything else is downstream of this.
    """
    id:             str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    doc_section_id: str   = ""
    code_unit_id:   str   = ""
    similarity:     float = 0.0    # Embedding cosine similarity 0-1
    binding_type:   str   = ""     # "implements" | "configures" | "validates" | "references"
    confidence:     float = 0.0    # How confident ANVIL is this binding is correct
    created_at:     str   = field(default_factory=lambda: datetime.now().isoformat())
    last_verified:  str   = ""
    is_stale:       bool  = False  # Doc updated since last verification

    def to_dict(self) -> dict:
        return asdict(self)


# ── Audit issue ────────────────────────────────────────────────────────────

class IssueType(str, Enum):
    CONTRADICTION   = "contradiction"   # Code does X, doc says Y
    MISSING_IMPL    = "missing_impl"    # Doc says feature exists, code doesn't
    DRIFT           = "drift"           # Doc updated, code not re-verified
    DEAD_CODE       = "dead_code"       # Code with no doc reference (possibly obsolete)
    UNVERIFIED      = "unverified"      # Code with no doc binding at all


class IssueSeverity(str, Enum):
    ERROR   = "error"    # Must fix — code contradicts verified spec
    WARNING = "warning"  # Should fix — missing implementation
    INFO    = "info"     # FYI — drift or dead code
    SKIP    = "skip"     # Explicitly excluded from audit


@dataclass
class AuditIssue:
    """An issue found when comparing a code unit against its doc spec."""
    id:             str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    repo_id:        str   = ""
    issue_type:     str   = IssueType.DRIFT
    severity:       str   = IssueSeverity.INFO
    file_path:      str   = ""
    line_start:     int   = 0
    line_end:       int   = 0
    code_unit_id:   str   = ""
    doc_section_id: str   = ""
    binding_id:     str   = ""

    # The actual finding
    title:          str   = ""
    description:    str   = ""    # What's wrong
    doc_says:       str   = ""    # What the document specifies
    code_does:      str   = ""    # What the code actually does
    evidence:       list  = field(default_factory=list)

    # Fix information
    auto_fixable:   bool  = False
    suggested_fix:  str   = ""
    patch_hint:     str   = ""    # Hint for the patch generator

    # Status tracking
    status:         str   = "open"   # open | patched | wont_fix | false_positive
    created_at:     str   = field(default_factory=lambda: datetime.now().isoformat())
    resolved_at:    Optional[str] = None
    patch_id:       Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Patch ─────────────────────────────────────────────────────────────────

class PatchStatus(str, Enum):
    GENERATED       = "generated"
    VALIDATED       = "validated"    # Syntax checked
    PENDING_REVIEW  = "pending_review"
    APPROVED        = "approved"
    REJECTED        = "rejected"
    COMMITTED       = "committed"
    FAILED          = "failed"


@dataclass
class CodePatch:
    """A proposed code change generated by ANVIL to fix an audit issue."""
    id:             str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    issue_id:       str   = ""
    repo_id:        str   = ""
    file_path:      str   = ""
    language:       str   = ""

    # The change
    content_before: str   = ""    # Original file content (or section)
    content_after:  str   = ""    # Patched content
    diff:           str   = ""    # Unified diff
    lines_changed:  int   = 0
    changes_summary:list  = field(default_factory=list)

    # Verification
    syntax_valid:   bool  = False
    tests_passed:   Optional[bool] = None
    self_check:     str   = ""    # AI's own assessment of the patch quality

    # Review
    status:         str   = PatchStatus.GENERATED
    requires_review:bool  = True
    reviewed_by:    Optional[str] = None
    reviewed_at:    Optional[str] = None
    review_notes:   Optional[str] = None
    rejection_reason:Optional[str] = None

    # Tracing
    ai_provider:    str   = ""
    ai_model:       str   = ""
    ai_tokens:      int   = 0
    doc_reference:  str   = ""    # Which doc section justified this patch
    created_at:     str   = field(default_factory=lambda: datetime.now().isoformat())
    committed_at:   Optional[str] = None
    commit_hash:    Optional[str] = None
    branch:         Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── Run summary ────────────────────────────────────────────────────────────

@dataclass
class AnvilRun:
    """Summary of one complete ANVIL loop execution."""
    id:             str   = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))
    started_at:     str   = field(default_factory=lambda: datetime.now().isoformat())
    completed_at:   Optional[str] = None
    repos_scanned:  int   = 0
    files_scanned:  int   = 0
    units_extracted:int   = 0
    bindings_made:  int   = 0
    issues_found:   int   = 0
    issues_by_type: dict  = field(default_factory=dict)
    patches_generated: int= 0
    patches_committed: int= 0
    patches_pending:int   = 0
    duration_seconds:float= 0.0
    errors:         list  = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
