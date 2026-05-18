#!/usr/bin/env python3
"""
FORGE — Framework for Ongoing Repository Growth & Enhancement
=============================================================
Self-repairing, self-improving, self-verifying document intelligence loop.

Architecture:
  PULL → VERIFY → REPAIR → COMMIT → (loop)

Each stage is a pluggable module. Ollama-primary, Claude-fallback.
Domain-agnostic (manufacturing, business, general, custom).
Git-backed, ITAR-safe, zero required API costs.

Usage:
  python3 forge.py                           # Run loop continuously
  python3 forge.py --once                    # Run loop one time and exit
  python3 forge.py --dry-run                 # Analyze only, no writes
  python3 forge.py --path ./my-docs          # Point at a specific directory
  python3 forge.py --domain manufacturing    # Force a domain
  python3 forge.py --file ./sop-001.md       # Process a single file
  python3 forge.py --status                  # Print status and exit
  python3 forge.py --add-domain              # Interactive domain wizard
  python3 forge.py --config ./my-config.yaml # Use custom config file

Environment variables:
  ANTHROPIC_API_KEY    Claude fallback (optional)
  WECR8_ADMIN_JWT      RAG backend JWT (optional)
  WECR8_CLIENT_ID      RAG client ID (optional)
  FORGE_LOG_LEVEL      DEBUG|INFO|WARNING (default: INFO)
"""

import os
import sys
import asyncio
import argparse
import signal
import time
from pathlib import Path
from datetime import datetime

import yaml
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

console = Console()
_running = True


def load_config(config_path: str = "./config/forge.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        console.print(f"[yellow]Config not found at {path} — using defaults[/]")
        return {}
    return yaml.safe_load(path.read_text()) or {}


def signal_handler(sig, frame):
    global _running
    console.print("\n[yellow]⚡ Shutdown signal received — finishing current run...[/]")
    _running = False


# ── Per-file processor ────────────────────────────────────────────────────

async def process_document(rec, repo_path: str, ai, verifier, repairer,
                            config: dict, verbose: bool = False) -> tuple:
    """Full pipeline for a single document. Returns (vr, rr)."""
    from core.logger import get_logger
    log = get_logger("forge")

    full_path = Path(repo_path) / rec.path
    if not full_path.exists():
        full_path = Path(rec.path)
    if not full_path.exists():
        log.warning(f"File not found: {rec.path}")
        return None, None

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.error(f"Cannot read {rec.path}: {e}")
        return None, None

    if len(content.strip()) < 50:
        log.debug(f"Skipping too-short file: {rec.path}")
        return None, None

    # VERIFY
    vr = await verifier.verify_document(rec, content)
    rec.quality_score = vr.quality_score
    rec.last_verified = datetime.now().isoformat()
    rec.verification_failures = vr.failures

    if verbose:
        console.print(
            f"  [dim]{rec.path[:50]}[/] "
            f"[{'green' if vr.quality_score >= 75 else 'yellow' if vr.quality_score >= 60 else 'red'}]"
            f"{vr.quality_score}[/] "
            f"[dim]({vr.domain})[/]"
        )

    # REPAIR if needed
    rr = None
    if vr.needs_repair:
        from modules.repair import RepairModule
        rr = await repairer.repair_document(rec, vr, content)

    if not vr.needs_repair:
        rec.status = "verified"

    return vr, rr


# ── Main loop ─────────────────────────────────────────────────────────────

async def run_loop(config: dict, repo_path: str, args):
    from core.logger import get_logger, StateManager, WebSearch
    from core.ai_client import AIClient
    from modules.pull import PullModule
    from modules.verify import VerifyModule
    from modules.repair import RepairModule
    from modules.commit import CommitModule

    log = get_logger("forge")

    # Apply CLI overrides
    if args.dry_run:
        config.setdefault("loop", {})["dry_run"] = True

    # Initialize subsystems
    state    = StateManager(config.get("system", {}).get("state_file", "./forge_state.json"))
    ai       = AIClient(config)
    web      = WebSearch(config)
    puller   = PullModule(config, state, repo_path)
    verifier = VerifyModule(config, state, ai, web, repo_path)
    repairer = RepairModule(config, state, ai, verifier, repo_path)
    committer= CommitModule(config, state, repo_path)

    # Check Ollama
    ollama_ok = await ai.check_ollama()
    if not ollama_ok:
        console.print("[yellow]⚠  Ollama not running — Claude fallback will be used if enabled[/]")
        if not config.get("ai", {}).get("fallback", {}).get("enabled"):
            console.print("[red]❌ No AI available. Start Ollama or enable Claude fallback.[/]")
            return

    console.print(Panel(
        f"[bold]FORGE[/] — Document Intelligence Loop\n"
        f"Repo: [dim]{repo_path}[/]\n"
        f"AI: [green]Ollama {'✓' if ollama_ok else '✗'}[/] | "
        f"Claude fallback: {'[green]enabled[/]' if config.get('ai',{}).get('fallback',{}).get('enabled') else '[dim]disabled[/]'}\n"
        f"Mode: [yellow]{'DRY RUN' if args.dry_run else 'LIVE'}[/]",
        border_style="dim"
    ))

    interval = config.get("loop", {}).get("interval_seconds", 3600)
    run_on_start = config.get("loop", {}).get("on_start", True)
    max_concurrent = config.get("loop", {}).get("max_concurrent_docs", 3)

    run_count = 0

    while _running:
        run_count += 1

        # Skip first sleep if run_on_start
        if run_count > 1 or not run_on_start:
            console.print(f"\n[dim]Sleeping {interval}s until next run...[/]")
            for _ in range(interval):
                if not _running:
                    break
                await asyncio.sleep(1)
            if not _running:
                break

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        console.rule(f"[bold]FORGE Run #{run_count}[/] — {now}")

        run_data = {
            "run": run_count,
            "verified": 0, "repaired": 0,
            "avg_quality": 0,
        }

        try:
            # ── STAGE 1: PULL ─────────────────────────────────────────────
            with console.status("[dim]Pulling latest...[/]"):
                pull_result = await puller.run()

            if not pull_result.queue:
                console.print("[dim]Nothing to process this run.[/]")
                state.record_run(run_data)
                state.save()
                if args.once:
                    break
                continue

            console.print(f"[bold]Queue:[/] {len(pull_result.queue)} documents")

            # ── STAGES 2+3: VERIFY + REPAIR ───────────────────────────────
            verify_results = []
            repair_results = []
            semaphore = asyncio.Semaphore(max_concurrent)

            async def process_with_sem(rec):
                async with semaphore:
                    return await process_document(
                        rec, repo_path, ai, verifier, repairer,
                        config, verbose=not args.quiet
                    )

            # Process in parallel (bounded by semaphore)
            tasks = [process_with_sem(rec) for rec in pull_result.queue]

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task_id = progress.add_task(
                    f"Processing {len(tasks)} documents...", total=len(tasks)
                )
                results = []
                for coro in asyncio.as_completed(tasks):
                    vr, rr = await coro
                    if vr:
                        verify_results.append(vr)
                        run_data["verified"] += 1
                    if rr and rr.success:
                        repair_results.append(rr)
                        run_data["repaired"] += 1
                    progress.advance(task_id)

            # ── STAGE 4: COMMIT ───────────────────────────────────────────
            with console.status("[dim]Committing...[/]"):
                commit_result = await committer.run(pull_result, verify_results, repair_results)

            # Summary
            scores = [vr.quality_score for vr in verify_results]
            avg    = int(sum(scores) / len(scores)) if scores else 0
            run_data["avg_quality"] = avg
            state.record_run(run_data)

            _print_summary(verify_results, repair_results, commit_result, avg)

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
            console.print(f"[red]❌ Loop error: {e}[/]")

        if args.once or not config.get("loop", {}).get("enabled", True):
            break

    console.print("[bold green]FORGE stopped cleanly.[/]")


def _print_summary(verify_results, repair_results, commit_result, avg_score):
    table = Table(title="Run Summary", border_style="dim", show_header=True)
    table.add_column("Metric", style="dim")
    table.add_column("Value", style="bold")

    repaired_ok  = sum(1 for r in repair_results if r.success)
    failed_repair= sum(1 for r in repair_results if not r.success and not r.skipped)

    sc = "#22d3a0" if avg_score >= 75 else "#f59e0b" if avg_score >= 60 else "#ef4444"
    table.add_row("Avg Quality Score", f"[{sc}]{avg_score}/100[/]")
    table.add_row("Verified",  str(len(verify_results)))
    table.add_row("Repaired",  f"[green]{repaired_ok}[/]")
    table.add_row("Failed",    f"[red]{failed_repair}[/]" if failed_repair else "0")
    table.add_row("Committed", "✓" if commit_result.committed else "—")
    table.add_row("Re-indexed", str(commit_result.reindexed))
    if commit_result.report_path:
        table.add_row("Report", Path(commit_result.report_path).name)

    console.print(table)


# ── CLI status ────────────────────────────────────────────────────────────

async def print_status(config: dict):
    from core.logger import StateManager
    state = StateManager(config.get("system", {}).get("state_file", "./forge_state.json"))

    summary = state.summary
    stats   = state.stats

    console.print(Panel(
        f"[bold]FORGE Status[/]\n\n"
        f"Total Runs:    {stats.get('total_runs', 0)}\n"
        f"Last Run:      {stats.get('last_run', 'Never')}\n"
        f"Avg Quality:   [bold]{stats.get('avg_quality', 0):.1f}/100[/]\n"
        f"Documents:     {summary.get('total', 0)} total\n"
        f"  Verified:    [green]{summary.get('verified', 0)}[/]\n"
        f"  Repaired:    [yellow]{summary.get('repaired', 0)}[/]\n"
        f"  Failed:      [red]{summary.get('failed', 0)}[/]\n"
        f"  Pending:     {summary.get('pending', 0)}",
        title="FORGE",
        border_style="dim"
    ))


# ── Entry point ───────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(
        description="FORGE — Self-repairing document intelligence loop"
    )
    parser.add_argument("--config",   default="./config/forge.yaml",
                        help="Config file path")
    parser.add_argument("--path",     default=".",
                        help="Repository/document root path")
    parser.add_argument("--once",     action="store_true",
                        help="Run once and exit")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Analyze only, no file writes or git commits")
    parser.add_argument("--file",     help="Process a single file")
    parser.add_argument("--domain",   help="Force document domain")
    parser.add_argument("--status",   action="store_true",
                        help="Print status and exit")
    parser.add_argument("--quiet",    action="store_true",
                        help="Minimal output")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.status:
        asyncio.run(print_status(config))
        return

    # Single file mode
    if args.file:
        args.once = True

    asyncio.run(run_loop(config, args.path, args))


if __name__ == "__main__":
    main()
