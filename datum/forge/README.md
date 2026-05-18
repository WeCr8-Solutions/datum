# FORGE
### Framework for Ongoing Repository Growth & Enhancement

A modular, self-repairing, self-improving, self-verifying document intelligence loop.
Domain-agnostic. Ollama-primary (local, ITAR-safe, zero API cost). Git-backed.

```
Documents → PULL → VERIFY → REPAIR → COMMIT → loop
               ↑                          |
               └──────── Git/State ───────┘
```

---

## What it does

Every loop run:

1. **PULL** — Git pull, scan for new/changed files, ingest staging directory, build priority queue
2. **VERIFY** — Auto-detect domain, run rule-based checks, AI quality scoring (0-100), optional web enrichment for technical verification
3. **REPAIR** — AI rewrites only what fails, self-verifies the repair improved the score, generates a full diff, falls back if worse
4. **COMMIT** — Git commit with structured changelog, re-index into RAG backend, generate HTML run report, save state

---

## Setup

### 1. Install Ollama (required)
```bash
# macOS / Linux
curl -fsSL https://ollama.com/install.sh | sh

# Pull required models
ollama pull llama3.1          # Main reasoning model
ollama pull mistral           # Fast classification
ollama pull nomic-embed-text  # Embeddings
```

### 2. Install Python deps
```bash
pip install -r requirements.txt
```

### 3. Configure
```bash
cp config/forge.yaml config/local.yaml
# Edit config/local.yaml with your paths and settings
```

Key settings in `config/forge.yaml`:
```yaml
loop:
  interval_seconds: 3600     # Run every hour
  dry_run: false             # Set true to analyze without writing

ai:
  fallback:
    enabled: true            # Allow Claude as fallback
    # Set false for full ITAR/air-gap mode

git:
  auto_pull: true
  auto_commit: true
  push_after_commit: false   # Set true if you want to push to remote

rag:
  enabled: true
  api_base: "http://localhost:3001/api"
```

### 4. Run
```bash
# One-time run (see what it finds)
python3 forge.py --once --dry-run --path ./your-docs

# Continuous loop
python3 forge.py --path ./your-docs

# Check status
python3 forge.py --status

# Process a single file
python3 forge.py --file ./docs/sop-001.md --once
```

### 5. Production deploy (pm2)
```bash
pm2 start forge.py --interpreter python3 --name forge -- --path ./your-docs
pm2 start watcher.py --interpreter python3 --name forge-watcher
pm2 save && pm2 startup
```

---

## Adding a domain

Domains define what "correct" looks like for a document type. Create a YAML file in `domains/`:

```yaml
# domains/medical_devices.yaml
domain:
  id: "medical_devices"
  name: "Medical Device Documentation"
  aliases: ["fda", "510k", "medical"]

detection:
  keywords:
    strong: ["FDA", "510(k)", "IFU", "predicate device", "substantial equivalence"]
    weak: ["clinical", "sterile", "biocompatibility"]

document_types:
  ifu:
    name: "Instructions for Use"
    required_sections: ["Intended Use", "Warnings", "Instructions", "Contraindications"]
    style_guide: "medical_ifu"

verification_rules:
  safety:
    - id: "warnings_present"
      description: "IFUs must have warnings and contraindications"
      severity: "error"
      trigger_keywords: ["use", "procedure"]
      required_content: ["WARNING", "CONTRAINDICATION", "Do not use"]

repair_prompts:
  base: |
    You are a medical device technical writer specializing in FDA-compliant documentation.
    Follow 21 CFR Part 801 and ISO 15223 standards.
```

FORGE will auto-detect and use this domain for any document that matches.

---

## File structure

```
forge/
├── forge.py                  ← Main loop orchestrator (run this)
├── watcher.py                ← Real-time file watcher
├── requirements.txt
├── config/
│   └── forge.yaml            ← Master config
├── core/
│   ├── ai_client.py          ← Ollama + Claude unified AI client
│   └── logger.py             ← Logger, StateManager, WebSearch
├── modules/
│   ├── pull/                 ← Stage 1: Git sync + file intake
│   ├── verify/               ← Stage 2: Quality scoring + rule checks
│   ├── repair/               ← Stage 3: AI repair + self-verification
│   └── commit/               ← Stage 4: Git commit + RAG reindex + report
├── domains/
│   ├── manufacturing.yaml    ← CNC/machining domain rules
│   ├── general.yaml          ← Universal fallback domain
│   └── [your-domain].yaml   ← Add your own
├── staging/                  ← Drop files here to queue them
├── processed/                ← Originals moved here after staging
├── reports/                  ← HTML run reports
└── logs/
```

---

## AI cost model

| Mode | Cost | Use case |
|------|------|----------|
| Ollama only | $0 | ITAR, air-gap, cost-sensitive |
| Ollama + Claude fallback | ~$0.01–$0.05/doc | Standard, best quality |
| Claude only | ~$0.05–$0.15/doc | No local GPU available |

Ollama handles 95%+ of work. Claude fallback is only triggered when:
- Ollama is unavailable
- Task is in `fallback.use_for` list (e.g., `web_enrichment`)

---

## Quality scoring

Each document gets a 0-100 score weighted across:

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| Accuracy | 30% | Technical claims correct, verified against web sources |
| Completeness | 25% | All required sections present for this doc type |
| Format | 20% | Follows domain style standards |
| Clarity | 15% | Readable by the target audience |
| Currency | 10% | Up to date, no obsolete references |

Documents below 75 are queued for repair. Errors (safety violations, missing required content) trigger immediate repair regardless of score.

---

## Integration with WeCr8 stack

FORGE connects to the WeCr8 RAG backend via the `/api/documents/upload` endpoint.
After repairing a document, FORGE re-indexes it so operators immediately get
better answers from the improved version.

```yaml
# config/forge.yaml
rag:
  enabled: true
  api_base: "http://localhost:3001/api"
  jwt_env: "WECR8_ADMIN_JWT"
  reindex_on_commit: true
```

The full pipeline:
```
Operator asks question → RAG can't answer well
→ FORGE detects low-confidence queries (via digest service)
→ FORGE drafts missing SOP (via sop_gap_draft.py)
→ Admin reviews and approves
→ Uploads to staging/
→ FORGE ingests, verifies, repairs if needed
→ FORGE commits and re-indexes
→ Operator gets accurate answer next time they ask
```

---

*FORGE — WeCr8 Consulting · wecr8.info · contact@wecr8.info*
