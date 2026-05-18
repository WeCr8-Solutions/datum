# DATUM
### The Reference Point for Manufacturing Intelligence

**FORGE · ANVIL · PANEL**

In manufacturing, a datum is the fixed reference point — the surface, edge, or feature that every measurement, every tolerance, and every part feature is located from. Without it, nothing is in the right place.

That's what this system is. The fixed reference point for your shop's documentation, your code, and your quality system.

---

## The three systems

```
┌─────────────────────────────────────────────────────────────────┐
│                            DATUM                                │
│                                                                 │
│   ┌──────────────┐    ┌──────────────┐    ┌─────────────────┐  │
│   │    FORGE     │───▶│    ANVIL     │    │     PANEL       │  │
│   │              │    │              │◀───│                 │  │
│   │  Reads your  │    │  Reads FORGE │    │  Your command   │  │
│   │  documents.  │    │  docs as the │    │  center. Every  │  │
│   │  Verifies &  │    │  spec. Finds │    │  AI change      │  │
│   │  repairs     │    │  where code  │    │  reviewed and   │  │
│   │  them.       │    │  disagrees.  │    │  approved here. │  │
│   │  Commits.    │    │  Fixes it.   │    │                 │  │
│   └──────────────┘    └──────────────┘    └─────────────────┘  │
│                                                                 │
│              Shared: decision ledger · agent memory             │
│              review gate · task queue · pattern library         │
└─────────────────────────────────────────────────────────────────┘
```

**FORGE** — Framework for Ongoing Repository Growth & Enhancement.
Reads your shop documents — SOPs, quality manuals, setup sheets, machine manuals, MSDS sheets. Verifies them against domain rules (AS9100, safety requirements, format standards). Repairs missing sections, incorrect values, and format violations. Commits improved versions. Runs hourly.

**ANVIL** — Automated Node for Verifying Implementation against Literature.
Uses FORGE-verified documents as the specification. Scans your codebase in any language. Binds code units to doc sections using embeddings. Finds contradictions (code does X, doc says Y), missing implementations, and drift (doc updated, code not re-verified). Generates minimal targeted fixes. Runs every two hours.

**PANEL** — Production Agent Notification & Execution Layer.
The human command center. Dashboard, review queue, ITAR gates, conversation threads, batch actions, client overview, pattern reports, system controls. Every AI change from FORGE and ANVIL lands here for your review before it goes live.

---

## Quickstart

### 1. Prerequisites

```bash
# Ollama (local AI — primary, ITAR-safe, zero API cost)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1
ollama pull mistral
ollama pull nomic-embed-text

# PM2 (recommended for production)
npm install -g pm2
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — minimum: CLIENT_NAME, CLIENT_ID, JWT_SECRET
```

### 3. Install & start

```bash
./start.sh --setup    # First time: install deps, create directories
./start.sh            # Start everything
```

### 4. Open PANEL

```
http://localhost:4000
```

---

## Production

```bash
# Start with PM2 (auto-restart, survives reboots)
pm2 start ecosystem.config.js
pm2 save && pm2 startup

# Monitor
pm2 list
pm2 logs datum-forge
pm2 logs datum-anvil
pm2 logs datum-panel

# Stop
pm2 stop all
```

---

## Per-client install

Each shop gets its own isolated DATUM instance:

```bash
# 1. Copy repository
cp -r datum acme-machine-co && cd acme-machine-co

# 2. Configure
cp .env.example .env
# Edit: CLIENT_NAME, CLIENT_ID, database credentials

# 3. Seed admin
python3 forge/scripts/seed.py \
  --client "Acme Machine Co" \
  --id acme-machine-co \
  --admin admin@acme.com \
  --password "Secure2024!"

# 4. Ingest documents
python3 forge/scripts/ingest.py \
  --dir ./client-docs \
  --client acme-machine-co

# 5. Start
./start.sh
```

---

## Directory structure

```
datum/
│
├── README.md                   ← You are here
├── start.sh                    ← One command to start everything
├── ecosystem.config.js         ← PM2 process configuration
├── requirements.txt            ← All Python dependencies
├── .env.example                ← Copy to .env and configure
├── .gitignore
│
├── forge/                      ← Document intelligence
│   ├── forge.py                ← Main loop (run this or use start.sh)
│   ├── review.py               ← CLI review tool
│   ├── watcher.py              ← Real-time file watcher
│   ├── config/forge.yaml       ← FORGE configuration
│   ├── core/                   ← AI client, ledger, memory, review gate, task queue
│   ├── modules/                ← pull · verify · repair · commit · cross_doc
│   ├── domains/                ← manufacturing.yaml · general.yaml · (add your own)
│   ├── staging/                ← Drop documents here to queue them
│   └── review_queue/           ← Documents awaiting your approval in PANEL
│
├── anvil/                      ← Code verification
│   ├── anvil.py                ← Main loop
│   ├── config/anvil.yaml       ← ANVIL configuration
│   ├── core/                   ← Types, FORGE bridge
│   ├── modules/                ← scan · bind · audit · patch
│   ├── parsers/                ← Language plugins (Python, JS/TS, SQL, Go, ...)
│   └── review_queue/           ← Code patches awaiting your approval in PANEL
│
├── panel/                      ← Human command center
│   ├── panel.html              ← The entire browser interface (single file)
│   └── server.py               ← API server + email notifications
│
├── shared/                     ← Cross-system utilities
│   ├── config/
│   ├── docs/
│   └── scripts/
│
└── logs/                       ← All system logs (auto-created)
```

---

## How the systems connect

All three systems share infrastructure without sharing a database:

| Shared component | Location | Used by |
|-----------------|----------|---------|
| Decision ledger | `forge/logs/decisions.ndjson` | FORGE writes · ANVIL reads · PANEL reads |
| Agent memory | `forge/memory/` | FORGE writes · ANVIL reads |
| Review queues | `forge/review_queue/` `anvil/review_queue/` | FORGE/ANVIL write · PANEL reads |
| Task queue | `forge/forge_tasks.db` | FORGE |

ANVIL reads from FORGE but never writes to it directly. PANEL reads from both and routes your decisions back through the review gate. Nothing is tightly coupled — any system can be replaced or upgraded independently.

---

## AI cost model

| Mode | Monthly cost | Use case |
|------|-------------|----------|
| Ollama only (local) | $0 | ITAR shops, air-gap, cost-sensitive |
| Ollama + Claude fallback | ~$5–$40 | Standard — best quality |
| Cloud only (no Ollama) | ~$20–$100 | No local GPU available |

Default is Ollama for 95%+ of work. Claude is fallback only, configurable per task type.

---

## Default schedule

| Process | Runs | What it does |
|---------|------|-------------|
| datum-forge | Every hour | Verify and repair documents |
| datum-anvil | Every 2 hours | Verify code against documents |
| datum-panel | Always on | Serve PANEL, send email alerts |
| datum-watcher | Always on | Pick up files dropped in `forge/staging/` |

---

## Adding a domain (FORGE)

Drop a YAML file in `forge/domains/`. FORGE picks it up automatically:

```yaml
# forge/domains/aerospace.yaml
domain:
  id: "aerospace"
  name: "Aerospace Manufacturing"

detection:
  keywords:
    strong: ["AS9100", "ITAR", "DCSA", "first article", "FAIR"]

document_types:
  fair:
    name: "First Article Inspection Report"
    required_sections: ["Part Information", "Ballooned Drawing", "Results", "Approval"]

repair_prompts:
  base: |
    You are an aerospace manufacturing documentation specialist...
```

---

## Adding a language parser (ANVIL)

Add a class to `anvil/parsers/__init__.py`:

```python
class RustParser(BaseParser):
    language = "rust"
    extensions = [".rs"]

    def parse(self, file_path, content):
        # extract fn, struct, impl blocks
        ...
```

---

*DATUM — Document and code intelligence for manufacturing.*
*Built for shop floors. Trusted by the people who run them.*
