"""
ANVIL Module — SCAN
Stage 1: Walk configured repositories, parse code into CodeUnits,
detect changes since last run, build the processing queue.
"""

import hashlib
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger
from core.types import CodeUnit
from parsers import registry

log = get_logger("scan")


@dataclass
class ScanResult:
    repo_id:        str
    files_scanned:  int   = 0
    units_extracted:int   = 0
    new_files:      list  = field(default_factory=list)
    changed_files:  list  = field(default_factory=list)
    all_units:      list  = field(default_factory=list)   # list[CodeUnit]
    file_hashes:    dict  = field(default_factory=dict)   # path → hash
    errors:         list  = field(default_factory=list)
    duration_ms:    int   = 0


class ScanModule:
    """
    Stage 1 of the ANVIL loop.
    Walks source repos, parses every code file into CodeUnits,
    detects what changed since the last run.
    """

    def __init__(self, config: dict, state: dict):
        self.config        = config
        self.state         = state
        self.repos         = config.get("repositories", [])
        self.audit_cfg     = config.get("audit", {})
        self.skip_patterns = self.audit_cfg.get("skip_file_patterns", [])
        self.max_chars     = self.audit_cfg.get("max_file_chars", 15000)
        self._supported    = registry.supported_extensions()

    async def scan_all(self) -> list[ScanResult]:
        results = []
        for repo_cfg in self.repos:
            result = await self.scan_repo(repo_cfg)
            results.append(result)
        return results

    async def scan_repo(self, repo_cfg: dict) -> ScanResult:
        start   = datetime.now()
        repo_id = repo_cfg.get("id", "unknown")
        result  = ScanResult(repo_id=repo_id)

        repo_path = Path(repo_cfg.get("path", "."))
        if not repo_path.exists():
            log.warning(f"Repo path not found: {repo_path}")
            result.errors.append(f"Path not found: {repo_path}")
            return result

        # Git pull if configured
        if repo_cfg.get("auto_pull") and self.config.get("git", {}).get("enabled"):
            await self._git_pull(repo_path, result)

        # Build extension filter for this repo
        langs = repo_cfg.get("languages", [])
        allowed_exts = self._extensions_for_languages(langs) if langs else self._supported

        # Walk and parse
        prev_hashes = self.state.get("file_hashes", {}).get(repo_id, {})

        for file_path in self._walk(repo_path, repo_cfg, allowed_exts):
            rel = str(file_path.relative_to(repo_path))
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                if not content.strip():
                    continue

                # Truncate very large files
                if len(content) > self.max_chars:
                    content = content[:self.max_chars]
                    log.debug(f"Truncated large file: {rel}")

                file_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                result.file_hashes[rel] = file_hash

                if prev_hashes.get(rel) != file_hash:
                    if rel not in prev_hashes:
                        result.new_files.append(rel)
                    else:
                        result.changed_files.append(rel)

                # Parse into units
                units = registry.parse(rel, content)
                for unit in units:
                    unit.repo_id = repo_id
                result.all_units.extend(units)
                result.files_scanned += 1
                result.units_extracted += len(units)

            except Exception as e:
                log.warning(f"Scan error {rel}: {e}")
                result.errors.append(f"{rel}: {e}")

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        log.info(
            f"Scan [{repo_id}]: {result.files_scanned} files, "
            f"{result.units_extracted} units, "
            f"{len(result.new_files)} new, {len(result.changed_files)} changed"
        )
        return result

    def _walk(self, repo_path: Path, repo_cfg: dict,
               allowed_exts: set) -> list[Path]:
        import fnmatch
        exclude = repo_cfg.get("exclude_patterns", []) + self.skip_patterns
        results = []

        for p in repo_path.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in allowed_exts:
                continue

            rel = str(p.relative_to(repo_path))
            skip = False
            for pattern in exclude:
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(p.name, pattern):
                    skip = True
                    break
            if not skip:
                results.append(p)

        return results

    def _extensions_for_languages(self, langs: list[str]) -> set[str]:
        lang_to_ext = {
            "python":     {".py"},
            "javascript": {".js", ".jsx", ".mjs", ".cjs"},
            "typescript": {".ts", ".tsx"},
            "sql":        {".sql"},
            "html":       {".html", ".htm"},
            "css":        {".css", ".scss", ".less"},
            "go":         {".go"},
            "rust":       {".rs"},
            "java":       {".java"},
            "yaml":       {".yaml", ".yml"},
            "json":       {".json"},
        }
        exts = set()
        for lang in langs:
            exts.update(lang_to_ext.get(lang.lower(), set()))
        return exts or self._supported

    async def _git_pull(self, repo_path: Path, result: ScanResult):
        try:
            import git
            repo = git.Repo(repo_path)
            if repo.remotes:
                repo.remotes.origin.pull()
                log.info(f"Git pull: {repo_path.name}")
        except Exception as e:
            log.warning(f"Git pull failed {repo_path}: {e}")
            result.errors.append(f"git pull: {e}")
