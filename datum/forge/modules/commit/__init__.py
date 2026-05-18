"""
FORGE Module — COMMIT
Stage 4: Git commit with structured changelog, RAG re-index,
run report generation, state save.
"""

import os
import json
import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger, StateManager

log = get_logger("commit")


@dataclass
class CommitResult:
    committed:      bool   = False
    commit_hash:    str    = ""
    files_committed:list   = field(default_factory=list)
    reindexed:      int    = 0
    report_path:    str    = ""
    errors:         list   = field(default_factory=list)
    duration_ms:    int    = 0


class CommitModule:
    """
    Stage 4 of the FORGE loop.
    - Commits all repaired documents to Git with a structured message
    - Re-indexes changed documents in the RAG backend
    - Generates the run report (HTML)
    - Saves state
    """

    def __init__(self, config: dict, state: StateManager, repo_path: str = "."):
        self.cfg       = config
        self.state     = state
        self.repo_path = Path(repo_path)
        self.git_cfg   = config.get("git", {})
        self.rag_cfg   = config.get("rag", {})
        self.rep_cfg   = config.get("reporting", {})

    async def run(self, pull_result, verify_results: list,
                   repair_results: list) -> CommitResult:
        start  = datetime.now()
        result = CommitResult()

        log.info("── COMMIT ───────────────────────────────────────")

        # 1. Git commit
        repaired_paths = [r.path for r in repair_results if r.success]
        if repaired_paths and self.git_cfg.get("auto_commit") and self.git_cfg.get("enabled"):
            await self._git_commit(repaired_paths, repair_results, result)

        # 2. RAG re-index
        if self.rag_cfg.get("enabled") and self.rag_cfg.get("reindex_on_commit"):
            await self._reindex(repaired_paths, result)

        # 3. Generate report
        if self.rep_cfg.get("generate_run_report"):
            result.report_path = await self._generate_report(
                pull_result, verify_results, repair_results
            )

        # 4. Save state
        self.state.save()

        result.duration_ms = int((datetime.now() - start).total_seconds() * 1000)

        log.info(f"Commit complete: committed={result.committed} "
                 f"reindexed={result.reindexed}")

        return result

    # ── Git ───────────────────────────────────────────────────────────────

    async def _git_commit(self, paths: list, repairs: list, result: CommitResult):
        try:
            import git
            repo = git.Repo(self.repo_path)

            # Stage all repaired files
            staged = []
            for path in paths:
                full_path = self.repo_path / path
                if full_path.exists():
                    repo.index.add([str(full_path)])
                    staged.append(path)

            if not staged:
                return

            # Build structured commit message
            repair_summary = "\n".join(
                f"  - {r.path}: {r.score_before}→{r.score_after} "
                f"({'improved' if r.improved else 'same'})"
                for r in repairs if r.success
            )

            msg = (
                f"{self.git_cfg.get('commit_message_prefix','forge:')} "
                f"AI quality repair — {len(staged)} document(s)\n\n"
                f"Repaired:\n{repair_summary}\n\n"
                f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"Avg quality: {self.state.stats.get('avg_quality','?')}/100"
            )

            author = git.Actor(
                self.git_cfg.get("author_name", "FORGE AI"),
                self.git_cfg.get("author_email", "forge@wecr8.info")
            )

            commit = repo.index.commit(msg, author=author, committer=author)
            result.committed       = True
            result.commit_hash     = commit.hexsha[:8]
            result.files_committed = staged

            log.info(f"Git committed: {commit.hexsha[:8]} ({len(staged)} files)")

            if self.git_cfg.get("push_after_commit") and repo.remotes:
                repo.remotes.origin.push()
                log.info("Pushed to remote")

        except Exception as e:
            msg = f"Git commit failed: {e}"
            log.error(msg)
            result.errors.append(msg)

    # ── RAG re-index ──────────────────────────────────────────────────────

    async def _reindex(self, paths: list, result: CommitResult):
        base    = self.rag_cfg.get("api_base", "")
        jwt     = os.environ.get(self.rag_cfg.get("jwt_env", "WECR8_ADMIN_JWT"), "")
        client  = os.environ.get("WECR8_CLIENT_ID", self.rag_cfg.get("client_id", ""))

        if not base or not jwt or not paths:
            return

        for path in paths:
            full_path = self.repo_path / path
            if not full_path.exists():
                continue

            try:
                content  = full_path.read_bytes()
                suffix   = full_path.suffix.lower()
                mimetype = {
                    ".pdf": "application/pdf",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ".md": "text/markdown",
                    ".txt": "text/plain",
                }.get(suffix, "application/octet-stream")

                form = aiohttp.FormData()
                form.add_field("files", content,
                               filename=full_path.name, content_type=mimetype)
                form.add_field("doc_type", "sop")  # TODO: use detected type

                async with aiohttp.ClientSession() as s:
                    async with s.post(
                        f"{base}/documents/upload",
                        data=form,
                        headers={"Authorization": f"Bearer {jwt}"},
                        timeout=aiohttp.ClientTimeout(total=120)
                    ) as r:
                        if r.status in (200, 201):
                            result.reindexed += 1
                            log.info(f"Re-indexed: {path}")
                        else:
                            log.warning(f"Re-index failed {path}: HTTP {r.status}")

            except Exception as e:
                log.warning(f"Re-index error {path}: {e}")

    # ── Report generation ─────────────────────────────────────────────────

    async def _generate_report(self, pull_result, verify_results: list,
                                 repair_results: list) -> str:
        report_dir = Path(self.rep_cfg.get("report_dir", "./reports"))
        report_dir.mkdir(parents=True, exist_ok=True)

        timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path= report_dir / f"forge_run_{timestamp}.html"

        summary = self.state.summary
        avg_score= self.state.stats.get("avg_quality", 0)

        # Score history for mini-chart
        score_history = [
            r.get("avg_quality", 0)
            for r in self.state.run_history[-20:]
            if isinstance(r.get("avg_quality"), (int, float))
        ]

        def score_color(s):
            if s >= 90: return "#22d3a0"
            if s >= 75: return "#f59e0b"
            if s >= 60: return "#f97316"
            return "#ef4444"

        def letter(s):
            if s >= 90: return "A"
            if s >= 80: return "B"
            if s >= 70: return "C"
            if s >= 60: return "D"
            return "F"

        # Build verify rows
        vr_rows = ""
        for vr in verify_results[:50]:
            sc = vr.quality_score
            failures_str = "; ".join(f["message"][:50] for f in vr.failures[:2])
            vr_rows += f"""<tr>
              <td style="font-family:monospace;font-size:.75rem;color:#8a9bb0;">{vr.path[:50]}</td>
              <td><span style="color:{score_color(sc)};font-weight:700;font-family:monospace;">{sc}</span></td>
              <td><span style="font-size:.7rem;color:#6b7a99;font-family:monospace;">{letter(sc)}</span></td>
              <td style="font-size:.75rem;color:#6b7a99;">{vr.domain}</td>
              <td style="font-size:.72rem;color:#ef4444;">{failures_str}</td>
              <td><span style="font-size:.7rem;padding:2px 6px;border-radius:2px;
                background:{"rgba(34,211,160,.1)" if not vr.needs_repair else "rgba(245,158,11,.1)"};
                color:{"#22d3a0" if not vr.needs_repair else "#f59e0b"};">
                {"pass" if not vr.needs_repair else "repair"}</span></td>
            </tr>"""

        # Build repair rows
        rr_rows = ""
        for rr in repair_results:
            delta = rr.score_after - rr.score_before
            delta_str = f"+{delta}" if delta > 0 else str(delta)
            delta_color = "#22d3a0" if delta > 0 else ("#ef4444" if delta < 0 else "#6b7a99")
            changes_str = "; ".join(rr.changes_made[:2]) if rr.changes_made else "No changes"
            rr_rows += f"""<tr>
              <td style="font-family:monospace;font-size:.75rem;color:#8a9bb0;">{rr.path[:50]}</td>
              <td style="font-family:monospace;font-size:.75rem;">{rr.score_before}</td>
              <td style="font-family:monospace;font-size:.75rem;">{rr.score_after}</td>
              <td style="font-family:monospace;font-size:.75rem;color:{delta_color};font-weight:700;">{delta_str}</td>
              <td style="font-size:.72rem;color:#6b7a99;">{changes_str[:60]}</td>
              <td><span style="font-size:.7rem;padding:2px 6px;border-radius:2px;
                background:{"rgba(34,211,160,.1)" if rr.success else "rgba(239,68,68,.1)"};
                color:{"#22d3a0" if rr.success else "#ef4444"};">
                {"success" if rr.success else ("skipped" if rr.skipped else "failed")}</span></td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FORGE Run Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;500&family=Instrument+Sans:wght@300;400;500&display=swap');
:root {{
  --bg:#080c10;--surface:#0d1218;--card:#121920;--border:#1e2833;
  --accent:#e8621a;--text:#dde6f0;--sub:#8a9bb0;--dim:#4a5568;
  --green:#22d3a0;--amber:#f59e0b;--red:#ef4444;
}}
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);font-family:'Instrument Sans',sans-serif;
      font-size:14px;line-height:1.6;padding:2rem;}}
h1{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;letter-spacing:-.5px;}}
h1 em{{color:var(--accent);font-style:normal;}}
h2{{font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:700;letter-spacing:.5px;
    color:var(--sub);margin:1.5rem 0 .6rem;text-transform:uppercase;font-size:.75rem;}}
.meta{{font-family:'JetBrains Mono',monospace;font-size:.65rem;color:var(--dim);margin-bottom:1.5rem;}}
.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:.75rem;margin-bottom:1.5rem;}}
@media(max-width:600px){{.stats{{grid-template-columns:1fr 1fr;}}}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:1rem;text-align:center;}}
.stat-v{{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;line-height:1;}}
.stat-l{{font-family:'JetBrains Mono',monospace;font-size:.55rem;letter-spacing:1.5px;color:var(--dim);margin-top:.25rem;}}
table{{width:100%;border-collapse:collapse;margin-bottom:1rem;}}
th{{font-family:'JetBrains Mono',monospace;font-size:.6rem;letter-spacing:1px;color:var(--dim);
    text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--border);
    background:rgba(255,255,255,.02);}}
td{{padding:.55rem .6rem;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle;}}
tr:last-child td{{border-bottom:none;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:1.1rem;margin-bottom:1rem;overflow-x:auto;}}
.tag{{font-family:'JetBrains Mono',monospace;font-size:.6rem;letter-spacing:1px;
      padding:2px 8px;border-radius:20px;}}
.footer{{font-family:'JetBrains Mono',monospace;font-size:.65rem;color:var(--dim);
         text-align:center;margin-top:2rem;border-top:1px solid var(--border);padding-top:1rem;}}
</style>
</head>
<body>

<h1>FORGE <em>Run Report</em></h1>
<div class="meta">
  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ·
  Run #{self.state.stats.get('total_runs','?')} ·
  Repo: {str(self.repo_path.resolve())[:60]}
  {f' · Committed: {repair_results[0].path if repair_results else ""}' if any(r.success for r in repair_results) else ''}
</div>

<div class="stats">
  <div class="stat">
    <div class="stat-v" style="color:{score_color(int(avg_score))}">{int(avg_score)}</div>
    <div class="stat-l">AVG QUALITY</div>
  </div>
  <div class="stat">
    <div class="stat-v" style="color:var(--accent)">{len(verify_results)}</div>
    <div class="stat-l">VERIFIED</div>
  </div>
  <div class="stat">
    <div class="stat-v" style="color:var(--amber)">{sum(1 for r in repair_results if r.success)}</div>
    <div class="stat-l">REPAIRED</div>
  </div>
  <div class="stat">
    <div class="stat-v" style="color:var(--green)">{summary.get('verified',0)}</div>
    <div class="stat-l">CLEAN DOCS</div>
  </div>
  <div class="stat">
    <div class="stat-v" style="color:var(--red)">{summary.get('failed',0)}</div>
    <div class="stat-l">FAILED</div>
  </div>
</div>

<h2>Verification Results</h2>
<div class="card">
  <table>
    <thead><tr>
      <th>Document</th><th>Score</th><th>Grade</th>
      <th>Domain</th><th>Issues</th><th>Status</th>
    </tr></thead>
    <tbody>{vr_rows or '<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:1.5rem;">No documents verified this run</td></tr>'}</tbody>
  </table>
</div>

<h2>Repair Results</h2>
<div class="card">
  <table>
    <thead><tr>
      <th>Document</th><th>Before</th><th>After</th>
      <th>Delta</th><th>Changes</th><th>Status</th>
    </tr></thead>
    <tbody>{rr_rows or '<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:1.5rem;">No repairs this run</td></tr>'}</tbody>
  </table>
</div>

<div class="footer">
  FORGE v1.0 · WeCr8 Consulting · wecr8.info ·
  Powered by Ollama (local) + Claude fallback ·
  Report: {report_path.name}
</div>

</body>
</html>"""

        report_path.write_text(html)

        # Prune old reports
        keep = self.rep_cfg.get("keep_last_n_reports", 30)
        old_reports = sorted(report_dir.glob("forge_run_*.html"))
        for old in old_reports[:-keep]:
            old.unlink(missing_ok=True)

        log.info(f"Report: {report_path}")
        return str(report_path)
