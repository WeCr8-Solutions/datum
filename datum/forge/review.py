#!/usr/bin/env python3
"""
FORGE Review CLI
=================
Approve or reject agent repairs from the terminal.
Designed to be fast — a quality manager can work through the queue
in minutes without opening a browser.

Usage:
  python3 review.py                    # Show queue
  python3 review.py --approve ABC123   # Approve item
  python3 review.py --reject  ABC123 --reason "Changed wrong values"
  python3 review.py --rollback ABC123  # Revert Git commit
  python3 review.py --diff    ABC123   # Show full diff
  python3 review.py --interactive      # Work through queue one by one
"""

import sys
import argparse
import json
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

console = Console()


def load_config(config_path: str = "./config/forge.yaml") -> dict:
    p = Path(config_path)
    if p.exists():
        return yaml.safe_load(p.read_text()) or {}
    return {}


def get_gate(config: dict, repo_path: str = "."):
    from core.ledger    import DecisionLedger
    from core.review_gate import HumanReviewGate
    from core.memory    import AgentMemory

    ledger = DecisionLedger("./logs", run_id="review_cli")
    gate   = HumanReviewGate(config, ledger, repo_path)
    memory = AgentMemory("./memory")
    return gate, memory


def cmd_list(gate):
    items = gate.get_pending()
    if not items:
        console.print("[green]✅ Review queue is empty[/]")
        return

    table = Table(title=f"Review Queue — {len(items)} pending",
                  border_style="dim", show_lines=True)
    table.add_column("ID",       style="dim",    no_wrap=True, width=16)
    table.add_column("Priority", width=10)
    table.add_column("Document", width=35)
    table.add_column("Score",    width=12)
    table.add_column("Age",      width=8)
    table.add_column("ITAR",     width=6)

    priority_style = {
        "critical": "bold red",
        "high":     "yellow",
        "medium":   "white",
        "low":      "dim",
    }

    for item in items:
        delta = item.score_delta
        score_str = f"{item.score_before}→{item.score_after} ({'+' if delta >= 0 else ''}{delta})"
        score_style = "green" if delta > 0 else ("red" if delta < 0 else "white")

        table.add_row(
            item.id,
            f"[{priority_style.get(item.priority,'white')}]{item.priority.upper()}[/]",
            item.doc_path[-35:] if len(item.doc_path) > 35 else item.doc_path,
            f"[{score_style}]{score_str}[/]",
            f"{item.age_hours:.1f}h",
            "[red]⚠ YES[/]" if item.itar_flagged else "No",
        )

    console.print(table)
    console.print(f"\n[dim]Commands: --approve ID | --reject ID | --diff ID | --interactive[/]")


def cmd_diff(gate, item_id: str):
    items = {i.id: i for i in gate.get_pending()}
    item  = items.get(item_id)
    if not item:
        console.print(f"[red]Item {item_id} not found[/]")
        return

    console.print(Panel(
        f"[bold]{item.doc_path}[/]\n"
        f"Score: {item.score_before} → {item.score_after} ({item.score_delta:+d})\n"
        f"Domain: {item.doc_domain} · Type: {item.doc_type}\n"
        f"Priority: {item.priority.upper()} · ITAR: {'YES ⚠' if item.itar_flagged else 'No'}\n\n"
        f"Summary:\n{item.summary}",
        title=f"Review: {item.id}",
        border_style="yellow" if item.itar_flagged else "dim"
    ))

    if item.diff:
        console.print(Syntax(item.diff, "diff", theme="monokai", line_numbers=False))
    else:
        console.print("[dim]No diff available[/]")


def cmd_approve(gate, item_id: str, notes: str = ""):
    ok, msg = gate.approve(item_id, reviewed_by="cli_user", notes=notes)
    if ok:
        console.print(f"[green]✅ Approved: {item_id}[/]")
        if notes:
            console.print(f"[dim]Notes: {notes}[/]")
    else:
        console.print(f"[red]❌ {msg}[/]")


def cmd_reject(gate, memory, item_id: str, reason: str = ""):
    if not reason:
        reason = Prompt.ask("Rejection reason")
    ok, msg = gate.reject(item_id, reviewed_by="cli_user", reason=reason, memory=memory)
    if ok:
        console.print(f"[yellow]↩ Rejected and reverted: {item_id}[/]")
        console.print(f"[dim]Agent will remember: {reason[:60]}[/]")
    else:
        console.print(f"[red]❌ {msg}[/]")


def cmd_rollback(gate, memory, item_id: str, reason: str = ""):
    if not Confirm.ask(f"Roll back the Git commit for {item_id}?"):
        return
    ok, msg = gate.rollback_commit(item_id, reason=reason, memory=memory)
    if ok:
        console.print(f"[green]↩ {msg}[/]")
    else:
        console.print(f"[red]❌ {msg}[/]")


def cmd_interactive(gate, memory):
    """Work through the queue one item at a time."""
    items = gate.get_pending()
    if not items:
        console.print("[green]Queue is empty[/]")
        return

    console.print(f"[bold]Interactive review — {len(items)} items[/]")
    console.print("[dim]Commands: a=approve, r=reject, d=diff, s=skip, q=quit[/]\n")

    for i, item in enumerate(items):
        console.print(
            f"\n[bold][{i+1}/{len(items)}][/] [{item.priority.upper()}] "
            f"[cyan]{item.doc_path}[/] "
            f"Score: {item.score_before}→{item.score_after} ({item.score_delta:+d}) "
            + ("[red]⚠ ITAR[/]" if item.itar_flagged else "")
        )
        if item.summary:
            console.print(f"[dim]{item.summary.splitlines()[0]}[/]")

        while True:
            cmd = Prompt.ask("Action", choices=["a","r","d","s","q"], default="s")
            if cmd == "a":
                notes = Prompt.ask("Notes (optional)", default="")
                cmd_approve(gate, item.id, notes)
                break
            elif cmd == "r":
                reason = Prompt.ask("Rejection reason")
                cmd_reject(gate, memory, item.id, reason)
                break
            elif cmd == "d":
                cmd_diff(gate, item.id)
            elif cmd == "s":
                console.print("[dim]Skipped[/]")
                break
            elif cmd == "q":
                console.print("[dim]Exiting review[/]")
                return

    console.print("\n[green]Review session complete[/]")


def main():
    parser = argparse.ArgumentParser(description="FORGE Review CLI")
    parser.add_argument("--config",      default="./config/forge.yaml")
    parser.add_argument("--repo",        default=".")
    parser.add_argument("--approve",     metavar="ID")
    parser.add_argument("--reject",      metavar="ID")
    parser.add_argument("--rollback",    metavar="ID")
    parser.add_argument("--diff",        metavar="ID")
    parser.add_argument("--reason",      default="")
    parser.add_argument("--notes",       default="")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    gate, memory = get_gate(config, args.repo)

    if args.diff:
        cmd_diff(gate, args.diff)
    elif args.approve:
        cmd_approve(gate, args.approve, args.notes)
    elif args.reject:
        cmd_reject(gate, memory, args.reject, args.reason)
    elif args.rollback:
        cmd_rollback(gate, memory, args.rollback, args.reason)
    elif args.interactive:
        cmd_interactive(gate, memory)
    else:
        cmd_list(gate)


if __name__ == "__main__":
    main()
