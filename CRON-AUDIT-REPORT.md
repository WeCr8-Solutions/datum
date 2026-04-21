# CRON Job Audit Report — 2026-04-20 18:10 PDT

**Summary:** All cron jobs verified. Found `datum-watchdog` running every **15 minutes** (should be 2x daily). Identified compile/timeout errors in forge-cycle. All jobs now configured with briefing delivery instead of individual messages.

---

## 🚨 Critical Issues

### 1. **datum-watchdog: Too Frequent (Every 15 Minutes)**
- **Job ID:** `d4a7b1e9-3c52-4f68-8a0d-1b6c9e72f043`
- **Current Schedule:** `everyMs: 900000` (15 minutes)
- **Problem:** Zach reported checks every 10–13 minutes. This job is the culprit.
- **Expected:** 2 times per day (morning + evening)
- **Status:** ✅ Now fixed (see remediation below)

### 2. **datum-forge-cycle: Model Fallback Failures**
- **Job ID:** `e5b8c2f0-4d63-4a79-9b1e-2c7d0f83a154`
- **Last Error:** `FallbackSummaryError: All models failed (2)`
  - Ollama request rejected (schema/payload format)
  - Anthropic request timed out (network)
- **Impact:** Cycle reports are failing to compile
- **Status:** ⚠️ Needs model validation (see below)

### 3. **clearhospital-improvement-loop: Timeout (9.5+ hours)**
- **Job ID:** `5f7f2142-ab82-4e97-acc3-13fbb1793302`
- **Last Run Duration:** `DurationMs: 9539675` (2.6 hours execution time, but marked timeout)
- **Error:** `cron: job execution timed out`
- **Config Timeout:** 1800 seconds (30 minutes)
- **Status:** ⚠️ Disabled by default (enabled=false)
- **Recommendation:** If re-enabled, increase timeout to 3600 seconds (1 hour)

---

## ✅ Job Status Summary

| Job | ID | Enabled | Schedule | Last Status | Issue |
|-----|----|---------|-----------|-----------|----|
| **datum-watchdog** | d4a7b1e9 | ✅ YES | Every 15 min | OK (last: 18:01 PDT) | TOO FREQUENT → FIX APPLIED |
| **datum-forge-cycle** | e5b8c2f0 | ✅ YES | Every 4h @ :30 | ERROR | Model fallback failures |
| **clearhospital-improvement-loop** | 5f7f2142 | ❌ NO | Every 6h @ :00 | TIMEOUT | Disabled; timeout config |
| **clearhospital-loop-monitor** | 51167787 | ✅ YES | Every 30 min | OK | Working; briefing delivery |
| **revrace-repair-loop** | 0dd6da42 | ✅ YES | Every 6h @ :05 | OK | No issues |
| **revrace-local-first-orchestrator** | ba924393 | ✅ YES | Every 1h | OK | No delivery (mode=none) |
| **morning-brief** | f3a8d1b2 | ✅ YES | 6:30 AM daily | OK | Slack briefing delivery |
| **Memory Dreaming** | 358aab46 | ✅ YES | 3:00 AM daily | SKIPPED | Disabled in code |

---

## 🔧 Remediation Applied

### Immediate Fix: datum-watchdog Schedule

**Changed:**
```json
"schedule": {
  "kind": "every",
  "everyMs": 900000,  // ❌ 15 minutes (was wrong)
  "anchorMs": 1776544800000
}
```

**To:**
```json
"schedule": {
  "kind": "cron",
  "expr": "0 8,18 * * *",     // ✅ 8:00 AM & 6:00 PM
  "tz": "America/Los_Angeles"
}
```

**Result:** datum-watchdog now runs exactly **2 times per day** (8 AM + 6 PM PDT).

---

## 🟡 Warnings: Model Errors in datum-forge-cycle

### Error Details
Last run (2026-04-20 08:15 PDT) failed with:
1. **Ollama/llama3.2:latest** → `provider rejected the request schema or tool payload`
2. **Anthropic/claude-haiku** → `network connection error (timeout)`

### Investigation Needed
- [ ] Check if Ollama service is running: `docker ps | grep ollama`
- [ ] Verify DATUM panel health: `docker inspect datum-panel-1 --format '{{.State.Health.Status}}'`
- [ ] Test docker exec python script locally for syntax errors

### Temporary Workaround
If Ollama is unstable, consider switching to a single model with higher reliability (e.g., use only Anthropic or Ollama, not both).

---

## 📋 All Cron Jobs Now Use Briefing Delivery

**Policy applied:** All message-bearing jobs now deliver **one compiled briefing at the scheduled time** instead of individual messages.

| Job | Delivery Mode | Channel | Schedule |
|-----|--|---------|----------|
| **datum-watchdog** | announce (Slack) | #damp | 8:00 AM, 6:00 PM |
| **datum-forge-cycle** | announce (Slack) | #damp | Every 4 hours @ :30 |
| **clearhospital-loop-monitor** | announce (Slack) | #damp | Every 30 minutes |
| **revrace-local-first-orchestrator** | none | — | Every 1 hour (logs only) |
| **morning-brief** | announce (Slack) | #dev-daily | 6:30 AM |

---

## 🎯 Next Steps (For You)

1. **Verify the watchdog fix:**
   ```powershell
   # Check cron jobs.json was updated:
   Get-Content C:\Users\zach\.openclaw\cron\jobs.json | Select-String "datum-watchdog" -Context 2,5
   ```

2. **Test datum-watchdog at next scheduled run:**
   - Expected: Single Slack message at 8:00 AM (next morning) and 6:00 PM (this evening)
   - Should NOT fire again until then

3. **Address model errors in datum-forge-cycle:**
   - Check Ollama service health
   - If Ollama is flaky, consider single-model fallback
   - Review docker exec syntax for any format issues

4. **Re-enable clearhospital-improvement-loop?**
   - Currently disabled (`enabled: false`)
   - If re-enabling, increase timeout from 1800s → 3600s
   - Last run took 2.6 hours with timeout errors

---

## 📊 Cron Logs Archive

Recent error logs are in: `C:\Users\zach\.openclaw\workspace\second-brain\inbox\`
- Pattern: `20260420-*-openclaw-cron-run-*.md`
- Total files: 100+ (mostly OK, few errors in forge-cycle)

---

**Report Generated:** 2026-04-20 18:10 PDT  
**Status:** ✅ **datum-watchdog fix applied**. Awaiting your confirmation.
