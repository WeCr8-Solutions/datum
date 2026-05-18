"""
FORGE Module — VERIFY
Stage 2: Per-document quality scoring, domain rule checks,
technical claim verification via web search.
Outputs a score, list of failures, and enrichment data.
"""

import re
import asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

import yaml

from core.logger import get_logger, StateManager, WebSearch
from core.ai_client import AIClient

log = get_logger("verify")


@dataclass
class VerificationResult:
    path:               str
    domain:             str   = "general"
    doc_type:           str   = "document"
    quality_score:      int   = 0
    score_breakdown:    dict  = field(default_factory=dict)
    failures:           list  = field(default_factory=list)   # rule violations
    warnings:           list  = field(default_factory=list)
    web_context:        str   = ""
    needs_repair:       bool  = False
    itar_flagged:       bool  = False
    ai_assessment:      str   = ""
    duration_ms:        int   = 0


class VerifyModule:
    """
    Stage 2 of the FORGE loop.
    - Detects the document domain from content
    - Loads domain-specific rules
    - Runs rule-based checks (fast, no AI)
    - Runs AI-based quality assessment
    - Optionally enriches with web search results
    - Produces a score and repair queue
    """

    def __init__(self, config: dict, state: StateManager,
                 ai: AIClient, web: WebSearch, repo_path: str = "."):
        self.cfg       = config
        self.state     = state
        self.ai        = ai
        self.web       = web
        self.repo_path = Path(repo_path)
        self.domains   = {}  # id -> domain config dict
        self.vcfg      = config.get("verification", {})
        self.weights   = self.vcfg.get("score_weights", {
            "completeness": 0.25, "accuracy": 0.30,
            "format": 0.20, "clarity": 0.15, "currency": 0.10
        })
        self._load_domains()

    def _load_domains(self):
        domain_dir = Path(__file__).parent.parent.parent / "domains"
        if not domain_dir.exists():
            domain_dir = Path("./domains")

        for yaml_file in domain_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text())
                did  = data.get("domain", {}).get("id", yaml_file.stem)
                self.domains[did] = data
                log.debug(f"Loaded domain: {did}")
            except Exception as e:
                log.warning(f"Domain load failed {yaml_file}: {e}")

        log.info(f"Loaded {len(self.domains)} domain configs")

    # ── Main ──────────────────────────────────────────────────────────────

    async def verify_document(self, rec, content: str) -> VerificationResult:
        start  = datetime.now()
        result = VerificationResult(path=rec.path)

        log.info(f"Verifying: {rec.path}")

        # 1. Detect domain
        result.domain   = self._detect_domain(rec.path, content)
        result.doc_type = self._detect_doc_type(result.domain, content)
        rec.domain      = result.domain
        rec.doc_type    = result.doc_type

        # 2. Check ITAR sensitivity
        result.itar_flagged = self._check_itar(content)
        rec.itar_sensitive  = result.itar_flagged

        domain_cfg = self.domains.get(result.domain, self.domains.get("general", {}))

        # 3. Rule-based checks
        rule_failures, rule_warnings = self._run_rules(content, result.doc_type, domain_cfg)
        result.failures = rule_failures
        result.warnings = rule_warnings

        # 4. Web enrichment (skip for ITAR docs)
        web_disabled = (result.itar_flagged and
                        self.cfg.get("ai",{}).get("web_search",{}).get("disabled_for_itar", True))
        if not web_disabled:
            result.web_context = await self._web_enrich(content, result.domain, domain_cfg)

        # 5. AI quality assessment
        result.ai_assessment = await self._ai_assess(
            content, result.domain, result.doc_type,
            result.failures, result.warnings, result.web_context, domain_cfg
        )

        # 6. Compute score
        result.quality_score, result.score_breakdown = await self._compute_score(
            content, result.doc_type, result.failures, result.warnings,
            result.ai_assessment, domain_cfg
        )

        # 7. Decide repair
        min_score        = self.vcfg.get("min_quality_score", 75)
        result.needs_repair = result.quality_score < min_score or bool(
            [f for f in result.failures if f.get("severity") == "error"]
        )

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)

        log.info(f"  Score: {result.quality_score}/100 | "
                 f"Domain: {result.domain} | "
                 f"Failures: {len(result.failures)} | "
                 f"Repair needed: {result.needs_repair}")

        return result

    # ── Domain detection ──────────────────────────────────────────────────

    def _detect_domain(self, path: str, content: str) -> str:
        best_domain = "general"
        best_score  = 0

        for domain_id, dcfg in self.domains.items():
            if domain_id == "general":
                continue

            detection = dcfg.get("detection", {})
            score = 0

            # Strong keyword hits
            strong = detection.get("keywords", {}).get("strong", [])
            for kw in strong:
                if re.search(re.escape(kw), content, re.IGNORECASE):
                    score += 10

            # Weak keyword hits (need 2+)
            weak = detection.get("keywords", {}).get("weak", [])
            weak_hits = sum(1 for kw in weak
                           if re.search(re.escape(kw), content, re.IGNORECASE))
            if weak_hits >= 2:
                score += weak_hits * 3

            # Path pattern match
            patterns = detection.get("path_patterns", [])
            for pattern in patterns:
                import fnmatch
                if fnmatch.fnmatch(path.lower(), pattern.lower()):
                    score += 15

            if score > best_score:
                best_score  = score
                best_domain = domain_id

        return best_domain

    def _detect_doc_type(self, domain: str, content: str) -> str:
        domain_cfg = self.domains.get(domain, {})
        doc_types  = domain_cfg.get("document_types", {})

        # Simple heuristic: look for type-indicator patterns
        lower = content.lower()

        if domain == "manufacturing":
            if any(t in lower for t in ["sop number", "standard operating", "procedure number"]):
                return "sop"
            if any(t in lower for t in ["setup sheet", "op sheet", "operation sheet"]):
                return "setup_sheet"
            if any(t in lower for t in ["machine specifications", "controller", "alarm"]):
                return "machine_manual"
            if any(t in lower for t in ["quality manual", "quality policy", "qms"]):
                return "quality_manual"
            if any(t in lower for t in ["g01", "g00", "m03", "g-code", "m-code"]):
                return "g_code_reference"
            if any(t in lower for t in ["training", "objectives", "competency"]):
                return "training_material"

        if any(t in lower for t in ["executive summary", "findings", "recommendation"]):
            return "report"
        if any(t in lower for t in ["objectives", "training", "assessment"]):
            return "training"
        if any(t in lower for t in ["proposal", "investment", "next steps", "problem statement"]):
            return "proposal"

        return list(doc_types.keys())[0] if doc_types else "document"

    def _check_itar(self, content: str) -> bool:
        itar_markers = [
            "ITAR", "International Traffic in Arms", "USML",
            "EAR controlled", "export controlled",
            "not for export", "distribution restricted"
        ]
        return any(re.search(m, content, re.IGNORECASE) for m in itar_markers)

    # ── Rule engine ───────────────────────────────────────────────────────

    def _run_rules(self, content: str, doc_type: str, domain_cfg: dict):
        failures = []
        warnings = []
        rules    = domain_cfg.get("verification_rules", {})

        for category, rule_list in rules.items():
            for rule in (rule_list or []):
                result = self._evaluate_rule(rule, content, doc_type)
                if result:
                    item = {
                        "rule_id":   rule.get("id"),
                        "category":  category,
                        "message":   rule.get("description"),
                        "severity":  rule.get("severity", "warning"),
                    }
                    if rule.get("severity") == "error":
                        failures.append(item)
                    else:
                        warnings.append(item)

        return failures, warnings

    def _evaluate_rule(self, rule: dict, content: str, doc_type: str) -> bool:
        """Returns True if rule is VIOLATED (i.e., the check failed)."""
        rule_id = rule.get("id", "")

        # Required content check
        if "required" in rule:
            required = rule["required"]
            has_any  = any(re.search(r, content, re.IGNORECASE) for r in required)
            if not has_any:
                # Only flag if document is long enough to be substantive
                return len(content) > 200
            return False

        # Pattern must exist check
        if "pattern" in rule:
            pattern = rule["pattern"]
            exists  = bool(re.search(pattern, content, re.IGNORECASE))
            # For these rules, lack of pattern is not always a violation
            return False  # Pattern rules are informational for now

        # Required content given trigger keywords
        if "trigger_keywords" in rule and "required_content" in rule:
            triggers    = rule["trigger_keywords"]
            required    = rule["required_content"]
            has_trigger = any(re.search(t, content, re.IGNORECASE) for t in triggers)
            if has_trigger:
                has_required = any(re.search(r, content, re.IGNORECASE) for r in required)
                return not has_required

        # Specific rules
        if rule_id == "imperial_metric_consistency":
            has_mm  = bool(re.search(r'\d+\.?\d*\s*mm', content))
            has_in  = bool(re.search(r'\d+\.?\d*\s*(in|inches|")', content))
            return has_mm and has_in  # Mixed units = violation

        return False

    # ── Web enrichment ────────────────────────────────────────────────────

    async def _web_enrich(self, content: str, domain: str, domain_cfg: dict) -> str:
        if not self.web.enabled:
            return ""

        verify_topics = domain_cfg.get("web_verification", {}).get("verify_topics", [])
        if not verify_topics:
            return ""

        # Find the most relevant topic for this document
        content_lower = content.lower()
        relevant_topics = [
            t for t in verify_topics
            if any(word.lower() in content_lower for word in t.split()[:3])
        ][:2]  # Max 2 searches per doc

        if not relevant_topics:
            return ""

        all_results = []
        for topic in relevant_topics:
            results = await self.web.search(topic)
            all_results.extend(results)

        return self.web.format_context(all_results)

    # ── AI assessment ─────────────────────────────────────────────────────

    async def _ai_assess(self, content: str, domain: str, doc_type: str,
                          failures: list, warnings: list, web_context: str,
                          domain_cfg: dict) -> str:
        system = domain_cfg.get("repair_prompts", {}).get("base", "")
        if not system:
            system = "You are an expert document quality reviewer."

        failure_list = "\n".join(f"- [{f['severity'].upper()}] {f['message']}"
                                 for f in failures + warnings[:5])

        prompt = f"""Assess the quality of this {doc_type} document from the {domain} domain.

RULE VIOLATIONS FOUND:
{failure_list if failure_list else 'None detected by rule engine'}

{web_context}

DOCUMENT CONTENT:
---
{content[:3000]}
---

Evaluate on these dimensions (score each 0-100):
1. COMPLETENESS: Does it cover all required sections for a {doc_type}?
2. ACCURACY: Are technical claims correct and specific? (flag vague or potentially wrong info)
3. FORMAT: Does it follow professional standards for this document type?
4. CLARITY: Is it readable and actionable by the target audience?
5. CURRENCY: Does it appear up to date? (look for outdated info, old standards)

Also note the 3 most important improvements needed.

Respond as JSON:
{{
  "completeness": 0-100,
  "accuracy": 0-100,
  "format": 0-100,
  "clarity": 0-100,
  "currency": 0-100,
  "top_improvements": ["improvement 1", "improvement 2", "improvement 3"],
  "critical_issues": ["issue if any"],
  "summary": "2-sentence overall assessment"
}}"""

        try:
            result = await self.ai.complete_json(
                prompt, system=system, model_role="fast",
                cache_key=f"assess_{hash(content[:500])}"
            )
            return result
        except Exception as e:
            log.warning(f"AI assessment failed: {e}")
            return {}

    # ── Score computation ──────────────────────────────────────────────────

    async def _compute_score(self, content: str, doc_type: str,
                              failures: list, warnings: list,
                              ai_assessment: dict, domain_cfg: dict):
        breakdown = {}

        if isinstance(ai_assessment, dict) and ai_assessment:
            # Use AI scores as the base
            breakdown = {
                "completeness": ai_assessment.get("completeness", 70),
                "accuracy":     ai_assessment.get("accuracy", 70),
                "format":       ai_assessment.get("format", 70),
                "clarity":      ai_assessment.get("clarity", 70),
                "currency":     ai_assessment.get("currency", 70),
            }
        else:
            # Fallback: rule-based scoring
            error_count   = len(failures)
            warning_count = len(warnings)
            base = max(0, 85 - error_count * 15 - warning_count * 5)
            breakdown = {
                "completeness": base,
                "accuracy":     base,
                "format":       max(0, base - warning_count * 3),
                "clarity":      base,
                "currency":     base,
            }

        # Apply rule violation penalties
        for failure in failures:
            if failure.get("severity") == "error":
                cat = failure.get("category", "accuracy")
                if cat in breakdown:
                    breakdown[cat] = max(0, breakdown[cat] - 15)

        for warning in warnings:
            cat = warning.get("category", "format")
            if cat in breakdown:
                breakdown[cat] = max(0, breakdown[cat] - 5)

        # Weighted composite
        weights  = self.weights
        composite = sum(
            breakdown.get(dim, 70) * weights.get(dim, 0.2)
            for dim in ["completeness", "accuracy", "format", "clarity", "currency"]
        )

        return int(composite), breakdown
