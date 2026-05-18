# PANEL
### Production Agent Notification & Execution Layer

The command center for FORGE and ANVIL. Every change the AI systems make lands here for your review. One screen. Every tool you need.

---

## What's in PANEL

**Dashboard** — System health at a glance. FORGE and ANVIL status, quality score trends, pending items, recent activity feed.

**Review Queue** — Every AI change waiting for your approval. Filter by source, priority, or ITAR flag. Batch approve routine items. Full conversation thread per item with AI reasoning, before/after content, and diff view.

**Clients** — One row per shop you serve. Doc quality, docs indexed, query volume, open NCRs.

**Patterns & Reports** — What the AI has learned and where. Approval rates by source, most common fix types, document gap report.

**Decision History** — Every decision you've made, auditable, with your notes.

**System Controls** — Start/stop FORGE and ANVIL runs, toggle features, view the role/permission matrix ready for when you add team members.

---

## Review item features

Each item in the queue has:
- **Plain English explanation** — what happened, in one readable paragraph
- **Before / After / Changes tabs** — see exactly what changed
- **AI Reasoning tab** — why the AI made this decision (confidence, doc quality, binding score)
- **Conversation Thread** — back-and-forth history between you and the AI
- **Pattern History** — past decisions on similar items
- **ITAR gate** — checkbox confirmation required before approving sensitive items
- **Quick reply chips** — one-tap common responses ("Keep original value", "Doc may be outdated")
- **Batch selection** — check multiple items, approve or reject all at once

---

## Setup

```bash
# Environment
export FORGE_PATH="../forge"
export ANVIL_PATH="../anvil"
export PANEL_REVIEWER_NAME="Zach"
export PANEL_REVIEWER_EMAIL="zach@wecr8.info"

# Email notifications (optional)
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="your@gmail.com"
export SMTP_PASS="your-app-password"
export FROM_EMAIL="panel@wecr8.info"

# Start
python3 server.py

# Open
http://localhost:4000
```

```bash
# PM2 (production)
pm2 start server.py --interpreter python3 --name panel \
  -- --port 4000 --forge ../forge --anvil ../anvil
pm2 save
```

---

## Role matrix (ready for team expansion)

| Role | Review | Approve ITAR | System Controls |
|------|--------|-------------|-----------------|
| Owner | All | ✓ | ✓ |
| Quality Manager | All | ✓ with PIN | ✗ |
| Shop Reviewer | Non-ITAR | ✗ | ✗ |
| Read-Only | ✗ | ✗ | ✗ |

---

## The full system

```
FORGE  (hourly)    → improves documents     → sends to PANEL queue
ANVIL  (2-hourly)  → verifies code          → sends to PANEL queue
PANEL  (always on) → review queue + alerts  → your decisions flow back
```

---

*PANEL — WeCr8 Consulting · wecr8.info*
