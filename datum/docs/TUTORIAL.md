# DATUM — New Shop User Tutorial
### From zero to running your first document analysis

---

## What is DATUM?

DATUM is a three-part mission control system that automatically reads, verifies, and repairs your shop documents and code — then asks *you* to approve every change before it lands.

| System | What it does |
|--|--|
| **FORGE** | Reads your documents (PDFs, Word files, Markdown, text). Verifies technical claims. Repairs gaps and outdated info. Commits clean docs to Git. |
| **ANVIL** | Reads your codebase. Compares code against FORGE-verified docs. Flags where code and docs disagree. Proposes patches. |
| **PANEL** | Your command center at http://localhost:4000. Every FORGE repair and ANVIL patch lands here for your review before anything is committed. |

Nothing gets committed without your approval in PANEL. You are always in the loop.

---

## Part 1: Start the System

### First-time start

Open a PowerShell terminal in `c:\Users\zach\.openclaw\workspace\datum\` and run:

```powershell
.\datum-start.ps1 -Action up
```

This builds the images (first time: ~2 min), starts all four services, and opens PANEL in your browser automatically.

### Start options

```powershell
.\datum-start.ps1 -Action up        # Start everything
.\datum-start.ps1 -Action stop      # Stop everything
.\datum-start.ps1 -Action status    # Show running containers
.\datum-start.ps1 -Action logs      # Stream all logs
.\datum-start.ps1 -Action errors    # Show only errors (saved to logs/datum-errors.log)
.\datum-start.ps1 -Action rebuild   # Full rebuild (after changing requirements.txt)

# Target one service:
.\datum-start.ps1 -Action logs    -Service forge
.\datum-start.ps1 -Action restart -Service anvil
```

---

## Part 2: Run FORGE on a Specific Directory

FORGE can process documents two ways: by dropping files into the staging area, or by pointing it at a specific folder with a one-shot command.

### Method A — Drop files into the staging folder (recommended for new shops)

The staging folder is watched in real-time by the WATCHER service.
Any supported file you drop in gets auto-queued for FORGE.

**Staging folder location (on your Windows host):**
```
c:\Users\zach\.openclaw\workspace\datum\datum\forge\staging\
```

**What to drop:**
```
.md   .txt   .pdf   .docx   .html   .rst
```

**Step by step:**
1. Open the staging folder in Explorer: `Explorer "c:\Users\zach\.openclaw\workspace\datum\datum\forge\staging"`
2. Copy your shop documents (SOPs, work instructions, quality procedures, machine specs) into that folder
3. Watch PANEL at http://localhost:4000 — items appear in the **Review Queue** within seconds
4. Review and approve each proposed change

### Method B — One-shot run on a specific directory

Use this when you want to analyze a folder once without the watcher loop.

```powershell
# From outside Docker (runs in the container):
docker exec datum-forge-1 python forge.py --path /app/forge --once

# To target a custom directory mounted into the container, add a volume in docker-compose.yml:
# volumes:
#   - C:\YourShop\Documents:/app/custom_docs
# Then run:
docker exec datum-forge-1 python forge.py --path /app/custom_docs --once
```

**One-shot flags:**
```powershell
--once          # Run one full loop and exit (no daemon)
--dry-run       # Analyze only — show what would change, commit nothing
--domain cnc_machining   # Override domain detection (see Domains section)
--config /app/forge/config/forge.yaml   # Custom config file
```

### Method C — Mount a Windows folder and run

To point FORGE at any folder on your Windows machine without copying files:

1. **Edit `docker-compose.yml`** — add the folder as a named volume under the forge service:
   ```yaml
   forge:
     volumes:
       - C:\YourShop\Docs:/app/shop_docs   # Your Windows path : Container path
   ```

2. **Recreate the container:**
   ```powershell
   docker compose up -d --force-recreate forge
   ```

3. **Trigger a one-shot run:**
   ```powershell
   docker exec datum-forge-1 python forge.py --path /app/shop_docs --once
   ```

4. **Watch PANEL** for the review queue to fill up.

---

## Part 3: The PANEL Review Queue

Open http://localhost:4000 in your browser.

### Dashboard overview

```
┌─────────────────────────────────────────────────┐
│  PANEL  ·  FORGE ● ONLINE  ·  ANVIL ● ONLINE    │
├──────────┬──────────┬──────────┬─────────────────┤
│  Pending │  Docs OK │  Issues  │  Quality Trend  │
│    3     │   142    │    1     │  ↑ 84→87        │
└──────────┴──────────┴──────────┴─────────────────┘
```

### Reviewing a FORGE repair

Every time FORGE repairs a document, a card appears in the queue:

1. Click the card — full before/after diff opens
2. Read the **AI Reasoning tab** to see *why* FORGE changed it
3. Check the **Changes tab** — specific lines highlighted
4. Choose:
   - **Approve** — FORGE commits the repaired doc to Git
   - **Reject** — Original is kept, decision is logged
   - **Suggest** — Add your own correction in the text box

### Batch approvals

For a high volume of routine fixes:
1. Check the boxes on multiple queue items
2. Click **Approve Selected** at the top of the queue
3. All selected items commit in one Git commit

---

## Part 4: Configure a New Shop Domain

A "domain" tells FORGE what type of documents to expect and what quality standards to apply.

### 1. Create a domain config file

Copy the template:
```powershell
Copy-Item "c:\Users\zach\.openclaw\workspace\datum\datum\forge\domains\racing_app.yaml" `
          "c:\Users\zach\.openclaw\workspace\datum\datum\forge\domains\my_shop.yaml"
```

Edit `my_shop.yaml`:
```yaml
name: "my_shop"
display_name: "My Shop SOPs"
description: "Standard Operating Procedures for the machine shop floor"

# What to look for when auto-detecting this domain
detection_patterns:
  - "SOP"
  - "work instruction"
  - "machine setup"
  - "quality check"
  - "CNC"

# Expected document sections
required_sections:
  - "Purpose"
  - "Scope"
  - "Procedure"
  - "Safety"

# Quality thresholds (0-100)
quality_thresholds:
  min_score: 75
  completeness_weight: 0.30
  accuracy_weight:     0.35
  clarity_weight:      0.20
  currency_weight:     0.15
```

### 2. Register it in forge.yaml

Open `c:\Users\zach\.openclaw\workspace\datum\datum\forge\config\forge.yaml` and add your domain:

```yaml
domains:
  available:
    - "manufacturing"
    - "cnc_machining"
    - "my_shop"        # <-- add this line
```

### 3. Restart FORGE

```powershell
docker compose restart forge
```

FORGE now auto-detects documents matching your domain patterns and applies the correct quality rules.

---

## Part 5: Run ANVIL on a Specific Codebase

ANVIL compares your code against FORGE-verified documents. To point it at a specific project:

### 1. Add the repo to anvil.yaml

Open `c:\Users\zach\.openclaw\workspace\datum\datum\anvil\config\anvil.yaml`:

```yaml
repositories:
  - id: "my-project"
    name: "My Project"
    path: "/app/repos/my-project"   # Container path
    languages: ["python", "javascript"]
    doc_binding:
      - domain: "my_shop"
        doc_patterns: ["**/*.md", "**/*.txt"]
```

### 2. Mount the repo in docker-compose.yml

```yaml
anvil:
  volumes:
    - C:\Code\my-project:/app/repos/my-project
```

### 3. Run ANVIL once

```powershell
docker exec datum-anvil-1 python anvil.py --once --repo my-project
```

Results appear in the PANEL **Review Queue** under source "ANVIL".

---

## Part 6: Read Logs and Errors

### Live log stream

```powershell
# All services
.\datum-start.ps1 -Action logs

# One service
.\datum-start.ps1 -Action logs -Service forge
.\datum-start.ps1 -Action logs -Service anvil
.\datum-start.ps1 -Action logs -Service panel
.\datum-start.ps1 -Action logs -Service watcher
```

### Error report (saved to file)

```powershell
.\datum-start.ps1 -Action errors
# Output saved to: c:\Users\zach\.openclaw\workspace\datum\datum\logs\datum-errors.log
```

### What healthy logs look like

```
forge-1  | 19:46:36 [INFO]  forge.ai_client: Ollama online — 6 models: ...
forge-1  | 19:46:37 [INFO]  forge.pull: Git pull OK — already up to date
forge-1  | 19:46:38 [INFO]  forge.verify: 0 docs queued for verification
forge-1  | 19:46:38 [INFO]  forge.loop: ✓ Loop complete — next run in 3600s

anvil-1  | 19:46:37 [INFO]  forge.ai_client: Ollama online — 6 models: ...
anvil-1  | Next run in 3600s...
```

### Common warnings and what to do

| Warning | Meaning | Fix |
|--|--|--|
| `Ollama not reachable: localhost:11434` | Container using wrong host | Check `base_url` in forge.yaml — must be `host.docker.internal:11434` |
| `Repo path not found: ../wecr8-rag` | Repo configured but not mounted | Mount the repo or remove it from anvil.yaml |
| `BrokenPipeError` in panel | Browser closed mid-request | Normal — not an error |
| `Git pull failed` | Repo not a git repo or no remote | Set `git.enabled: false` in forge.yaml for local-only use |

---

## Part 7: One-Shot Quick Reference

**Process one file immediately:**
```powershell
# Copy the file into staging
Copy-Item "C:\MyDocs\SOP-001.docx" `
  "c:\Users\zach\.openclaw\workspace\datum\datum\forge\staging\"
# WATCHER picks it up instantly — check PANEL
```

**Run FORGE on a folder, see what it would change (no commits):**
```powershell
docker exec datum-forge-1 python forge.py --path /app/forge --once --dry-run
```

**Check system health:**
```powershell
.\datum-start.ps1 -Action status
Invoke-WebRequest http://localhost:4000/health -UseBasicParsing | Select-Object Content
```

**Open PANEL:**
```powershell
Start-Process "http://localhost:4000"
```

**Stop everything:**
```powershell
.\datum-start.ps1 -Action stop
```

---

## Appendix: File Locations

| What | Where |
|--|--|
| Drop documents for FORGE | `datum\datum\forge\staging\` |
| Processed originals | `datum\datum\forge\processed\` |
| FORGE repair reports | `datum\datum\forge\reports\` |
| FORGE state / memory | `datum\datum\forge\forge_state.json` |
| Decision ledger | `datum\datum\forge\logs\decisions.ndjson` |
| ANVIL review queue | `datum\datum\anvil\review_queue\` |
| Error log (after running errors action) | `datum\datum\logs\datum-errors.log` |
| FORGE config | `datum\datum\forge\config\forge.yaml` |
| ANVIL config | `datum\datum\anvil\config\anvil.yaml` |
| Domain definitions | `datum\datum\forge\domains\` |
| Docker compose | `datum\datum\docker-compose.yml` |
| Windows launcher | `datum\datum-start.ps1` |
