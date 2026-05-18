# ANVIL
### Automated Node for Verifying Implementation against Literature

Doc-driven code verification and repair agent. Companion to FORGE.

**FORGE improves your documents. ANVIL uses those documents as the spec to verify and repair your code.**

```
FORGE (verified docs) → ANVIL reads → validates code → repairs code → reports
     ↑                                                         |
     └───────── FORGE ledger ←──────── ANVIL writes findings ─┘
```

---

## What it detects

**Contradictions** — Code does X, doc says Y.
> Doc: "Timeout must be 30 seconds." Code: `timeout = 60`

**Missing implementations** — Doc describes a feature with no corresponding code.
> Doc: "The system must log all NCR dispositions." No logging code found.

**Drift** — Doc was updated by FORGE since code was last verified.
> FORGE repaired SOP-001 yesterday. Code that implements SOP-001 hasn't been re-verified.

---

## The FORGE relationship

ANVIL reads three things from FORGE:
1. **Quality scores** — only uses docs FORGE scored ≥ 70. Low-quality docs = unreliable specs.
2. **Verified content** — the actual doc text that FORGE improved and committed.
3. **Decision ledger** — knows when FORGE last touched each doc (triggers drift detection).

ANVIL writes one thing back to FORGE:
- Findings in the shared decision ledger (FORGE can see what ANVIL found).

They share:
- Agent memory (learned patterns and failures)
- Human review gate (same queue, same CLI)
- AI client (same Ollama-primary, Claude-fallback setup)

---

## Setup

### Prerequisites
- FORGE installed and has run at least once (needs verified docs)
- Ollama running with `llama3.1`, `mistral`, `nomic-embed-text`

### Install
```bash
pip install -r requirements.txt
```

### Configure
```bash
cp config/anvil.yaml config/local.yaml
```

Key settings:
```yaml
forge:
  docs_path: "../forge/docs"       # Where FORGE keeps verified docs
  state_file: "../forge/forge_state.json"
  min_doc_quality_score: 70        # Only use docs scored ≥ this

repositories:
  - id: "my-project"
    path: "../my-project"
    languages: ["python", "typescript"]
```

### Run
```bash
# Dry run — see what ANVIL would find without changing anything
python3 anvil.py --once --dry-run

# Full run
python3 anvil.py --once

# Continuous loop (runs every hour)
python3 anvil.py

# Target one repo
python3 anvil.py --repo my-project --once
```

### Production (pm2)
```bash
pm2 start anvil.py --interpreter python3 --name anvil -- --config ./config/anvil.yaml
pm2 save
```

### Review pending patches
```bash
# Uses the same review CLI as FORGE
python3 ../forge/review.py               # List queue
python3 ../forge/review.py --interactive # Work through queue
python3 ../forge/review.py --approve ID
python3 ../forge/review.py --reject ID --reason "Changed wrong values"
```

---

## Pipeline stages

```
SCAN  → Walk configured repos, parse code into CodeUnits (language-agnostic)
BIND  → Map CodeUnits to DocSections using embeddings + keyword similarity
AUDIT → Check each binding for contradictions, missing impls, drift
PATCH → Generate minimal targeted fixes for auto-fixable issues
COMMIT → Git commit patches, submit others for human review
```

---

## Language support

| Language | File types | Parser |
|----------|-----------|--------|
| Python | .py | AST-based (full) |
| JavaScript | .js, .jsx, .mjs | Regex + pattern |
| TypeScript | .ts, .tsx | Regex + pattern |
| SQL | .sql | Pattern-based |
| Go | .go | Pattern-based |
| YAML/JSON | .yaml, .yml, .json | Structure-aware |
| HTML/CSS | .html, .css | Generic fallback |
| Any other | * | Generic (whole-file unit) |

Adding a language: create a class in `parsers/__init__.py` extending `BaseParser`.

---

## File structure

```
anvil/
├── anvil.py                 ← Main orchestrator (run this)
├── requirements.txt
├── config/
│   └── anvil.yaml           ← Master config
├── core/
│   ├── types.py             ← Shared data structures
│   ├── forge_bridge.py      ← Read FORGE docs and state
│   └── logger.py            ← (symlink or copy from FORGE)
├── parsers/
│   └── __init__.py          ← Language parser plugins
├── modules/
│   ├── scan/                ← Stage 1: Code ingestion
│   ├── bind/                ← Stage 2: Doc-to-code mapping
│   ├── audit/               ← Stage 3: Issue detection
│   └── patch/               ← Stage 4: Fix generation
├── review_queue/            ← Patches awaiting human approval
├── reports/                 ← HTML run reports
└── logs/
    └── decisions.ndjson     ← Decision ledger (shared with FORGE)
```

---

## Cron schedule example

```bash
# /etc/cron.d/anvil
# Run FORGE first (doc improvement), then ANVIL (code verification)

# FORGE: hourly doc improvement
0 * * * *  user  cd /opt/wecr8/forge  && python3 forge.py --once >> /var/log/forge.log 2>&1

# ANVIL: every 2 hours, offset by 30 min (runs after FORGE)
30 */2 * * *  user  cd /opt/wecr8/anvil  && python3 anvil.py --once >> /var/log/anvil.log 2>&1

# Review digest: daily summary of pending reviews
0 8 * * *  user  cd /opt/wecr8/forge && python3 review.py 2>&1 | mail -s "ANVIL Review Queue" admin@shop.com
```

---

## How FORGE and ANVIL work together

```
Monday morning:
  6:00  FORGE runs — improves SOP-014 (coolant selection)
        FORGE scores it 88/100, commits to docs repo
        FORGE ledger: "repair_accepted: docs/sop-014.md"

  6:30  ANVIL runs — detects SOP-014 was updated
        ANVIL finds binding: sop-014.md → src/quality/ncr_triage.py
        ANVIL audits: NCR triage code has 48hr timeout, SOP says 24hr
        ANVIL generates patch: timeout = 86400 → timeout = 43200
        Syntax validated ✓, self-check: "Correct, minimal change"
        ANVIL commits patch to feature branch
        ANVIL writes to shared ledger: "contradiction fixed: ncr_triage.py"

  8:00  Quality manager runs: python3 review.py
        Sees patch queued, reviews diff, approves
        Patch merged to main
```

---

*ANVIL — WeCr8 Consulting · wecr8.info · contact@wecr8.info*
*Companion to FORGE — Framework for Ongoing Repository Growth & Enhancement*
