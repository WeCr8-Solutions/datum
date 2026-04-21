# DATUM Status Check Configuration

**Issue:** Status checks running every 10-13 minutes instead of 2 times per day

**Solution:** Configure DATUM to run status checks at specific times only

---

## Recommended Schedule

### Option 1: Morning & Evening (Recommended)
- **Morning:** 8:00 AM PDT
- **Evening:** 6:00 PM PDT

This gives you daily coverage with minimal overhead.

### Option 2: Business Day Only
- **Start of day:** 9:00 AM PDT
- **Mid-day:** 12:00 PM PDT

Use if you only care about business hours.

### Option 3: 24-Hour Coverage
- **Morning:** 6:00 AM PDT
- **Evening:** 6:00 PM PDT

Use if DATUM runs 24/7 and you want balanced coverage.

---

## How to Disable Frequent Checks

### In `.env` (DATUM Configuration)

Add or update:

```env
# DATUM Status Check Configuration
DATUM_STATUS_CHECK_ENABLED=true
DATUM_STATUS_CHECK_INTERVAL=43200000  # 12 hours (milliseconds)
DATUM_STATUS_CHECK_TIMES=["08:00", "18:00"]  # 8 AM and 6 PM
DATUM_HEALTH_CHECK_VERBOSE=false      # Reduce log noise
```

### In Your OpenClaw Agent

If you're running status checks from this agent, update `HEARTBEAT.md`:

```markdown
# HEARTBEAT.md - Keep empty to disable periodic checks

# Status checks are now scheduled via OpenClaw cron
# See: DATUM-STATUS-CHECK-CONFIG.md for schedule details
```

---

## How to Set Up Cron-Based Checks (Recommended)

Use OpenClaw's native cron system for precise scheduling:

### Create `.openclaw/cron/datum-status-check.yml`:

```yaml
schedule:
  id: datum-status-checks
  name: "DATUM Status Check"
  description: "Check DATUM service health 2 times daily"
  
  # Cron expression: Run at 8 AM and 6 PM every day
  cron: "0 8,18 * * *"  # 8:00 AM and 6:00 PM UTC
  
  # Or in PDT (adjust for your timezone):
  # cron: "0 15,1 * * *"  # 3:00 PM and 1:00 AM UTC = 8:00 AM and 6:00 PM PDT
  
  timezone: "America/Los_Angeles"
  
  agent: "datum-main"
  
  task: |
    Check DATUM service health (FORGE, ANVIL, PANEL).
    Report status: up/down, latency, last sync time.
    Alert if any service is degraded.
  
  output:
    channel: slack
    to: "#damp"
    
  onSuccess: |
    Services healthy ✅
  
  onFailure: |
    Service alert triggered 🚨
```

---

## Manual Cron Commands (If Using CLI)

### List current cron jobs:
```bash
openclaw cron list
```

### Add DATUM status check:
```bash
openclaw cron add \
  --id datum-status-2x-daily \
  --schedule "0 8,18 * * *" \
  --timezone "America/Los_Angeles" \
  --agent datum-main \
  --task "Check DATUM health"
```

### Remove frequent checks:
```bash
openclaw cron remove datum-frequent-checks
# or
openclaw cron remove --older-than 60m  # Remove checks older than 60 minutes
```

### Disable all status checks:
```bash
openclaw cron disable datum-status-checks
```

---

## Expected Behavior After Fix

### Before:
- ❌ Status messages every 10-13 minutes
- ❌ Clutters Slack with frequent updates
- ❌ Wastes API calls
- ❌ Hard to spot real issues in noise

### After:
- ✅ Status checks at 8:00 AM & 6:00 PM only
- ✅ Clean, predictable notifications
- ✅ Minimal overhead
- ✅ Easy to see anomalies
- ✅ You know exactly when to expect updates

---

## Testing the Configuration

Once you've updated the config:

1. **Verify cron is configured correctly:**
   ```bash
   openclaw cron list | grep datum
   ```

2. **Force a manual check to test:**
   ```bash
   openclaw cron run datum-status-2x-daily
   ```

3. **Check logs for next scheduled run:**
   ```bash
   openclaw cron logs datum-status-2x-daily
   ```

4. **Wait for next scheduled time and verify message appears in Slack**

---

## If Issue Persists

### Check for duplicate tasks:

```bash
# Look for multiple DATUM health check tasks
openclaw cron list | grep -i datum

# Check agent heartbeat
cat C:\Users\zach\.openclaw\workspace\datum\HEARTBEAT.md
```

### Clear all status checks and reconfigure:

```bash
# Remove all DATUM cron jobs
openclaw cron remove --pattern "datum*"

# Re-add with specific schedule
openclaw cron add \
  --id datum-status-2x-daily \
  --schedule "0 8,18 * * *" \
  --timezone "America/Los_Angeles" \
  --agent datum-main \
  --task "DATUM health check"
```

### Contact OpenClaw support:
If the issue continues, it may be a background daemon running independently. Check:
```bash
openclaw status
openclaw gateway status
```

---

## Summary

**What you asked for:** Status checks 2 times per day only  
**What's happening:** Checks running every 10-13 minutes  
**Why:** Likely a cron job or heartbeat running too frequently  
**Fix:** Configure cron to run at 8:00 AM & 6:00 PM PDT only  
**Time to fix:** 5 minutes  

**Next step:** Apply the cron configuration above and let me know if the frequency stops.

---

**Created:** April 20, 2026  
**Status:** Configuration Template Ready  
**By:** OpenClaw Assistant
