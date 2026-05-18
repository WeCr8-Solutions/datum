"""
ANVIL Core — FORGE Bridge
===========================
Reads FORGE's verified documents, quality scores, and decision ledger.
ANVIL treats FORGE-verified docs as the authoritative specification.

Key principle: ANVIL never uses a doc FORGE scored below the threshold.
A low-quality doc is an unreliable spec — using it would cause ANVIL
to generate incorrect patches. FORGE quality gates ANVIL's inputs.
"""

import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional, Iterator
from dataclasses import dataclass

from .logger import get_logger
from .types import DocSection

log = get_logger("forge_bridge")


@dataclass
class ForgeDocRecord:
    """A document as known to FORGE — with quality metadata."""
    path:           str
    domain:         str
    doc_type:       str
    quality_score:  int
    last_verified:  str
    last_repaired:  str
    status:         str
    itar_sensitive: bool
    chunk_count:    int = 0
    forge_verified: bool = False


class ForgeBridge:
    """
    Read-only interface to FORGE.
    ANVIL reads from FORGE but never writes to it directly.
    (ANVIL writes to the shared ledger, which FORGE can also read.)
    """

    def __init__(self, config: dict):
        forge_cfg = config.get("forge", {})
        self.docs_path    = Path(forge_cfg.get("docs_path", "../forge/docs"))
        self.state_file   = Path(forge_cfg.get("state_file", "../forge/forge_state.json"))
        self.ledger_file  = Path(forge_cfg.get("ledger_file", "../forge/logs/decisions.ndjson"))
        self.min_quality  = forge_cfg.get("min_doc_quality_score", 70)
        self._state: dict = {}
        self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                self._state = json.loads(self.state_file.read_text())
                docs = self._state.get("documents", {})
                log.info(f"FORGE bridge: {len(docs)} documents in FORGE state")
            except Exception as e:
                log.warning(f"FORGE state load failed: {e}")

    def refresh(self):
        """Reload FORGE state (call at start of each run)."""
        self._load_state()

    # ── Document access ───────────────────────────────────────────────────

    def get_verified_docs(self, min_score: int = None) -> list[ForgeDocRecord]:
        """Return all docs FORGE has verified above the quality threshold."""
        threshold = min_score or self.min_quality
        docs = []
        for path, data in self._state.get("documents", {}).items():
            score = data.get("quality_score") or 0
            if score >= threshold and data.get("status") in ("verified", "repaired"):
                docs.append(ForgeDocRecord(
                    path=path,
                    domain=data.get("domain", "general"),
                    doc_type=data.get("doc_type", "document"),
                    quality_score=score,
                    last_verified=data.get("last_verified", ""),
                    last_repaired=data.get("last_repaired", ""),
                    status=data.get("status", "unknown"),
                    itar_sensitive=data.get("itar_sensitive", False),
                    forge_verified=True,
                ))
        docs.sort(key=lambda d: d.quality_score, reverse=True)
        log.info(f"FORGE bridge: {len(docs)} docs above quality threshold {threshold}")
        return docs

    def read_doc(self, forge_record: ForgeDocRecord) -> Optional[str]:
        """Read the actual content of a FORGE-verified document."""
        # Try direct path first
        candidates = [
            Path(forge_record.path),
            self.docs_path / forge_record.path,
            self.docs_path.parent / forge_record.path,
        ]
        for p in candidates:
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    log.warning(f"Cannot read {p}: {e}")
        log.warning(f"Doc not found on disk: {forge_record.path}")
        return None

    def was_updated_since(self, forge_record: ForgeDocRecord,
                           since_iso: str) -> bool:
        """Check if FORGE updated this doc more recently than a given timestamp."""
        if not since_iso:
            return True
        try:
            doc_time  = datetime.fromisoformat(forge_record.last_verified or "2000-01-01")
            check_time= datetime.fromisoformat(since_iso)
            return doc_time > check_time
        except Exception:
            return True

    # ── Section extraction ────────────────────────────────────────────────

    def extract_sections(self, content: str, forge_record: ForgeDocRecord,
                          min_length: int = 80) -> list[DocSection]:
        """
        Split a document into meaningful sections for binding to code.
        Uses markdown headers as section boundaries; falls back to paragraphs.
        """
        sections = []
        lines    = content.splitlines()

        # Try markdown header-based splitting
        header_sections = self._split_by_headers(lines, forge_record)
        if header_sections:
            sections = [s for s in header_sections if len(s.content) >= min_length]

        # Fallback: paragraph-based splitting
        if not sections:
            para_sections = self._split_by_paragraphs(content, forge_record)
            sections = [s for s in para_sections if len(s.content) >= min_length]

        # Extract specs from each section
        for section in sections:
            section.specs = self._extract_specs(section.content)

        return sections

    def _split_by_headers(self, lines: list[str],
                           rec: ForgeDocRecord) -> list[DocSection]:
        sections = []
        current_title = "Introduction"
        current_lines = []
        current_start = 0

        for i, line in enumerate(lines):
            if re.match(r'^#{1,4}\s+', line):
                # Save previous section
                if current_lines:
                    sections.append(DocSection(
                        doc_path=rec.path,
                        doc_domain=rec.domain,
                        doc_type=rec.doc_type,
                        doc_quality=rec.quality_score,
                        section_title=current_title,
                        content="\n".join(current_lines).strip(),
                        line_start=current_start,
                        line_end=i - 1,
                        forge_verified=rec.forge_verified,
                        forge_score=rec.quality_score,
                        last_forge_run=rec.last_verified,
                    ))
                current_title = re.sub(r'^#+\s+', '', line).strip()
                current_lines = []
                current_start = i
            else:
                current_lines.append(line)

        if current_lines:
            sections.append(DocSection(
                doc_path=rec.path,
                doc_domain=rec.domain,
                doc_type=rec.doc_type,
                doc_quality=rec.quality_score,
                section_title=current_title,
                content="\n".join(current_lines).strip(),
                line_start=current_start,
                line_end=len(lines) - 1,
                forge_verified=rec.forge_verified,
                forge_score=rec.quality_score,
                last_forge_run=rec.last_verified,
            ))

        return sections

    def _split_by_paragraphs(self, content: str,
                              rec: ForgeDocRecord) -> list[DocSection]:
        paragraphs = re.split(r'\n\n+', content)
        sections   = []
        line_num   = 0

        for para in paragraphs:
            lines = para.count("\n") + 1
            sections.append(DocSection(
                doc_path=rec.path,
                doc_domain=rec.domain,
                doc_type=rec.doc_type,
                doc_quality=rec.quality_score,
                section_title=para.splitlines()[0][:60] if para else "",
                content=para.strip(),
                line_start=line_num,
                line_end=line_num + lines,
                forge_verified=rec.forge_verified,
                forge_score=rec.quality_score,
            ))
            line_num += lines + 2

        return sections

    def _extract_specs(self, content: str) -> list[dict]:
        """
        Extract specific, verifiable specifications from doc text.
        These become the ground truth ANVIL checks code against.
        """
        specs = []

        # Timeout/duration specs
        for m in re.finditer(
            r'(?:timeout|within|maximum|deadline|interval|delay|wait).*?'
            r'(\d+)\s*(second|minute|hour|hour|day|ms|millisecond)',
            content, re.IGNORECASE
        ):
            specs.append({
                "type": "duration",
                "value": int(m.group(1)),
                "unit": m.group(2).lower(),
                "context": m.group(0)[:100],
            })

        # Numeric limits
        for m in re.finditer(
            r'(?:minimum|maximum|max|min|limit|threshold|at least|no more than|up to)'
            r'\s+(\d+(?:\.\d+)?)\s*(%|items?|records?|users?|requests?|MB|GB|KB)?',
            content, re.IGNORECASE
        ):
            specs.append({
                "type": "numeric_limit",
                "value": float(m.group(1)),
                "unit": (m.group(2) or "").lower(),
                "context": m.group(0)[:100],
            })

        # Status codes / response codes
        for m in re.finditer(
            r'(?:return|respond|status|code|HTTP)\s+(\d{3})\b',
            content, re.IGNORECASE
        ):
            specs.append({
                "type": "status_code",
                "value": int(m.group(1)),
                "unit": "http_status",
                "context": m.group(0)[:100],
            })

        # Required fields / attributes
        for m in re.finditer(
            r'(?:must|shall|required|mandatory).*?(?:include|contain|have|provide)\s+'
            r'(?:a |an |the )?(["\']?)(\w[\w_-]{2,40})\1',
            content, re.IGNORECASE
        ):
            specs.append({
                "type": "required_field",
                "value": m.group(2),
                "unit": "field_name",
                "context": m.group(0)[:100],
            })

        return specs[:20]  # Cap to avoid noise

    # ── FORGE ledger access ───────────────────────────────────────────────

    def get_recent_forge_decisions(self, last_n: int = 200) -> list[dict]:
        """Read recent FORGE decisions — used to detect when docs changed."""
        if not self.ledger_file.exists():
            return []
        try:
            lines = self.ledger_file.read_text().strip().split("\n")
            result = []
            for line in lines[-last_n:]:
                try:
                    result.append(json.loads(line))
                except Exception:
                    pass
            return result
        except Exception as e:
            log.warning(f"FORGE ledger read failed: {e}")
            return []

    def get_docs_changed_since(self, since_iso: str) -> list[str]:
        """Return paths of docs FORGE repaired or verified after a given time."""
        decisions = self.get_recent_forge_decisions()
        changed   = set()
        for d in decisions:
            if d.get("decision_type") in ("repair_accepted", "passed_verification"):
                ts = d.get("timestamp", "")
                if ts > since_iso:
                    path = d.get("doc_path", "")
                    if path:
                        changed.add(path)
        return list(changed)

    @property
    def avg_forge_quality(self) -> float:
        return self._state.get("stats", {}).get("avg_quality", 0.0)

    @property
    def forge_total_runs(self) -> int:
        return self._state.get("stats", {}).get("total_runs", 0)
