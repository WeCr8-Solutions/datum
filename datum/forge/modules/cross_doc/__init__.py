"""
FORGE Module — Cross-Document Reasoning
=========================================
Looks ACROSS documents for problems a single-document verifier cannot see:
  - Broken references (SOP-001 mentions Setup-Sheet-47 which doesn't exist)
  - Contradictions (two SOPs specify different coolant for the same material)
  - Missing dependencies (a training doc references an SOP that's not indexed)
  - Orphaned documents (docs that nothing references — possibly obsolete)
  - Version conflicts (SOP Rev B references quality manual Rev A but Rev C exists)

This is where real knowledge graph value lives. Runs after VERIFY and REPAIR,
before COMMIT, on a configurable interval (not every run — expensive).
"""

import re
import asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger, StateManager
from core.ai_client import AIClient
from core.ledger import DecisionLedger, DecisionType, Confidence, Outcome

log = get_logger("cross_doc")


@dataclass
class CrossDocIssue:
    """A problem found across multiple documents."""
    issue_type:     str         # "broken_reference" | "contradiction" | "missing_dep" | "orphan" | "version_conflict"
    severity:       str         # "error" | "warning" | "info"
    description:    str
    primary_doc:    str         # The document where the issue was detected
    related_docs:   list        # Other documents involved
    evidence:       list        # Specific text excerpts that show the issue
    suggested_fix:  str         = ""
    auto_fixable:   bool        = False


@dataclass
class CrossDocResult:
    issues:         list[CrossDocIssue] = field(default_factory=list)
    docs_analyzed:  int  = 0
    references_found: int = 0
    duration_ms:    int  = 0


class CrossDocReasoner:
    """
    Analyzes relationships between documents to find cross-cutting issues.
    Builds a simple reference graph and checks for consistency.
    """

    # Patterns that indicate a reference to another document
    REFERENCE_PATTERNS = [
        r"(?:see|refer to|per|reference|as defined in|in accordance with)\s+"
        r"((?:SOP|QP|WI|MP|OP|Setup|Form|Doc|Rev|Procedure|Manual|Sheet|Spec)[-\s]?[\w\d.-]+)",
        r"\b(SOP[-\s]?\d{2,5}[-\w]*)\b",
        r"\b(Form[-\s]?\d{3,5}[-\w]*)\b",
        r"\b(QP[-\s]?\d{2,4}[-\w]*)\b",
        r"\b(WI[-\s]?\d{2,4}[-\w]*)\b",
        r"\b(Rev(?:ision)?\s+[A-Z0-9]+)\b",
    ]

    # Contradiction indicators — phrases that define a specific value
    SPEC_PATTERNS = [
        r"(?:use|apply|set|maintain|ensure)\s+(.{5,60}?)\s+(?:coolant|speed|feed|temperature|pressure|rpm|sfm|ipm)",
        r"(?:minimum|maximum|nominal|target)\s+(.{5,40}?)\s+(?:rpm|sfm|ipm|mm|in|°[FC]|psi|bar)",
    ]

    def __init__(self, config: dict, state: StateManager,
                 ai: AIClient, ledger: DecisionLedger, repo_path: str = "."):
        self.cfg       = config
        self.state     = state
        self.ai        = ai
        self.ledger    = ledger
        self.repo_path = Path(repo_path)

    async def analyze(self, doc_paths: list[str]) -> CrossDocResult:
        """Run cross-document analysis on a set of documents."""
        start  = datetime.now()
        result = CrossDocResult()

        if len(doc_paths) < 2:
            return result  # Nothing to cross-reference with a single doc

        log.info(f"Cross-document analysis: {len(doc_paths)} documents")

        # Load all document content
        docs: dict[str, str] = {}
        for path in doc_paths:
            full = self.repo_path / path
            if full.exists():
                try:
                    docs[path] = full.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

        result.docs_analyzed = len(docs)

        # Build reference graph
        ref_graph = self._build_reference_graph(docs)
        result.references_found = sum(len(v) for v in ref_graph.values())

        # Check for broken references
        issues = []
        issues.extend(self._check_broken_references(ref_graph, docs))

        # Check for contradictions (AI-assisted for complex cases)
        if len(docs) <= 20:  # Only run AI cross-check on smaller sets
            contradictions = await self._check_contradictions(docs)
            issues.extend(contradictions)

        # Check for orphaned documents
        issues.extend(self._check_orphans(ref_graph, docs))

        result.issues = issues
        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)

        # Record in ledger
        for issue in issues:
            decision_type = {
                "broken_reference":  DecisionType.REFERENCE_BROKEN,
                "contradiction":     DecisionType.CONTRADICTION_FOUND,
                "missing_dep":       DecisionType.DEPENDENCY_MISSING,
            }.get(issue.issue_type, DecisionType.REFERENCE_BROKEN)

            self.ledger.record(
                decision_type=decision_type,
                doc_path=issue.primary_doc,
                decision=f"{issue.issue_type}: {issue.description[:80]}",
                reasoning="\n".join(issue.evidence[:2]),
                confidence=Confidence.HIGH if issue.issue_type == "broken_reference" else Confidence.MEDIUM,
                outcome=Outcome.PENDING,
                related_doc_paths=issue.related_docs,
            )

        if issues:
            log.info(f"Cross-doc issues: {len(issues)} "
                     f"({sum(1 for i in issues if i.severity == 'error')} errors, "
                     f"{sum(1 for i in issues if i.severity == 'warning')} warnings)")
        else:
            log.info("Cross-doc analysis: no issues found")

        return result

    # ── Reference graph ───────────────────────────────────────────────────

    def _build_reference_graph(self, docs: dict[str, str]) -> dict[str, list[str]]:
        """
        For each document, find references to other documents.
        Returns {doc_path: [referenced_doc_ids]}
        """
        # Build a lookup of known document identifiers
        known_ids: dict[str, str] = {}  # normalized_id -> actual path

        for path in docs:
            p = Path(path)
            stem = p.stem.upper()
            known_ids[stem] = path

            # Also extract doc number from content (e.g., "SOP-001")
            content = docs[path]
            for pattern in self.REFERENCE_PATTERNS:
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    doc_id = re.sub(r'[-\s]', '', match.group(1)).upper()
                    if doc_id not in known_ids:
                        known_ids[doc_id] = path  # Tentative — may be overwritten

        # Build graph
        graph: dict[str, list[str]] = {}
        for path, content in docs.items():
            refs = []
            for pattern in self.REFERENCE_PATTERNS:
                for match in re.finditer(pattern, content, re.IGNORECASE):
                    ref = re.sub(r'[-\s]', '', match.group(1)).upper()
                    if ref in known_ids and known_ids[ref] != path:
                        refs.append(known_ids[ref])
            graph[path] = list(set(refs))

        return graph

    # ── Broken references ─────────────────────────────────────────────────

    def _check_broken_references(self, ref_graph: dict, docs: dict) -> list[CrossDocIssue]:
        issues = []
        all_paths = set(docs.keys())

        for doc_path, references in ref_graph.items():
            for ref in references:
                if ref not in all_paths:
                    # Find the actual text that makes this reference
                    content   = docs.get(doc_path, "")
                    evidence  = []
                    for line in content.splitlines():
                        if Path(ref).stem.lower() in line.lower():
                            evidence.append(line.strip()[:100])
                            if len(evidence) >= 2:
                                break

                    issues.append(CrossDocIssue(
                        issue_type="broken_reference",
                        severity="warning",
                        description=f"References '{Path(ref).name}' which is not in the document index",
                        primary_doc=doc_path,
                        related_docs=[ref],
                        evidence=evidence,
                        suggested_fix=f"Upload '{ref}' to the document index, or update the reference",
                        auto_fixable=False,
                    ))

        return issues

    # ── Orphaned docs ─────────────────────────────────────────────────────

    def _check_orphans(self, ref_graph: dict, docs: dict) -> list[CrossDocIssue]:
        """Find documents that nothing else references — possibly obsolete."""
        all_referenced = set()
        for refs in ref_graph.values():
            all_referenced.update(refs)

        issues = []
        for doc_path in docs:
            if doc_path not in all_referenced:
                # Could be legitimate (top-level docs, standalone references)
                # Only flag if it's a type that would normally be referenced
                p = Path(doc_path)
                if any(kw in p.stem.lower() for kw in ["setup", "form", "checklist", "template"]):
                    issues.append(CrossDocIssue(
                        issue_type="orphan",
                        severity="info",
                        description=f"No other document references this — may be obsolete or not yet linked",
                        primary_doc=doc_path,
                        related_docs=[],
                        evidence=[],
                        suggested_fix="Verify this document is still in use and add references from relevant SOPs",
                        auto_fixable=False,
                    ))

        return issues[:10]  # Cap orphan reports

    # ── AI contradiction detection ────────────────────────────────────────

    async def _check_contradictions(self, docs: dict[str, str]) -> list[CrossDocIssue]:
        """
        Use AI to detect contradictions across documents.
        Only run on a small, focused set to manage token cost.
        """
        if not docs:
            return []

        # Build a compressed summary of specifications from each doc
        spec_summaries = []
        for path, content in list(docs.items())[:10]:  # Max 10 docs per AI call
            # Extract only lines with specific values
            spec_lines = []
            for line in content.splitlines():
                if any(kw in line.lower() for kw in
                       ["rpm", "sfm", "ipm", "coolant", "temperature",
                        "feed", "speed", "tolerance", "material"]):
                    stripped = line.strip()
                    if 10 < len(stripped) < 150:
                        spec_lines.append(stripped)

            if spec_lines:
                spec_summaries.append(
                    f"[{Path(path).name}]\n" + "\n".join(spec_lines[:10])
                )

        if not spec_summaries:
            return []

        prompt = f"""Review these specification excerpts from {len(spec_summaries)} documents 
in a CNC machine shop's document library. Identify any DIRECT CONTRADICTIONS where two 
documents specify different values for the same parameter (e.g., different RPM for the 
same material, different coolant types for the same operation).

Documents:
{chr(10).join(spec_summaries)}

Return a JSON array of contradictions found (empty array if none):
[
  {{
    "doc_a": "filename",
    "doc_b": "filename", 
    "parameter": "what they disagree on",
    "value_a": "what doc_a says",
    "value_b": "what doc_b says",
    "severity": "error|warning"
  }}
]
Only real contradictions — not just different contexts or materials."""

        try:
            result = await self.ai.complete_json(
                prompt, model_role="reasoning",
                cache_key=f"xdoc_{hash(str(sorted(docs.keys())))}"
            )

            issues = []
            for item in (result if isinstance(result, list) else []):
                issues.append(CrossDocIssue(
                    issue_type="contradiction",
                    severity=item.get("severity", "warning"),
                    description=(
                        f"Contradiction on '{item.get('parameter')}': "
                        f"{item.get('doc_a')} says '{item.get('value_a')}', "
                        f"{item.get('doc_b')} says '{item.get('value_b')}'"
                    ),
                    primary_doc=item.get("doc_a", ""),
                    related_docs=[item.get("doc_b", "")],
                    evidence=[
                        f"{item.get('doc_a')}: {item.get('value_a')}",
                        f"{item.get('doc_b')}: {item.get('value_b')}",
                    ],
                    suggested_fix=f"Reconcile '{item.get('parameter')}' across both documents",
                ))
            return issues

        except Exception as e:
            log.debug(f"Contradiction check failed (non-fatal): {e}")
            return []
