"""
ANVIL Module — AUDIT
Stage 3: Compare code units against their bound doc sections.
Detects three types of issues:
  1. CONTRADICTION — code does X, doc says Y
  2. MISSING_IMPL  — doc specifies feature, no code implements it
  3. DRIFT         — doc updated since code was last verified

This is the core analytical engine. Uses AI for contradiction detection,
spec-matching for numeric/timeout issues (no AI needed — deterministic).
"""

import re
import asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger
from core.types import (
    CodeUnit, DocSection, Binding, AuditIssue,
    IssueType, IssueSeverity
)

log = get_logger("audit")


@dataclass
class AuditResult:
    issues:         list  = field(default_factory=list)   # list[AuditIssue]
    issues_by_type: dict  = field(default_factory=dict)
    bindings_checked: int = 0
    auto_fixable:   int   = 0
    duration_ms:    int   = 0


class AuditModule:
    """
    Stage 3 of ANVIL.
    For each Binding, runs all three audit checks.
    Deterministic spec checks run first (cheap).
    AI contradiction check runs only when spec checks pass (expensive).
    """

    def __init__(self, config: dict, ai):
        self.config    = config
        self.ai        = ai
        self.audit_cfg = config.get("audit", {})
        self.weights   = self.audit_cfg.get("score_weights", {
            "contradiction": 1.0, "missing_impl": 0.6, "drift": 0.3
        })

    async def audit(self, bindings: list[Binding], lookup: dict,
                     unbound_docs: list[DocSection]) -> AuditResult:
        start  = datetime.now()
        result = AuditResult()

        log.info(f"Auditing {len(bindings)} bindings + {len(unbound_docs)} unbound docs")

        unit_map    = lookup.get("units", {})
        section_map = lookup.get("sections", {})

        # 1. Audit each binding
        tasks = []
        for binding in bindings:
            unit    = unit_map.get(binding.code_unit_id)
            section = section_map.get(binding.doc_section_id)
            if unit and section:
                tasks.append(self._audit_binding(binding, unit, section))

        # Run in parallel (bounded)
        sem = asyncio.Semaphore(4)
        async def bounded(coro):
            async with sem:
                return await coro

        all_issues = []
        for batch in [tasks[i:i+8] for i in range(0, len(tasks), 8)]:
            results = await asyncio.gather(*[bounded(t) for t in batch], return_exceptions=True)
            for r in results:
                if isinstance(r, list):
                    all_issues.extend(r)
                elif isinstance(r, Exception):
                    log.warning(f"Audit task error: {r}")

        result.bindings_checked = len(bindings)

        # 2. Check for missing implementations (doc sections with no code binding)
        for section in unbound_docs:
            missing_issues = self._check_missing_impl(section)
            all_issues.extend(missing_issues)

        result.issues = all_issues
        result.auto_fixable = sum(1 for i in all_issues if i.auto_fixable)
        result.issues_by_type = {}
        for issue in all_issues:
            result.issues_by_type[issue.issue_type] = result.issues_by_type.get(issue.issue_type, 0) + 1

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)

        log.info(
            f"Audit complete: {len(all_issues)} issues found "
            f"({result.auto_fixable} auto-fixable) — "
            f"{result.issues_by_type}"
        )
        return result

    # ── Per-binding audit ──────────────────────────────────────────────────

    async def _audit_binding(self, binding: Binding, unit: CodeUnit,
                               section: DocSection) -> list[AuditIssue]:
        issues = []

        # A. Drift check (deterministic — free)
        if binding.is_stale:
            issues.append(AuditIssue(
                repo_id=unit.repo_id,
                issue_type=IssueType.DRIFT,
                severity=IssueSeverity.INFO,
                file_path=unit.file_path,
                line_start=unit.line_start,
                line_end=unit.line_end,
                code_unit_id=unit.id,
                doc_section_id=section.id,
                binding_id=binding.id,
                title=f"Doc updated — code not re-verified: {unit.name}",
                description=(
                    f"'{section.section_title}' was updated by FORGE after this code "
                    f"was last verified. Review for potential drift."
                ),
                doc_says=section.content[:200],
                code_does=unit.signature or unit.body[:200],
                auto_fixable=False,
                suggested_fix="Re-run audit after reviewing code against updated doc",
            ))

        # B. Spec violation checks (deterministic — free)
        spec_issues = self._check_specs(unit, section, binding)
        issues.extend(spec_issues)

        # C. AI contradiction check (only if no simple spec violations found)
        if not spec_issues and len(unit.body) > 50 and len(section.content) > 80:
            ai_issues = await self._ai_contradiction_check(unit, section, binding)
            issues.extend(ai_issues)

        return issues

    # ── Spec violation checks (deterministic) ────────────────────────────

    def _check_specs(self, unit: CodeUnit, section: DocSection,
                      binding: Binding) -> list[AuditIssue]:
        issues = []
        code   = unit.body + " " + unit.docstring

        for spec in section.specs:
            stype = spec.get("type")
            value = spec.get("value")
            unit_name = spec.get("unit", "")
            context = spec.get("context", "")

            if stype == "duration":
                issue = self._check_duration_spec(
                    code, value, unit_name, context, unit, section, binding
                )
                if issue:
                    issues.append(issue)

            elif stype == "numeric_limit":
                issue = self._check_numeric_spec(
                    code, value, unit_name, context, unit, section, binding
                )
                if issue:
                    issues.append(issue)

            elif stype == "status_code":
                issue = self._check_status_code(
                    code, value, context, unit, section, binding
                )
                if issue:
                    issues.append(issue)

            elif stype == "required_field":
                issue = self._check_required_field(
                    code, str(value), context, unit, section, binding
                )
                if issue:
                    issues.append(issue)

        return issues

    def _check_duration_spec(self, code: str, value: float, unit_str: str,
                              context: str, unit: CodeUnit,
                              section: DocSection, binding: Binding) -> Optional[AuditIssue]:
        # Convert to seconds for comparison
        multipliers = {
            "second": 1, "seconds": 1, "minute": 60, "minutes": 60,
            "hour": 3600, "hours": 3600, "day": 86400, "days": 86400,
            "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
        }
        spec_seconds = value * multipliers.get(unit_str.lower(), 1)

        # Find timeout/duration values in code
        for m in re.finditer(
            r'(?:timeout|delay|wait|sleep|interval|expires?\s*in|ttl)\s*[=:]\s*(\d+(?:\.\d+)?)',
            code, re.IGNORECASE
        ):
            code_val = float(m.group(1))
            # Heuristic: if code value looks like ms but spec is in seconds
            if code_val > spec_seconds * 100:
                code_val /= 1000  # Probably milliseconds

            tolerance = spec_seconds * 0.1 + 1  # 10% tolerance
            if abs(code_val - spec_seconds) > tolerance:
                return AuditIssue(
                    repo_id=unit.repo_id,
                    issue_type=IssueType.CONTRADICTION,
                    severity=IssueSeverity.ERROR,
                    file_path=unit.file_path,
                    line_start=unit.line_start,
                    code_unit_id=unit.id,
                    doc_section_id=section.id,
                    binding_id=binding.id,
                    title=f"Timeout mismatch in {unit.name}",
                    description=f"Doc specifies {value}{unit_str} ({spec_seconds}s), code has {code_val}s",
                    doc_says=context,
                    code_does=m.group(0),
                    evidence=[f"Doc: {context}", f"Code: {m.group(0)}"],
                    auto_fixable=True,
                    suggested_fix=f"Change timeout value to {spec_seconds}",
                    patch_hint=f"Replace {m.group(0)} with timeout={int(spec_seconds)}",
                )
        return None

    def _check_numeric_spec(self, code: str, value: float, unit_str: str,
                             context: str, unit: CodeUnit, section: DocSection,
                             binding: Binding) -> Optional[AuditIssue]:
        # Look for the same numeric value or close variants in code
        # Only flag if we find a clearly different value in a relevant context
        pattern = rf'(?:max|min|limit|threshold|maximum|minimum)\s*[=:]\s*(\d+(?:\.\d+)?)'
        for m in re.finditer(pattern, code, re.IGNORECASE):
            code_val = float(m.group(1))
            if abs(code_val - value) > value * 0.05 and abs(code_val - value) > 1:
                return AuditIssue(
                    repo_id=unit.repo_id,
                    issue_type=IssueType.CONTRADICTION,
                    severity=IssueSeverity.WARNING,
                    file_path=unit.file_path,
                    line_start=unit.line_start,
                    code_unit_id=unit.id,
                    doc_section_id=section.id,
                    binding_id=binding.id,
                    title=f"Numeric limit mismatch in {unit.name}",
                    description=f"Doc says {value}{unit_str}, code has {code_val}",
                    doc_says=context,
                    code_does=m.group(0),
                    auto_fixable=True,
                    suggested_fix=f"Update value to {value}",
                    patch_hint=f"Change {m.group(0)} to match doc spec: {value}{unit_str}",
                )
        return None

    def _check_status_code(self, code: str, value: int, context: str,
                            unit: CodeUnit, section: DocSection,
                            binding: Binding) -> Optional[AuditIssue]:
        # Look for HTTP status codes in code
        code_codes = set(int(m) for m in re.findall(r'\b([1-5]\d{2})\b', code))
        if code_codes and value not in code_codes:
            # Only flag if there's a clearly wrong code (same category, wrong number)
            wrong = [c for c in code_codes if c // 100 == value // 100 and c != value]
            if wrong:
                return AuditIssue(
                    repo_id=unit.repo_id,
                    issue_type=IssueType.CONTRADICTION,
                    severity=IssueSeverity.WARNING,
                    file_path=unit.file_path,
                    line_start=unit.line_start,
                    code_unit_id=unit.id,
                    doc_section_id=section.id,
                    binding_id=binding.id,
                    title=f"HTTP status code mismatch in {unit.name}",
                    description=f"Doc says HTTP {value}, code uses {wrong[0]}",
                    doc_says=context,
                    code_does=f"Status {wrong[0]}",
                    auto_fixable=True,
                    suggested_fix=f"Change status code {wrong[0]} to {value}",
                )
        return None

    def _check_required_field(self, code: str, field_name: str, context: str,
                               unit: CodeUnit, section: DocSection,
                               binding: Binding) -> Optional[AuditIssue]:
        # Check if the required field/attribute is referenced in code
        pattern = rf'\b{re.escape(field_name)}\b'
        if not re.search(pattern, code, re.IGNORECASE):
            return AuditIssue(
                repo_id=unit.repo_id,
                issue_type=IssueType.MISSING_IMPL,
                severity=IssueSeverity.WARNING,
                file_path=unit.file_path,
                line_start=unit.line_start,
                code_unit_id=unit.id,
                doc_section_id=section.id,
                binding_id=binding.id,
                title=f"Required field '{field_name}' not found in {unit.name}",
                description=f"Doc requires '{field_name}' but it's not referenced in this code unit",
                doc_says=context,
                code_does=f"No reference to '{field_name}'",
                auto_fixable=False,
                suggested_fix=f"Add handling for required field '{field_name}'",
            )
        return None

    # ── AI contradiction check ────────────────────────────────────────────

    async def _ai_contradiction_check(self, unit: CodeUnit, section: DocSection,
                                       binding: Binding) -> list[AuditIssue]:
        prompt = f"""You are auditing code against its specification document.

SPECIFICATION (from verified documentation — this is the ground truth):
Section: {section.section_title}
{section.content[:600]}

CODE UNIT ({unit.language} {unit.kind}):
Name: {unit.name}
File: {unit.file_path}
{unit.full_text[:800]}

Check for these issues:
1. CONTRADICTION: Does the code do something that directly contradicts the specification?
   (e.g., doc says 30-second timeout, code has 60; doc says return 201, code returns 200)
2. MISSING: Does the spec mention a requirement that the code clearly doesn't implement?
   (Only flag things that are clearly missing, not things that might be elsewhere)

Be conservative — only report real, specific, verifiable issues.
Do NOT flag:
- Style differences
- Implementation choices that aren't specified in the doc
- Vague language that could mean many things
- Things that might be handled in another file

Respond as JSON:
{{
  "issues": [
    {{
      "type": "contradiction|missing_impl",
      "severity": "error|warning",
      "title": "short title",
      "doc_says": "exact quote from spec",
      "code_does": "what the code actually does",
      "auto_fixable": true|false,
      "suggested_fix": "specific fix instruction"
    }}
  ]
}}
Empty array if no real issues found."""

        try:
            result = await self.ai.complete_json(
                prompt,
                model_role="reasoning",
                task="audit",
                cache_key=f"audit_{unit.id[:8]}_{section.id[:8]}",
            )

            issues = []
            for item in result.get("issues", []):
                issues.append(AuditIssue(
                    repo_id=unit.repo_id,
                    issue_type=item.get("type", IssueType.CONTRADICTION),
                    severity=item.get("severity", IssueSeverity.WARNING),
                    file_path=unit.file_path,
                    line_start=unit.line_start,
                    line_end=unit.line_end,
                    code_unit_id=unit.id,
                    doc_section_id=section.id,
                    binding_id=binding.id,
                    title=item.get("title", ""),
                    description=item.get("title", ""),
                    doc_says=item.get("doc_says", ""),
                    code_does=item.get("code_does", ""),
                    evidence=[item.get("doc_says", ""), item.get("code_does", "")],
                    auto_fixable=item.get("auto_fixable", False),
                    suggested_fix=item.get("suggested_fix", ""),
                    doc_reference=section.doc_path,
                ))
            return issues

        except Exception as e:
            log.debug(f"AI audit failed (non-fatal): {e}")
            return []

    # ── Missing implementation check ──────────────────────────────────────

    def _check_missing_impl(self, section: DocSection) -> list[AuditIssue]:
        """Flag doc sections that describe functionality with no code binding."""
        # Only flag sections that describe behavioral requirements
        indicators = [
            "must", "shall", "will", "should", "required",
            "API", "endpoint", "function", "method", "process", "handle"
        ]
        has_requirement = any(
            re.search(rf'\b{kw}\b', section.content, re.IGNORECASE)
            for kw in indicators
        )
        if not has_requirement or len(section.content) < 100:
            return []

        return [AuditIssue(
            issue_type=IssueType.MISSING_IMPL,
            severity=IssueSeverity.WARNING,
            file_path="",
            doc_section_id=section.id,
            title=f"No code found implementing: {section.section_title}",
            description=(
                f"Doc section '{section.section_title}' describes a requirement "
                f"but no code unit was bound to it."
            ),
            doc_says=section.content[:300],
            code_does="(no binding found)",
            auto_fixable=False,
            suggested_fix="Implement the described functionality or link to existing code",
        )]
