#!/usr/bin/env python3
"""
ANVIL — Automated Node for Verifying Implementation against Literature
======================================================================
Doc-driven code verification and repair agent.
Companion to FORGE — reads FORGE-verified docs as the source of truth.

Loop stages:
  SCAN → BIND → AUDIT → PATCH → COMMIT

Scheduler runs on configurable intervals. Cron-style, daemon mode,
or one-shot. Inherits FORGE's decision ledger, agent memory, and
human review gate.

Usage:
  python3 anvil.py                        # Run loop continuously
  python3 anvil.py --once                 # One run and exit
  python3 anvil.py --dry-run              # Analyze only, no code changes
  python3 anvil.py --stage scan           # Run only one stage
  python3 anvil.py --repo wecr8-rag       # Target specific repo
  python3 anvil.py --status               # Print status and exit
  python3 anvil.py --config ./my.yaml     # Custom config

Environment:
  ANTHROPIC_API_KEY   Claude fallback (optional)
  WECR8_ADMIN_JWT     RAG backend JWT (optional)
  ANVIL_LOG_LEVEL     DEBUG|INFO|WARNING
"""

import os
import sys
import json
import asyncio
import argparse
import signal
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

import yaml
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# Re-use FORGE's infrastructure
sys.path.insert(0, str(Path(__file__).parent.parent / "forge"))

from core.logger      import get_logger, StateManager, WebSearch
from core.ai_client   import AIClient
from core.ledger      import DecisionLedger, DecisionType, Confidence, Outcome
from core.memory      import AgentMemory
from core.review_gate import HumanReviewGate, ReviewPriority
from core.task_queue  import TaskQueue, Priority

from core.forge_bridge import ForgeBridge
from parsers           import registry as parser_registry
from modules.scan      import ScanModule
from modules.bind      import BindModule
from modules.audit     import AuditModule
from modules.patch     import PatchModule

console = Console()
log     = get_logger("anvil")
_running = True


def load_config(path: str = "./config/anvil.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        console.print(f"[yellow]Config not found: {p} — using defaults[/]")
        return {}
    return yaml.safe_load(p.read_text()) or {}


def signal_handler(sig, frame):
    global _running
    console.print("\n[yellow]⚡ Shutdown signal — finishing current run...[/]")
    _running = False


# ── Shared infrastructure ──────────────────────────────────────────────────

def build_infrastructure(config: dict):
    """Initialize all shared subsystems."""
    sched_cfg = config.get("scheduler", {})
    state_file= config.get("system", {}).get("state_file", "./anvil_state.json")
    tasks_db  = config.get("system", {}).get("tasks_db", "./anvil_tasks.db")
    log_dir   = config.get("system", {}).get("log_dir", "./logs")

    run_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
    state   = _load_state(state_file)
    ai      = AIClient(config)
    web     = WebSearch(config)
    ledger  = DecisionLedger(log_dir, run_id=run_id)
    memory  = AgentMemory(str(Path(__file__).parent.parent / "forge" / "memory"))
    queue   = TaskQueue(tasks_db, worker_id=f"anvil_{run_id}")
    forge   = ForgeBridge(config)
    review  = HumanReviewGate(config, ledger, repo_path=".")

    return {
        "state": state, "state_file": state_file,
        "ai": ai, "web": web, "ledger": ledger,
        "memory": memory, "queue": queue,
        "forge": forge, "review": review,
        "run_id": run_id,
    }


def _load_state(state_file: str) -> dict:
    p = Path(state_file)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"file_hashes": {}, "last_audit_time": "", "runs": []}


def _save_state(state: dict, state_file: str):
    Path(state_file).write_text(json.dumps(state, indent=2))


# ── One full loop run ──────────────────────────────────────────────────────

async def run_once(config: dict, infra: dict, args) -> dict:
    """Execute one complete ANVIL loop. Returns run summary."""
    start = datetime.now()
    forge = infra["forge"]
    ai    = infra["ai"]
    state = infra["state"]

    forge.refresh()

    console.rule(f"[bold]ANVIL Run[/] — {start.strftime('%Y-%m-%d %H:%M:%S')}")

    run_summary = {
        "run_id":         infra["run_id"],
        "files_scanned":  0,
        "units_extracted":0,
        "bindings":       0,
        "issues":         0,
        "patches":        0,
        "pending_review": 0,
        "errors":         [],
    }

    # ── STAGE 1: SCAN ─────────────────────────────────────────────────────
    with console.status("[dim]Scanning repositories...[/]"):
        scanner    = ScanModule(config, state)
        scan_results = await scanner.scan_all()

    all_units = []
    for sr in scan_results:
        all_units.extend(sr.all_units)
        state.setdefault("file_hashes", {})[sr.repo_id] = sr.file_hashes
        run_summary["files_scanned"]   += sr.files_scanned
        run_summary["units_extracted"] += sr.units_extracted

    console.print(
        f"[bold]SCAN[/]: {run_summary['files_scanned']} files, "
        f"{run_summary['units_extracted']} code units"
    )

    if not all_units:
        console.print("[dim]No code units found — check repository paths[/]")
        return run_summary

    # ── STAGE 2: LOAD FORGE DOCS ──────────────────────────────────────────
    with console.status("[dim]Loading FORGE-verified documents...[/]"):
        verified_docs = forge.get_verified_docs()

    if not verified_docs:
        console.print(
            "[yellow]⚠  No FORGE-verified docs found. "
            "Run FORGE first to build a verified doc library.[/]"
        )
        # Continue — can still scan for dead code, etc.

    # Extract all doc sections
    all_sections = []
    for doc_rec in verified_docs:
        content = forge.read_doc(doc_rec)
        if content:
            sections = forge.extract_sections(content, doc_rec)
            all_sections.extend(sections)

    console.print(
        f"[bold]FORGE DOCS[/]: {len(verified_docs)} verified docs, "
        f"{len(all_sections)} sections"
    )

    if not all_sections and not args.dry_run:
        console.print("[dim]No doc sections to bind against — SCAN results saved[/]")
        _save_state(state, infra["state_file"])
        return run_summary

    # ── STAGE 3: BIND ─────────────────────────────────────────────────────
    with console.status("[dim]Building doc-to-code bindings...[/]"):
        binder    = BindModule(config, ai, forge, state)
        bind_result = await binder.bind(all_units, all_sections)

    lookup = binder.build_lookup(
        bind_result.bindings, all_units, all_sections
    )
    run_summary["bindings"] = len(bind_result.bindings)

    console.print(
        f"[bold]BIND[/]: {len(bind_result.bindings)} bindings "
        f"({len(bind_result.unbound_units)} unbound units, "
        f"{len(bind_result.unbound_docs)} unbound docs)"
    )

    # ── STAGE 4: AUDIT ────────────────────────────────────────────────────
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        task = prog.add_task("Auditing code against specifications...", total=None)
        auditor      = AuditModule(config, ai)
        audit_result = await auditor.audit(
            bind_result.bindings,
            lookup,
            bind_result.unbound_docs,
        )
        prog.remove_task(task)

    run_summary["issues"] = len(audit_result.issues)

    _print_audit_summary(audit_result)
    state["last_audit_time"] = datetime.now().isoformat()

    if not audit_result.issues:
        console.print("[green]✅ No issues found — code matches specifications[/]")
        _save_state(state, infra["state_file"])
        return run_summary

    # ── STAGE 5: PATCH ────────────────────────────────────────────────────
    if args.dry_run:
        console.print("[yellow]DRY RUN — skipping patch generation[/]")
    else:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            task = prog.add_task("Generating patches...", total=None)
            patcher      = PatchModule(config, ai, forge, state)
            patch_result = await patcher.patch_issues(
                audit_result.issues,
                lookup["units"],
                lookup["sections"],
                memory=infra["memory"],
            )
            prog.remove_task(task)

        run_summary["patches"]        = len(patch_result.patches)
        run_summary["pending_review"] = len(patch_result.pending_review)

        # ── STAGE 6: COMMIT ───────────────────────────────────────────────
        if patch_result.patches:
            await _commit_patches(patch_result.patches, config, infra)

        # Submit pending-review patches to review gate
        for patch in patch_result.pending_review:
            _submit_for_review(patch, audit_result, lookup, infra)

        _print_patch_summary(patch_result)

    # ── Finalize ──────────────────────────────────────────────────────────
    duration = (datetime.now() - start).total_seconds()
    run_summary["duration_seconds"] = duration

    # Record run in state
    state.setdefault("runs", []).append(run_summary)
    state["runs"] = state["runs"][-50:]  # Keep last 50
    _save_state(state, infra["state_file"])

    # Write to FORGE ledger so FORGE knows ANVIL ran
    infra["ledger"].record(
        decision_type=DecisionType.COMMITTED,
        decision=f"ANVIL run complete: {run_summary['issues']} issues, {run_summary['patches']} patches",
        reasoning=f"Full loop: scan→bind→audit→patch in {duration:.1f}s",
        confidence=Confidence.CERTAIN,
        outcome=Outcome.SUCCESS,
    )

    # Learn from successful patches
    if not args.dry_run:
        for patch in patch_result.patches:
            if patch.status.value in ("validated", "committed"):
                infra["memory"].save()

    return run_summary


async def _commit_patches(patches: list, config: dict, infra: dict):
    git_cfg = config.get("git", {})
    if not git_cfg.get("enabled") or not git_cfg.get("auto_commit"):
        return

    for patch in patches:
        repo_path = _resolve_repo_path(config, patch.repo_id)
        if not repo_path:
            continue

        file_path = repo_path / patch.file_path
        try:
            # Write patched content
            file_path.write_text(patch.content_after, encoding="utf-8")

            import git
            repo = git.Repo(repo_path)

            # Optionally create a feature branch
            if git_cfg.get("create_branch_for_patches"):
                branch_name = f"{git_cfg.get('branch_prefix','anvil/fix-')}{patch.id[:8]}"
                branch = repo.create_head(branch_name)
                branch.checkout()

            repo.index.add([str(file_path)])
            msg = (
                f"{git_cfg.get('commit_message_prefix','anvil:')} "
                f"fix: {patch.changes_summary[0] if patch.changes_summary else 'code spec alignment'}\n\n"
                f"Issue: {patch.issue_id}\n"
                f"Doc reference: {patch.doc_reference}\n"
                f"Self-check: {patch.self_check[:100]}"
            )
            import git as gitlib
            author = gitlib.Actor(
                git_cfg.get("author_name", "ANVIL AI"),
                git_cfg.get("author_email", "anvil@wecr8.info")
            )
            commit = repo.index.commit(msg, author=author, committer=author)
            patch.commit_hash = commit.hexsha[:8]
            patch.committed_at = datetime.now().isoformat()
            patch.status = "committed"
            log.info(f"Committed patch {patch.id[:8]}: {commit.hexsha[:8]}")

        except Exception as e:
            log.error(f"Commit failed for {patch.file_path}: {e}")


def _submit_for_review(patch, audit_result, lookup, infra):
    issue = next((i for i in audit_result.issues if i.id == patch.issue_id), None)
    if not issue:
        return

    unit = lookup["units"].get(issue.code_unit_id)
    from core.types import AuditIssue
    from core.review_gate import ReviewItem, ReviewStatus

    item = ReviewItem(
        doc_path=patch.file_path,
        doc_domain=unit.language if unit else "code",
        doc_type=unit.kind if unit else "file",
        score_before=0,
        score_after=0,
        score_delta=0,
        summary=f"{issue.title}\n{issue.description[:200]}",
        diff=patch.diff[:5000],
        changes_made=patch.changes_summary,
        content_before=patch.content_before[:10000],
        content_after=patch.content_after[:10000],
        priority="high" if issue.severity == "error" else "medium",
    )
    review_dir = Path(infra["review"].review_dir)
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / f"{item.id}.json").write_text(
        json.dumps(item.to_dict(), indent=2)
    )
    log.info(f"Submitted for review: {patch.file_path}")


def _resolve_repo_path(config: dict, repo_id: str) -> Optional[Path]:
    for repo in config.get("repositories", []):
        if repo.get("id") == repo_id:
            return Path(repo["path"])
    return None


# ── Printer helpers ────────────────────────────────────────────────────────

def _print_audit_summary(audit_result):
    by_type = audit_result.issues_by_type
    console.print(
        f"[bold]AUDIT[/]: {len(audit_result.issues)} issues — "
        f"[red]{by_type.get('contradiction',0)} contradictions[/] · "
        f"[yellow]{by_type.get('missing_impl',0)} missing[/] · "
        f"[dim]{by_type.get('drift',0)} drift[/] "
        f"({audit_result.auto_fixable} auto-fixable)"
    )

    if audit_result.issues:
        table = Table(border_style="dim", show_header=True, expand=False)
        table.add_column("Severity", width=9)
        table.add_column("Type", width=14)
        table.add_column("File", width=35)
        table.add_column("Issue", width=50)

        sev_style = {"error": "red", "warning": "yellow", "info": "dim"}
        for issue in sorted(audit_result.issues,
                            key=lambda i: {"error":0,"warning":1,"info":2}.get(i.severity,3))[:15]:
            table.add_row(
                f"[{sev_style.get(issue.severity,'white')}]{issue.severity.upper()}[/]",
                issue.issue_type,
                issue.file_path[-35:] if len(issue.file_path) > 35 else issue.file_path,
                issue.title[:50],
            )
        console.print(table)


def _print_patch_summary(patch_result):
    console.print(
        f"[bold]PATCH[/]: {len(patch_result.patches)} ready · "
        f"[yellow]{len(patch_result.pending_review)} pending review[/] · "
        f"[red]{len(patch_result.failed)} failed[/]"
    )


# ── Entry point ────────────────────────────────────────────────────────────

async def main_loop(config: dict, args):
    infra    = build_infrastructure(config)
    interval = config.get("scheduler", {}).get("scan_interval_seconds", 3600)
    run_on_start = config.get("scheduler", {}).get("run_on_start", True)
    run_count = 0

    console.print(Panel(
        f"[bold]ANVIL[/] — Doc-Driven Code Verification\n"
        f"FORGE docs: [dim]{config.get('forge',{}).get('docs_path','../forge/docs')}[/]\n"
        f"Repos: {len(config.get('repositories',[]))} configured\n"
        f"AI: [green]Ollama primary[/] + Claude fallback\n"
        f"Mode: [yellow]{'DRY RUN' if args.dry_run else 'LIVE'}[/] · "
        f"Interval: {interval}s",
        border_style="dim"
    ))

    # Check Ollama
    ollama_ok = await infra["ai"].check_ollama()
    if not ollama_ok:
        console.print("[yellow]⚠  Ollama not running — using Claude fallback[/]")

    while _running:
        run_count += 1
        if run_count > 1 or not run_on_start:
            console.print(f"\n[dim]Next run in {interval}s...[/]")
            for _ in range(interval):
                if not _running:
                    break
                await asyncio.sleep(1)
            if not _running:
                break

        try:
            summary = await run_once(config, infra, args)
            _print_run_summary(summary)
        except Exception as e:
            log.error(f"Run failed: {e}", exc_info=True)
            console.print(f"[red]❌ Run error: {e}[/]")

        if args.once:
            break

    console.print("[bold green]ANVIL stopped cleanly.[/]")


def _print_run_summary(summary: dict):
    table = Table(title="Run Summary", border_style="dim")
    table.add_column("Metric")
    table.add_column("Value", style="bold")
    for k, v in summary.items():
        if k not in ("errors", "run_id"):
            table.add_row(k.replace("_", " ").title(), str(v))
    console.print(table)


def main():
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="ANVIL — Doc-Driven Code Agent")
    parser.add_argument("--config",  default="./config/anvil.yaml")
    parser.add_argument("--once",    action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stage",   choices=["scan","bind","audit","patch"])
    parser.add_argument("--repo",    help="Target specific repo ID")
    parser.add_argument("--status",  action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.dry_run:
        config.setdefault("scheduler", {})["dry_run"] = True

    if args.once or args.stage:
        args.once = True

    asyncio.run(main_loop(config, args))


if __name__ == "__main__":
    main()
