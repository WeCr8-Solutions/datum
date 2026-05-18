"""
FORGE Core — Logger, State, Web Search
"""

import os
import json
import time
import asyncio
import logging
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Logger ────────────────────────────────────────────────────────────────

_loggers: dict = {}

def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    log = logging.getLogger(f"forge.{name}")
    if not log.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        )
        handler.setFormatter(fmt)
        log.addHandler(handler)

    level = os.environ.get("FORGE_LOG_LEVEL", "INFO").upper()
    log.setLevel(getattr(logging, level, logging.INFO))
    _loggers[name] = log
    return log


log = get_logger("state")


# ── Document record ───────────────────────────────────────────────────────

class DocRecord:
    """Tracks the state and quality history of a single document."""

    def __init__(self, path: str):
        self.path            = path
        self.domain          = "general"
        self.doc_type        = "document"
        self.quality_score   = None       # 0-100, None = not yet scored
        self.last_verified   = None
        self.last_repaired   = None
        self.repair_attempts = 0
        self.repair_history  = []         # list of {date, score_before, score_after, changes}
        self.verification_failures = []
        self.git_hash        = None
        self.file_hash       = None
        self.status          = "pending"  # pending|verified|repaired|failed|skipped
        self.itar_sensitive  = False
        self.web_enriched    = False

    def to_dict(self) -> dict:
        return {
            "path":                 self.path,
            "domain":               self.domain,
            "doc_type":             self.doc_type,
            "quality_score":        self.quality_score,
            "last_verified":        self.last_verified,
            "last_repaired":        self.last_repaired,
            "repair_attempts":      self.repair_attempts,
            "repair_history":       self.repair_history,
            "verification_failures":self.verification_failures,
            "git_hash":             self.git_hash,
            "file_hash":            self.file_hash,
            "status":               self.status,
            "itar_sensitive":       self.itar_sensitive,
            "web_enriched":         self.web_enriched,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DocRecord":
        r = cls(d["path"])
        for k, v in d.items():
            setattr(r, k, v)
        return r


# ── State manager ─────────────────────────────────────────────────────────

class StateManager:
    """
    Persists FORGE state between runs.
    Tracks every document, its quality score, repair history, and loop run stats.
    """

    def __init__(self, state_file: str = "./forge_state.json"):
        self.state_file  = Path(state_file)
        self.documents:  dict[str, DocRecord] = {}
        self.run_history: list[dict] = []
        self.stats = {
            "total_runs":      0,
            "total_repairs":   0,
            "total_verified":  0,
            "avg_quality":     0,
            "last_run":        None,
        }
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                for path, d in data.get("documents", {}).items():
                    self.documents[path] = DocRecord.from_dict(d)
                self.run_history = data.get("run_history", [])[-100:]  # keep last 100
                self.stats.update(data.get("stats", {}))
                log.info(f"State loaded: {len(self.documents)} documents tracked")
            except Exception as e:
                log.warning(f"State load failed (starting fresh): {e}")

    def save(self):
        data = {
            "documents":   {p: r.to_dict() for p, r in self.documents.items()},
            "run_history": self.run_history[-100:],
            "stats":       self.stats,
            "saved_at":    datetime.now().isoformat(),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, indent=2))

    def get_or_create(self, path: str) -> DocRecord:
        if path not in self.documents:
            self.documents[path] = DocRecord(path)
        return self.documents[path]

    def record_run(self, run_data: dict):
        self.run_history.append({**run_data, "timestamp": datetime.now().isoformat()})
        self.stats["total_runs"] += 1
        self.stats["last_run"] = datetime.now().isoformat()
        scores = [r.quality_score for r in self.documents.values()
                  if r.quality_score is not None]
        if scores:
            self.stats["avg_quality"] = round(sum(scores) / len(scores), 1)

    def needs_processing(self, rec: DocRecord, file_hash: str) -> bool:
        """Returns True if file has changed or has never been processed."""
        if rec.file_hash != file_hash:
            return True
        if rec.status == "pending":
            return True
        if rec.quality_score is not None and rec.quality_score < 75:
            return True
        return False

    @property
    def summary(self) -> dict:
        records = list(self.documents.values())
        return {
            "total":    len(records),
            "verified": sum(1 for r in records if r.status == "verified"),
            "repaired": sum(1 for r in records if r.status == "repaired"),
            "failed":   sum(1 for r in records if r.status == "failed"),
            "pending":  sum(1 for r in records if r.status == "pending"),
            "avg_score":self.stats["avg_quality"],
        }


# ── Web search ────────────────────────────────────────────────────────────

class WebSearch:
    """
    Lightweight web search for fact verification.
    Uses DuckDuckGo (free, no API key) by default.
    Falls back gracefully if network unavailable.
    """

    def __init__(self, config: dict):
        self.cfg     = config.get("ai", {}).get("web_search", {})
        self.enabled = self.cfg.get("enabled", True)
        self.max_res = self.cfg.get("max_results", 5)
        self.timeout = self.cfg.get("timeout_seconds", 10)

    async def search(self, query: str) -> list[dict]:
        """
        Search and return list of {title, url, snippet} dicts.
        Returns empty list on any failure — never raises.
        """
        if not self.enabled:
            return []

        try:
            provider = self.cfg.get("provider", "duckduckgo")
            if provider == "duckduckgo":
                return await self._ddg_search(query)
            elif provider == "brave":
                return await self._brave_search(query)
            else:
                return await self._ddg_search(query)
        except Exception as e:
            log.debug(f"Web search failed (non-fatal): {e}")
            return []

    async def _ddg_search(self, query: str) -> list[dict]:
        """DuckDuckGo Instant Answer API — free, no key required."""
        import urllib.parse
        q = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={q}&format=json&no_redirect=1&no_html=1"

        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout),
                             headers={"User-Agent": "FORGE/1.0"}) as r:
                data = await r.json(content_type=None)

        results = []

        # Abstract answer
        if data.get("AbstractText"):
            results.append({
                "title":   data.get("Heading", query),
                "url":     data.get("AbstractURL", ""),
                "snippet": data["AbstractText"][:500],
                "source":  "ddg_abstract"
            })

        # Related topics
        for topic in data.get("RelatedTopics", [])[:self.max_res]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append({
                    "title":   topic.get("Text", "")[:80],
                    "url":     topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", "")[:300],
                    "source":  "ddg_related"
                })

        return results[:self.max_res]

    async def _brave_search(self, query: str) -> list[dict]:
        api_key = os.environ.get(self.cfg.get("brave_key_env", "BRAVE_API_KEY"), "")
        if not api_key:
            return await self._ddg_search(query)

        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": self.max_res},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as r:
                data = await r.json()

        results = []
        for item in data.get("web", {}).get("results", []):
            results.append({
                "title":   item.get("title", ""),
                "url":     item.get("url", ""),
                "snippet": item.get("description", "")[:400],
                "source":  "brave"
            })
        return results

    def format_context(self, results: list[dict], max_chars: int = 1500) -> str:
        """Format search results as context for AI prompts."""
        if not results:
            return ""

        lines = ["WEB SEARCH RESULTS FOR VERIFICATION:"]
        total = 0
        for r in results:
            entry = f"\n[{r.get('title','')}] ({r.get('url','')})\n{r.get('snippet','')}"
            if total + len(entry) > max_chars:
                break
            lines.append(entry)
            total += len(entry)

        return "\n".join(lines)
