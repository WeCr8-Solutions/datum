"""
ANVIL Module — PATCH
Stage 4: Generate targeted code fixes for audit issues.
- Only patches auto_fixable issues or those that pass severity threshold
- Generates minimal changes (not rewrites)
- Validates syntax after patching
- Self-checks the patch quality before submitting for review
- Inherits FORGE's human review gate for dangerous changes
"""

import re
import difflib
import asyncio
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger
from core.types import AuditIssue, CodePatch, CodeUnit, DocSection, PatchStatus
from core.forge_bridge import ForgeBridge

log = get_logger("patch")


@dataclass
class PatchResult:
    patches:        list  = field(default_factory=list)   # list[CodePatch]
    skipped:        list  = field(default_factory=list)   # issues skipped (why)
    failed:         list  = field(default_factory=list)
    pending_review: list  = field(default_factory=list)
    duration_ms:    int   = 0


class PatchModule:
    """
    Stage 4 of ANVIL.
    Generates minimal, targeted code patches for verified issues.
    Uses FORGE docs as the authoritative source for what the patch should do.
    """

    def __init__(self, config: dict, ai, forge: ForgeBridge,
                 state: dict, repo_path: str = "."):
        self.config    = config
        self.ai        = ai
        self.forge     = forge
        self.state     = state
        self.repo_path = Path(repo_path)
        self.patch_cfg = config.get("patch", {})
        self.always_review_patterns = self.patch_cfg.get("always_review_patterns", [])
        self.max_lines = self.patch_cfg.get("max_lines_per_patch", 50)
        self.dry_run   = config.get("scheduler", {}).get("dry_run", False)

    async def patch_issues(self, issues: list[AuditIssue],
                            unit_map: dict, section_map: dict,
                            memory=None) -> PatchResult:
        start  = datetime.now()
        result = PatchResult()

        # Filter to patchable issues
        patchable = [
            i for i in issues
            if i.auto_fixable and i.severity in ("error", "warning")
            and i.file_path  # Must have a file to patch
        ]

        log.info(f"Patch: {len(patchable)} patchable issues of {len(issues)} total")

        sem = asyncio.Semaphore(2)  # Conservative — code changes are sensitive

        async def bounded(issue):
            async with sem:
                return await self._patch_issue(issue, unit_map, section_map, memory)

        patches = await asyncio.gather(
            *[bounded(i) for i in patchable],
            return_exceptions=True
        )

        for p in patches:
            if isinstance(p, CodePatch):
                if p.status == PatchStatus.PENDING_REVIEW:
                    result.pending_review.append(p)
                elif p.status in (PatchStatus.VALIDATED, PatchStatus.APPROVED):
                    result.patches.append(p)
            elif isinstance(p, Exception):
                result.failed.append(str(p))

        result.skipped = [
            {"issue": i.title, "reason": "not auto-fixable or below threshold"}
            for i in issues if i not in patchable
        ]

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        log.info(
            f"Patch complete: {len(result.patches)} ready, "
            f"{len(result.pending_review)} pending review, "
            f"{len(result.failed)} failed"
        )
        return result

    # ── Per-issue patch ────────────────────────────────────────────────────

    async def _patch_issue(self, issue: AuditIssue, unit_map: dict,
                            section_map: dict, memory=None) -> Optional[CodePatch]:
        unit    = unit_map.get(issue.code_unit_id)
        section = section_map.get(issue.doc_section_id)

        if not unit or not issue.file_path:
            return None

        # Read current file content
        file_path = self._resolve_file(unit.repo_id, issue.file_path)
        if not file_path or not file_path.exists():
            log.warning(f"File not found for patch: {issue.file_path}")
            return None

        content = file_path.read_text(encoding="utf-8", errors="replace")

        # Check if this file always requires review
        if self._always_requires_review(issue.file_path):
            log.info(f"Held for review (policy): {issue.file_path}")
            return CodePatch(
                issue_id=issue.id,
                repo_id=unit.repo_id,
                file_path=issue.file_path,
                language=unit.language,
                content_before=content,
                content_after=content,  # No change yet — human decides
                status=PatchStatus.PENDING_REVIEW,
                requires_review=True,
                changes_summary=[f"Policy hold: {issue.title}"],
                doc_reference=section.doc_path if section else "",
            )

        if self.dry_run:
            log.info(f"DRY RUN: would patch {issue.file_path}")
            return None

        # Get memory context to avoid past mistakes
        memory_context = ""
        if memory:
            memory_context = memory.format_for_prompt(
                unit.language, unit.kind, issue.issue_type
            )

        # Generate patch
        patched_content = await self._generate_patch(
            content, issue, unit, section, memory_context
        )

        if not patched_content or patched_content.strip() == content.strip():
            log.debug(f"No change generated for {issue.file_path}")
            return None

        # Build diff
        diff = self._make_diff(content, patched_content, issue.file_path)
        lines_changed = sum(1 for l in diff.splitlines()
                            if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))

        if lines_changed > self.max_lines:
            log.warning(f"Patch too large ({lines_changed} lines) — holding for review")
            return CodePatch(
                issue_id=issue.id,
                repo_id=unit.repo_id,
                file_path=issue.file_path,
                language=unit.language,
                content_before=content,
                content_after=patched_content,
                diff=diff,
                lines_changed=lines_changed,
                status=PatchStatus.PENDING_REVIEW,
                requires_review=True,
                changes_summary=[f"Large patch ({lines_changed} lines) — review required"],
                doc_reference=section.doc_path if section else "",
            )

        # Validate syntax
        syntax_ok = await self._validate_syntax(patched_content, unit.language, file_path)

        # Self-check the patch
        self_check = await self._self_check(content, patched_content, issue, section)

        patch = CodePatch(
            issue_id=issue.id,
            repo_id=unit.repo_id,
            file_path=issue.file_path,
            language=unit.language,
            content_before=content,
            content_after=patched_content,
            diff=diff,
            lines_changed=lines_changed,
            changes_summary=self._summarize_diff(diff),
            syntax_valid=syntax_ok,
            self_check=self_check,
            status=PatchStatus.VALIDATED if syntax_ok else PatchStatus.PENDING_REVIEW,
            requires_review=not syntax_ok or issue.severity == "error",
            doc_reference=section.doc_path if section else "",
        )

        if not syntax_ok:
            log.warning(f"Syntax invalid after patch — holding for review: {issue.file_path}")
            patch.status = PatchStatus.PENDING_REVIEW

        return patch

    # ── AI patch generation ────────────────────────────────────────────────

    async def _generate_patch(self, content: str, issue: AuditIssue,
                               unit: CodeUnit, section: Optional[DocSection],
                               memory_context: str) -> str:
        doc_context = ""
        if section:
            doc_context = f"""
AUTHORITATIVE SPECIFICATION (from FORGE-verified documentation):
Document: {section.doc_path}
Section: {section.section_title}
---
{section.content[:600]}
---
"""

        prompt = f"""You are fixing a specific bug in code where it contradicts its documentation.

ISSUE:
Type: {issue.issue_type}
Severity: {issue.severity}
Problem: {issue.description}
Documentation says: {issue.doc_says}
Code currently does: {issue.code_does}
Suggested fix: {issue.suggested_fix}
{doc_context}
{f"AGENT MEMORY (learned from past fixes):{chr(10)}{memory_context}" if memory_context else ""}

FILE TO PATCH ({unit.language}):
Path: {issue.file_path}
---
{content}
---

Make the MINIMAL change required to fix this specific issue.
Rules:
1. Return the COMPLETE file content with only the necessary change
2. Do not change anything unrelated to this issue
3. Do not add comments explaining what you changed
4. Do not change variable names, function signatures, or formatting unless required
5. If the fix requires adding a value, use the exact value from the documentation
6. If unsure, make NO change and return the original file unchanged

Return only the corrected file content:"""

        resp = await self.ai.complete(
            prompt,
            model_role="reasoning",
            task="complex_patch",
            max_tokens=4000,
            temperature=0.05,  # Extremely conservative for code changes
        )

        text = resp.text.strip()
        # Strip any markdown code fences
        for fence in ["```python", "```javascript", "```typescript",
                       "```sql", "```go", "```rust", "```java", "```"]:
            if text.startswith(fence):
                text = text[len(fence):].lstrip("\n")
        if text.endswith("```"):
            text = text[:-3].rstrip()

        return text

    # ── Self-check ────────────────────────────────────────────────────────

    async def _self_check(self, original: str, patched: str,
                           issue: AuditIssue, section: Optional[DocSection]) -> str:
        diff = self._make_diff(original, patched, "file")
        if not diff:
            return "No changes made"

        prompt = f"""Review this code patch. Does it correctly fix the issue without introducing problems?

ISSUE TO FIX: {issue.description}
DOC SAYS: {issue.doc_says}

PATCH (unified diff):
{diff[:1500]}

Answer in one sentence: Is this patch correct, safe, and minimal?
If there's a problem, say what it is. If it looks good, say so."""

        try:
            resp = await self.ai.complete(
                prompt, model_role="fast", task="general",
                max_tokens=100, temperature=0.1
            )
            return resp.text.strip()
        except Exception:
            return "Self-check unavailable"

    # ── Syntax validation ─────────────────────────────────────────────────

    async def _validate_syntax(self, content: str, language: str,
                                file_path: Path) -> bool:
        if not self.patch_cfg.get("validate_syntax", True):
            return True

        try:
            if language == "python":
                import ast
                ast.parse(content)
                return True
            elif language in ("javascript", "typescript"):
                # Try node --check if available
                with tempfile.NamedTemporaryFile(
                    suffix=".js", mode="w", delete=False
                ) as f:
                    f.write(content)
                    tmp = f.name
                result = subprocess.run(
                    ["node", "--check", tmp],
                    capture_output=True, timeout=10
                )
                Path(tmp).unlink(missing_ok=True)
                return result.returncode == 0
            elif language == "sql":
                # Basic SQL syntax check — look for unmatched quotes/parens
                return content.count("(") == content.count(")")
            else:
                return True  # Can't validate — assume ok
        except Exception as e:
            log.debug(f"Syntax validation error (assuming invalid): {e}")
            return False

    # ── Utilities ──────────────────────────────────────────────────────────

    def _resolve_file(self, repo_id: str, file_path: str) -> Optional[Path]:
        for repo_cfg in self.config.get("repositories", []):
            if repo_cfg.get("id") == repo_id:
                return Path(repo_cfg["path"]) / file_path
        return self.repo_path / file_path

    def _always_requires_review(self, file_path: str) -> bool:
        import fnmatch
        for pattern in self.always_review_patterns:
            if fnmatch.fnmatch(file_path.lower(), pattern.lower()):
                return True
        return False

    def _make_diff(self, before: str, after: str, path: str) -> str:
        return "".join(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        ))

    def _summarize_diff(self, diff: str) -> list[str]:
        added   = [l[1:].strip() for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:].strip() for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        changes = []
        if added:
            changes.append(f"Added: {added[0][:60]}")
        if removed:
            changes.append(f"Removed: {removed[0][:60]}")
        if len(added) > 1:
            changes.append(f"...{len(added)-1} more additions")
        return changes[:3]
