"""
FORGE Module — REPAIR
Stage 3: AI rewrites only what fails verification.
Tracks every change with a diff. Verifies the repair improved the score.
Never silently overwrites — always diffs first.
"""

import re
import difflib
import asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import yaml

from core.logger import get_logger, StateManager
from core.ai_client import AIClient
from modules.verify import VerifyModule, VerificationResult

log = get_logger("repair")


@dataclass
class RepairResult:
    path:           str
    success:        bool  = False
    score_before:   int   = 0
    score_after:    int   = 0
    improved:       bool  = False
    changes_made:   list  = field(default_factory=list)
    diff:           str   = ""
    content_before: str   = ""
    content_after:  str   = ""
    attempt:        int   = 1
    error:          str   = ""
    duration_ms:    int   = 0
    skipped:        bool  = False


class RepairModule:
    """
    Stage 3 of the FORGE loop.
    - Builds a targeted repair prompt based on verification failures
    - Sends to AI (Ollama primary)
    - Verifies the repair actually improved the score
    - Falls back if repair made things worse
    - Tracks a full diff of every change
    """

    def __init__(self, config: dict, state: StateManager,
                 ai: AIClient, verifier: VerifyModule, repo_path: str = "."):
        self.cfg       = config
        self.state     = state
        self.ai        = ai
        self.verifier  = verifier
        self.repo_path = Path(repo_path)
        self.vcfg      = config.get("verification", {})
        self.max_attempts = self.vcfg.get("max_repair_attempts", 3)
        self.domains   = self.verifier.domains

    async def repair_document(self, rec, vr: VerificationResult,
                               content: str) -> RepairResult:
        start  = datetime.now()
        result = RepairResult(path=rec.path, score_before=vr.quality_score)

        if self.cfg.get("loop", {}).get("dry_run"):
            log.info(f"  DRY RUN — skipping repair of {rec.path}")
            result.skipped = True
            return result

        log.info(f"Repairing: {rec.path} (score: {vr.quality_score})")

        # Check attempt limit
        if rec.repair_attempts >= self.max_attempts:
            log.warning(f"  Max repair attempts ({self.max_attempts}) reached — skipping")
            result.skipped = True
            result.error   = f"Max attempts ({self.max_attempts}) reached"
            return result

        rec.repair_attempts += 1
        result.attempt = rec.repair_attempts

        try:
            # 1. Build repair prompt
            repaired_content = await self._repair(content, vr)

            if not repaired_content or repaired_content.strip() == content.strip():
                log.info("  No changes produced by AI — skipping")
                result.skipped = True
                return result

            # 2. Generate diff
            result.diff           = self._diff(content, repaired_content, rec.path)
            result.content_before = content
            result.content_after  = repaired_content
            result.changes_made   = self._summarize_changes(result.diff)

            # 3. Self-verify: score the repaired content
            rec_temp = type(rec)(rec.path)
            rec_temp.domain   = vr.domain
            rec_temp.doc_type = vr.doc_type
            vr_after = await self.verifier.verify_document(rec_temp, repaired_content)

            result.score_after = vr_after.quality_score
            result.improved    = vr_after.quality_score >= vr.quality_score

            if not result.improved and vr_after.quality_score < vr.quality_score - 5:
                log.warning(f"  Repair made things worse "
                            f"({vr.quality_score} → {vr_after.quality_score}) — reverting")
                result.success = False
                result.error   = "Score decreased after repair — reverted"
                return result

            # 4. Write to file
            file_path = self.repo_path / rec.path
            if not file_path.exists():
                file_path = Path(rec.path)

            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(repaired_content, encoding="utf-8")

            # 5. Update record
            rec.quality_score  = vr_after.quality_score
            rec.last_repaired  = datetime.now().isoformat()
            rec.status         = "repaired"
            rec.repair_history.append({
                "date":         datetime.now().isoformat(),
                "score_before": vr.quality_score,
                "score_after":  vr_after.quality_score,
                "attempt":      result.attempt,
                "changes":      result.changes_made[:5],
            })

            result.success = True

            log.info(f"  ✅ Repaired: {vr.quality_score} → {vr_after.quality_score} "
                     f"({len(result.changes_made)} changes)")

        except Exception as e:
            log.error(f"  Repair failed: {e}")
            result.error   = str(e)
            result.success = False

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return result

    # ── AI repair ─────────────────────────────────────────────────────────

    async def _repair(self, content: str, vr: VerificationResult) -> str:
        domain_cfg = self.domains.get(vr.domain, self.domains.get("general", {}))

        # Build targeted system prompt
        base_prompt  = domain_cfg.get("repair_prompts", {}).get("base", "")
        type_prompt  = domain_cfg.get("repair_prompts", {}).get(
            f"{vr.doc_type}_repair", ""
        )
        system = "\n\n".join(filter(None, [base_prompt, type_prompt]))
        if not system:
            system = "You are an expert technical writer. Improve this document."

        # Prioritize what to fix
        errors   = [f for f in vr.failures if f.get("severity") == "error"]
        warnings = vr.warnings[:5]

        fix_list = "\n".join(
            f"- [MUST FIX - {f['severity'].upper()}] {f['message']}" for f in errors
        ) + "\n" + "\n".join(
            f"- [IMPROVE] {f['message']}" for f in warnings
        )

        ai_improvements = ""
        if isinstance(vr.ai_assessment, dict):
            improvements = vr.ai_assessment.get("top_improvements", [])
            if improvements:
                ai_improvements = "\nAI-IDENTIFIED IMPROVEMENTS:\n" + "\n".join(
                    f"- {i}" for i in improvements
                )

        web_context = f"\n\nVERIFIED REFERENCE INFORMATION:\n{vr.web_context}" \
                      if vr.web_context else ""

        prompt = f"""Improve the following {vr.doc_type} document from the {vr.domain} domain.

ISSUES TO ADDRESS:
{fix_list}
{ai_improvements}{web_context}

CRITICAL RULES:
1. Return ONLY the complete improved document — no commentary, no explanation
2. Preserve ALL existing correct content — only fix what's broken
3. Do NOT change document numbers, revision letters, or approval blocks
4. Mark any specific values you cannot verify as [VERIFY WITH ENGINEERING]
5. Do NOT invent specifications, tolerances, speeds, feeds, or chemical data
6. If a section is missing, add it with appropriate placeholder content

DOCUMENT TO IMPROVE:
---
{content}
---

Return the complete improved document:"""

        response = await self.ai.complete(
            prompt,
            system=system,
            model_role="reasoning",
            task="repair",
            max_tokens=4000,
            temperature=0.1,   # Very low temp for doc repair — be conservative
        )

        # Strip any AI preamble
        text = response.text.strip()
        for prefix in ["Here is the improved", "Here's the improved", "Improved document:"]:
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
                if text.startswith(":"):
                    text = text[1:].strip()

        return text

    # ── Diff generation ───────────────────────────────────────────────────

    def _diff(self, before: str, after: str, path: str) -> str:
        before_lines = before.splitlines(keepends=True)
        after_lines  = after.splitlines(keepends=True)

        diff = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=self.cfg.get("git", {}).get("diff_context_lines", 3),
        )
        return "".join(diff)

    def _summarize_changes(self, diff: str) -> list[str]:
        """Extract a human-readable list of changes from a unified diff."""
        additions    = []
        deletions    = []
        for line in diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions.append(line[1:].strip())
            elif line.startswith("-") and not line.startswith("---"):
                deletions.append(line[1:].strip())

        changes = []
        if len(additions) > len(deletions):
            changes.append(f"Added {len(additions) - len(deletions)} lines of content")
        elif len(deletions) > len(additions):
            changes.append(f"Removed {len(deletions) - len(additions)} lines")

        if additions:
            sample = additions[0][:60]
            changes.append(f"Added: \"{sample}\"" + ("..." if len(additions[0]) > 60 else ""))

        return changes[:5]
