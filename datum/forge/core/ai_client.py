"""
FORGE Core — AI Client
Ollama-primary, Claude-fallback, unified interface.
All AI calls go through this module. Never call providers directly.
"""

import os
import json
import time
import asyncio
import aiohttp
from typing import Optional, AsyncIterator
from dataclasses import dataclass
from enum import Enum

from .logger import get_logger

log = get_logger("ai_client")


class AIProvider(Enum):
    OLLAMA    = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI    = "openai"


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    from_cache: bool = False


class AIClient:
    """
    Unified AI client. Tries Ollama first (local, free, ITAR-safe).
    Falls back to Claude only if configured and Ollama fails.
    """

    def __init__(self, config: dict):
        self.cfg         = config.get("ai", {})
        self.primary_cfg = self.cfg.get("primary", {})
        self.fallback_cfg= self.cfg.get("fallback", {})
        self._ollama_ok  = None   # None = not yet checked
        self._response_cache: dict[str, AIResponse] = {}

    # ── Health check ──────────────────────────────────────────────────────

    async def check_ollama(self) -> bool:
        """Ping Ollama to see if it's running."""
        base = self.primary_cfg.get("base_url", "http://localhost:11434")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{base}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    self._ollama_ok = r.status == 200
                    if self._ollama_ok:
                        data = await r.json()
                        models = [m["name"] for m in data.get("models", [])]
                        log.info(f"Ollama online — {len(models)} models: {', '.join(models[:5])}")
                    return self._ollama_ok
        except Exception as e:
            log.warning(f"Ollama not reachable: {e}")
            self._ollama_ok = False
            return False

    async def list_ollama_models(self) -> list[str]:
        base = self.primary_cfg.get("base_url", "http://localhost:11434")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{base}/api/tags") as r:
                    data = await r.json()
                    return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    # ── Main interface ────────────────────────────────────────────────────

    async def complete(
        self,
        prompt: str,
        system: str = "",
        model_role: str = "reasoning",   # reasoning | fast
        task: str = "general",           # used to decide fallback eligibility
        max_tokens: int = 2000,
        temperature: float = 0.2,
        cache_key: Optional[str] = None,
    ) -> AIResponse:
        """
        Complete a prompt. Tries Ollama first, falls back to Claude if needed.
        Low temperature default (0.2) = more deterministic for doc work.
        """

        # Cache hit
        if cache_key and cache_key in self._response_cache:
            cached = self._response_cache[cache_key]
            log.debug(f"Cache hit: {cache_key[:40]}")
            return AIResponse(**{**cached.__dict__, "from_cache": True})

        # Try Ollama
        if self._ollama_ok is None:
            await self.check_ollama()

        if self._ollama_ok:
            try:
                resp = await self._ollama_complete(prompt, system, model_role, max_tokens, temperature)
                if cache_key:
                    self._response_cache[cache_key] = resp
                return resp
            except Exception as e:
                log.warning(f"Ollama failed ({e}) — checking fallback")
                self._ollama_ok = False

        # Fallback to Claude
        fallback_enabled = self.fallback_cfg.get("enabled", False)
        fallback_tasks   = self.fallback_cfg.get("use_for", [])

        if fallback_enabled and (not fallback_tasks or task in fallback_tasks or "all" in fallback_tasks):
            log.info(f"Using Claude fallback for task: {task}")
            try:
                resp = await self._claude_complete(prompt, system, max_tokens, temperature)
                if cache_key:
                    self._response_cache[cache_key] = resp
                return resp
            except Exception as e:
                log.error(f"Claude fallback also failed: {e}")
                raise RuntimeError(f"All AI providers failed. Last error: {e}")

        raise RuntimeError(
            "Ollama unavailable and Claude fallback disabled or not eligible for this task. "
            "Start Ollama or enable fallback in config."
        )

    async def embed(self, text: str) -> list[float]:
        """Generate embeddings. Always uses Ollama (no cloud embedding)."""
        if self._ollama_ok is None:
            await self.check_ollama()

        if not self._ollama_ok:
            log.warning("Ollama not available for embeddings — returning empty vector")
            return []

        base  = self.primary_cfg.get("base_url", "http://localhost:11434")
        model = self.primary_cfg.get("models", {}).get("embedding", "nomic-embed-text")

        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{base}/api/embeddings",
                json={"model": model, "prompt": text[:4000]},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                data = await r.json()
                return data.get("embedding", [])

    # ── Ollama implementation ─────────────────────────────────────────────

    async def _ollama_complete(
        self, prompt: str, system: str, model_role: str,
        max_tokens: int, temperature: float
    ) -> AIResponse:
        base    = self.primary_cfg.get("base_url", "http://localhost:11434")
        models  = self.primary_cfg.get("models", {})
        model   = models.get(model_role, models.get("reasoning", "llama3.1"))
        timeout = self.primary_cfg.get("timeout_seconds", 120)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        start = time.time()

        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{base}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    }
                },
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as r:
                if r.status != 200:
                    text = await r.text()
                    raise RuntimeError(f"Ollama HTTP {r.status}: {text[:200]}")

                data  = await r.json()
                ms    = int((time.time() - start) * 1000)
                content = data.get("message", {}).get("content", "")

                return AIResponse(
                    text=content,
                    provider="ollama",
                    model=model,
                    input_tokens=data.get("prompt_eval_count", 0),
                    output_tokens=data.get("eval_count", 0),
                    latency_ms=ms,
                )

    # ── Claude implementation ─────────────────────────────────────────────

    async def _claude_complete(
        self, prompt: str, system: str, max_tokens: int, temperature: float
    ) -> AIResponse:
        api_key = os.environ.get(
            self.fallback_cfg.get("api_key_env", "ANTHROPIC_API_KEY"), ""
        )
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        model  = self.fallback_cfg.get("model", "claude-sonnet-4-5")
        start  = time.time()

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as r:
                if r.status != 200:
                    text = await r.text()
                    raise RuntimeError(f"Claude HTTP {r.status}: {text[:200]}")

                data    = await r.json()
                ms      = int((time.time() - start) * 1000)
                content = data["content"][0]["text"]

                return AIResponse(
                    text=content,
                    provider="anthropic",
                    model=model,
                    input_tokens=data["usage"]["input_tokens"],
                    output_tokens=data["usage"]["output_tokens"],
                    latency_ms=ms,
                )

    # ── JSON helper ───────────────────────────────────────────────────────

    async def complete_json(self, prompt: str, system: str = "",
                            model_role: str = "fast", **kwargs) -> dict:
        """Complete and parse JSON response. Retries on parse failure."""
        for attempt in range(3):
            resp = await self.complete(
                prompt + "\n\nRespond ONLY with valid JSON. No markdown, no explanation.",
                system=system, model_role=model_role, **kwargs
            )
            try:
                text = resp.text.strip()
                text = text.replace("```json", "").replace("```", "").strip()
                return json.loads(text)
            except json.JSONDecodeError:
                if attempt == 2:
                    log.error(f"JSON parse failed after 3 attempts: {resp.text[:200]}")
                    return {}
                log.debug(f"JSON parse attempt {attempt+1} failed, retrying")
        return {}

    def clear_cache(self):
        self._response_cache.clear()

    @property
    def stats(self) -> dict:
        return {
            "ollama_online": self._ollama_ok,
            "cache_size": len(self._response_cache),
        }
