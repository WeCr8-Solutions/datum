"""
FORGE Module — PULL
Stage 1: Git sync + filesystem scan + change detection + intake queue.
Outputs a prioritized list of documents that need processing this run.
"""

import os
import re
import hashlib
import asyncio
import aiofiles
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field

from core.logger import get_logger, StateManager, DocRecord

log = get_logger("pull")


@dataclass
class PullResult:
    """Summary of what PULL found this run."""
    repo_pulled:     bool  = False
    new_files:       list  = field(default_factory=list)
    changed_files:   list  = field(default_factory=list)
    staged_files:    list  = field(default_factory=list)
    queue:           list  = field(default_factory=list)   # DocRecords to process
    git_commit:      str   = ""
    git_branch:      str   = ""
    errors:          list  = field(default_factory=list)
    duration_ms:     int   = 0


class PullModule:
    """
    Stage 1 of the FORGE loop.
    - Pulls latest from Git
    - Scans configured directories for documents
    - Detects new and changed files via content hash
    - Builds a prioritized processing queue
    - Ingests files from the staging directory
    """

    def __init__(self, config: dict, state: StateManager, repo_path: str = "."):
        self.cfg       = config
        self.state     = state
        self.repo_path = Path(repo_path)
        self.git_cfg   = config.get("git", {})
        self.fs_cfg    = config.get("filesystem", {})
        self.extensions= set(self.fs_cfg.get("supported_extensions", [".md", ".txt"]))
        self.ignore    = self.fs_cfg.get("ignore_patterns", [])
        self.staging   = Path(self.fs_cfg.get("staging_dir", "./staging"))
        self.processed = Path(self.fs_cfg.get("processed_dir", "./processed"))

    async def run(self) -> PullResult:
        start = datetime.now()
        result = PullResult()

        log.info("── PULL ─────────────────────────────────────────")

        # 1. Git pull
        if self.git_cfg.get("auto_pull") and self.git_cfg.get("enabled"):
            await self._git_pull(result)

        # 2. Ingest staging directory
        await self._ingest_staging(result)

        # 3. Scan repo for documents
        all_docs = await self._scan_directory(self.repo_path)
        log.info(f"Found {len(all_docs)} documents in repository")

        # 4. Detect changes
        for doc_path in all_docs:
            file_hash = await self._hash_file(doc_path)
            rel_path  = str(doc_path.relative_to(self.repo_path))
            rec       = self.state.get_or_create(rel_path)
            rec.file_hash = file_hash

            if rel_path not in [r.path for r in result.staged_files]:
                if self.state.needs_processing(rec, file_hash):
                    if rec.git_hash is None:
                        result.new_files.append(rel_path)
                    else:
                        result.changed_files.append(rel_path)

        # 5. Build processing queue (new + changed + low-score)
        queued_paths = set()

        # Priority 1: Staged files (just uploaded)
        for rec in result.staged_files:
            if rec.path not in queued_paths:
                result.queue.append(rec)
                queued_paths.add(rec.path)

        # Priority 2: New and changed files
        for rel_path in result.new_files + result.changed_files:
            if rel_path not in queued_paths:
                result.queue.append(self.state.get_or_create(rel_path))
                queued_paths.add(rel_path)

        # Priority 3: Low-quality docs not recently repaired
        low_quality = [
            rec for path, rec in self.state.documents.items()
            if rec.quality_score is not None
            and rec.quality_score < 75
            and rec.repair_attempts < 3
            and path not in queued_paths
        ]
        low_quality.sort(key=lambda r: r.quality_score or 0)
        for rec in low_quality[:10]:  # Cap at 10 low-quality per run
            result.queue.append(rec)
            queued_paths.add(rec.path)

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)

        log.info(f"Pull complete: {len(result.new_files)} new, "
                 f"{len(result.changed_files)} changed, "
                 f"{len(result.staged_files)} staged, "
                 f"{len(result.queue)} queued for processing")

        return result

    # ── Git operations ────────────────────────────────────────────────────

    async def _git_pull(self, result: PullResult):
        try:
            import git
            repo = git.Repo(self.repo_path)
            result.git_branch = repo.active_branch.name

            if not repo.remotes:
                log.info("No git remote configured — skipping pull")
                return

            origin = repo.remotes.origin
            origin.pull()
            result.repo_pulled = True
            result.git_commit  = repo.head.commit.hexsha[:8]
            log.info(f"Git pull: branch={result.git_branch} commit={result.git_commit}")

        except Exception as e:
            msg = f"Git pull failed: {e}"
            log.warning(msg)
            result.errors.append(msg)

    # ── Staging ingestion ─────────────────────────────────────────────────

    async def _ingest_staging(self, result: PullResult):
        if not self.staging.exists():
            self.staging.mkdir(parents=True, exist_ok=True)
            return

        staged = list(self.staging.glob("**/*"))
        if not staged:
            return

        self.processed.mkdir(parents=True, exist_ok=True)

        for file_path in staged:
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in self.extensions:
                continue

            try:
                # Move to repo docs directory
                dest_dir = self.repo_path / "docs" / "staged"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / file_path.name

                # Copy content
                content = await aiofiles.open(file_path, "rb").__aenter__()
                # Simple synchronous fallback
                src_bytes  = file_path.read_bytes()
                dest.write_bytes(src_bytes)

                # Move original to processed
                proc_dest = self.processed / file_path.name
                file_path.rename(proc_dest)

                rel_path = str(dest.relative_to(self.repo_path))
                file_hash = self._hash_bytes(src_bytes)

                rec = self.state.get_or_create(rel_path)
                rec.file_hash = file_hash
                rec.status = "pending"
                result.staged_files.append(rec)

                log.info(f"Staged: {file_path.name} → {rel_path}")

            except Exception as e:
                log.error(f"Failed to stage {file_path}: {e}")

    # ── Directory scanner ─────────────────────────────────────────────────

    async def _scan_directory(self, directory: Path) -> list[Path]:
        results = []
        ignore_dirs = {".git", "node_modules", "__pycache__", ".forge_cache",
                       "staging", "processed", "reports", "logs"}

        for item in directory.rglob("*"):
            if not item.is_file():
                continue

            # Skip ignored directories
            if any(part in ignore_dirs for part in item.parts):
                continue

            # Check extension
            if item.suffix.lower() not in self.extensions:
                continue

            # Check ignore patterns
            if self._is_ignored(item):
                continue

            results.append(item)

        return results

    def _is_ignored(self, path: Path) -> bool:
        import fnmatch
        path_str = str(path)
        for pattern in self.ignore:
            if fnmatch.fnmatch(path_str, pattern):
                return True
            if fnmatch.fnmatch(path.name, pattern):
                return True
        return False

    # ── Hashing ───────────────────────────────────────────────────────────

    async def _hash_file(self, path: Path) -> str:
        try:
            content = path.read_bytes()
            return self._hash_bytes(content)
        except Exception:
            return ""

    def _hash_bytes(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()[:16]
