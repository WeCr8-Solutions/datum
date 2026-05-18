"""
ANVIL Module — BIND
Stage 2: Map doc sections (the spec) to code units (the implementation)
using semantic embeddings. This is the bridge that makes ANVIL
doc-driven rather than just a generic linter.

A Binding says: "DocSection X is the specification for CodeUnit Y."
Everything downstream (AUDIT, PATCH) operates on Bindings.
"""

import json
import math
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger
from core.types import CodeUnit, DocSection, Binding
from core.forge_bridge import ForgeBridge

log = get_logger("bind")


@dataclass
class BindResult:
    bindings:       list  = field(default_factory=list)   # list[Binding]
    doc_sections:   list  = field(default_factory=list)   # list[DocSection]
    unbound_units:  list  = field(default_factory=list)   # CodeUnits with no doc match
    unbound_docs:   list  = field(default_factory=list)   # DocSections with no code match
    duration_ms:    int   = 0


class BindModule:
    """
    Stage 2 of ANVIL.
    Uses embeddings to find which doc sections specify which code units.
    Falls back to keyword matching when embeddings aren't available.
    """

    def __init__(self, config: dict, ai, forge: ForgeBridge,
                 state: dict):
        self.config    = config
        self.ai        = ai
        self.forge     = forge
        self.state     = state
        self.threshold = config.get("audit", {}).get("binding_threshold", 0.65)
        self._embed_cache: dict[str, list[float]] = {}

    async def bind(self, units: list[CodeUnit],
                    doc_sections: list[DocSection]) -> BindResult:
        start  = datetime.now()
        result = BindResult(doc_sections=doc_sections)

        if not units or not doc_sections:
            log.info("Bind: nothing to bind (empty units or docs)")
            return result

        log.info(f"Binding {len(units)} code units against {len(doc_sections)} doc sections")

        # 1. Embed all doc sections
        doc_embeddings: dict[str, list[float]] = {}
        for section in doc_sections:
            text = f"{section.section_title}\n{section.content[:800]}"
            emb  = await self._embed(text)
            if emb:
                doc_embeddings[section.id] = emb

        # 2. For each code unit, find best-matching doc sections
        bound_unit_ids  = set()
        bound_doc_ids   = set()

        for unit in units:
            # Skip trivial units
            if unit.kind == "module" and len(unit.body) < 200:
                continue

            unit_text = self._unit_text(unit)
            unit_emb  = await self._embed(unit_text)

            best_score   = 0.0
            best_section = None

            for section in doc_sections:
                doc_emb = doc_embeddings.get(section.id)
                if not doc_emb:
                    # Fallback to keyword similarity
                    score = self._keyword_similarity(unit_text, section.content)
                else:
                    score = self._cosine(unit_emb, doc_emb)
                    # Boost for keyword overlap on top of embedding similarity
                    kw_boost = self._keyword_similarity(unit_text, section.content) * 0.15
                    score = min(1.0, score + kw_boost)

                if score > best_score:
                    best_score   = score
                    best_section = section

            if best_section and best_score >= self.threshold:
                binding = Binding(
                    doc_section_id=best_section.id,
                    code_unit_id=unit.id,
                    similarity=round(best_score, 3),
                    binding_type=self._infer_binding_type(unit, best_section),
                    confidence=round(best_score, 3),
                    last_verified=datetime.now().isoformat(),
                    is_stale=self._is_stale(best_section),
                )
                result.bindings.append(binding)
                bound_unit_ids.add(unit.id)
                bound_doc_ids.add(best_section.id)
            else:
                result.unbound_units.append(unit)

        result.unbound_docs = [s for s in doc_sections if s.id not in bound_doc_ids]
        result.duration_ms  = int((datetime.now() - start).total_seconds() * 1000)

        log.info(
            f"Bind complete: {len(result.bindings)} bindings, "
            f"{len(result.unbound_units)} unbound units, "
            f"{len(result.unbound_docs)} unbound docs"
        )
        return result

    # ── Embedding helpers ──────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        key = text[:100]
        if key in self._embed_cache:
            return self._embed_cache[key]
        try:
            emb = await self.ai.embed(text[:2000])
            if emb:
                self._embed_cache[key] = emb
            return emb
        except Exception as e:
            log.debug(f"Embed failed (using keyword fallback): {e}")
            return []

    def _cosine(self, a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot  = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def _keyword_similarity(self, code_text: str, doc_text: str) -> float:
        """Fast keyword overlap — used when embeddings unavailable."""
        import re
        def tokens(t):
            return set(re.findall(r'\b[a-zA-Z]\w{2,}\b', t.lower()))

        code_toks = tokens(code_text)
        doc_toks  = tokens(doc_text)
        if not code_toks or not doc_toks:
            return 0.0
        intersection = code_toks & doc_toks
        # Jaccard similarity
        union = code_toks | doc_toks
        return len(intersection) / len(union) if union else 0.0

    def _unit_text(self, unit: CodeUnit) -> str:
        """Build a searchable text representation of a code unit."""
        parts = [unit.name]
        if unit.docstring:
            parts.append(unit.docstring)
        if unit.signature:
            parts.append(unit.signature)
        # Add first 300 chars of body (enough for semantic signal)
        if unit.body:
            parts.append(unit.body[:300])
        return " ".join(parts)

    def _infer_binding_type(self, unit: CodeUnit, section: DocSection) -> str:
        """Guess the semantic relationship between code and doc."""
        name_lower = unit.name.lower()
        if any(kw in name_lower for kw in ["validate", "check", "verify", "assert"]):
            return "validates"
        if any(kw in name_lower for kw in ["config", "setting", "env", "constant"]):
            return "configures"
        if unit.kind == "route":
            return "implements"
        if unit.kind == "schema":
            return "defines"
        return "implements"

    def _is_stale(self, section: DocSection) -> bool:
        """Check if this doc section was updated more recently than last audit."""
        last_audit = self.state.get("last_audit_time", "")
        if not last_audit or not section.last_forge_run:
            return False
        try:
            return section.last_forge_run > last_audit
        except Exception:
            return False

    # ── Build lookup maps ──────────────────────────────────────────────────

    def build_lookup(self, bindings: list[Binding],
                      units: list[CodeUnit],
                      sections: list[DocSection]) -> dict:
        """Build fast lookup maps for the AUDIT stage."""
        unit_map    = {u.id: u for u in units}
        section_map = {s.id: s for s in sections}
        unit_bindings: dict[str, list[Binding]] = {}
        for b in bindings:
            unit_bindings.setdefault(b.code_unit_id, []).append(b)
        return {
            "units":         unit_map,
            "sections":      section_map,
            "unit_bindings": unit_bindings,
        }
