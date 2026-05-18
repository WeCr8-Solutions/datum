#!/usr/bin/env python3
"""
PANEL — Production Agent Notification & Execution Layer
Server: serves the UI, API, handles decisions, email alerts.

Usage:
  python3 server.py
  python3 server.py --port 4000 --forge ../forge --anvil ../anvil

PM2:
  pm2 start server.py --interpreter python3 --name panel
"""

import os, sys, json, time, smtplib, ssl, threading, argparse, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Config ─────────────────────────────────────────────────
PORT          = int(os.environ.get("PANEL_PORT", 4000))
FORGE_PATH    = Path(os.environ.get("FORGE_PATH", "../forge"))
ANVIL_PATH    = Path(os.environ.get("ANVIL_PATH", "../anvil"))
REVIEWER_NAME = os.environ.get("PANEL_REVIEWER_NAME", "Zach")
REVIEWER_EMAIL= os.environ.get("PANEL_REVIEWER_EMAIL", "")
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASS     = os.environ.get("SMTP_PASS", "")
FROM_EMAIL    = os.environ.get("FROM_EMAIL", "panel@wecr8.info")
BASE_URL      = os.environ.get("PANEL_BASE_URL", f"http://localhost:{PORT}")

# Derived paths
def forge_review():  return FORGE_PATH / "review_queue"
def anvil_review():  return ANVIL_PATH / "review_queue"
def forge_state():   return FORGE_PATH / "forge_state.json"
def anvil_state():   return ANVIL_PATH / "anvil_state.json"
def forge_ledger():  return FORGE_PATH / "logs" / "decisions.ndjson"
def anvil_ledger():  return ANVIL_PATH / "logs" / "decisions.ndjson"
def forge_memory():  return FORGE_PATH / "memory" / "repair_patterns.json"

DECISIONS_FILE = Path("./panel_decisions.json")
NOTIFIED_FILE  = Path("./panel_notified.json")
FEATURES_FILE  = Path("./panel_features.json")


# ── State helpers ───────────────────────────────────────────
def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text()) if Path(p).exists() else (default or {})
    except Exception:
        return default or {}

def save_json(p, data):
    Path(p).write_text(json.dumps(data, indent=2))

def load_decisions(): return load_json(DECISIONS_FILE, {})
def save_decisions(d): save_json(DECISIONS_FILE, d)
def load_features():
    defaults = dict(auto_approve=False, web_search=True, email_notify=True,
                    anvil_patch=True, dry_run=False)
    return {**defaults, **load_json(FEATURES_FILE, {})}


# ── Item loading ────────────────────────────────────────────
def load_review_dir(directory: Path, source: str) -> list:
    items = []
    if not directory.exists(): return items
    for f in sorted(directory.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text())
            if d.get("status") != "pending": continue
            items.append(_normalize_item(d, source, str(f)))
        except Exception: pass
    return items

def _normalize_item(d: dict, source: str, fpath: str) -> dict:
    created = d.get("created_at","")
    return {
        "id":           d.get("id", Path(fpath).stem),
        "source":       source,
        "title":        _title(d, source),
        "priority":     d.get("priority","medium"),
        "file":         d.get("doc_path",""),
        "doc":          d.get("doc_path",""),
        "age":          _time_ago(created),
        "created_at":   created,
        "issue_type":   d.get("issue_type",""),
        "itar_flagged": d.get("itar_flagged", False),
        "doc_quality":  d.get("score_before",0),
        "confidence":   d.get("confidence","medium"),
        "plain_explanation": _plain(d, source),
        "issues":       _issues(d),
        "content_before": (d.get("content_before","") or "")[:3000],
        "content_after":  (d.get("content_after","")  or "")[:3000],
        "diff":           (d.get("diff","")            or "")[:4000],
        "ai_reasoning":   _reasoning(d, source),
        "summary":        d.get("summary",""),
        "thread":         d.get("thread") or [],
        "pattern_history":d.get("pattern_history") or [],
        "changes_made":   d.get("changes_made") or [],
        "_source_file":   fpath,
    }

def _title(d, source):
    s = (d.get("summary","") or "").splitlines()
    if s: return s[0][:80]
    p = d.get("doc_path","")
    return f"{'Document' if source=='forge' else 'Code'} update: {Path(p).name}" if p else "AI change needs review"

def _plain(d, source):
    p = d.get("doc_path","")
    fname = f"<strong>{Path(p).name}</strong>" if p else "a file"
    sb = d.get("score_before",0); sa = d.get("score_after",0)
    q = f" Quality improved from {sb} to {sa}." if sb and sa and sa != sb else ""
    itar = " <strong>⚠ ITAR-sensitive — confirm authorization before approving.</strong>" if d.get("itar_flagged") else ""
    if source == "forge":
        return f"FORGE improved {fname}.{q} Take a look and approve if it reads correctly.{itar}"
    itype = d.get("issue_type","")
    if itype == "contradiction":
        return f"ANVIL found a disagreement between the documentation and the code in {fname}. It proposed a fix. Review before it goes live.{itar}"
    return f"ANVIL proposed a code change in {fname} based on your documentation.{itar}"

def _issues(d):
    out = []
    for f in (d.get("failures_fixed") or [])[:3]:
        if f: out.append({"severity":"error","message":str(f)})
    for c in (d.get("changes_made") or [])[:2]:
        if c: out.append({"severity":"info","message":str(c)})
    for w in (d.get("warnings") or [])[:2]:
        if w: out.append({"severity":"warning","message":str(w)})
    return out[:5]

def _reasoning(d, source):
    src = source.upper()
    itype = d.get("issue_type","")
    conf = d.get("confidence","unknown")
    score = d.get("score_before","—")
    descriptions = {
        "contradiction":"A value in the code contradicted the verified documentation.",
        "missing_impl":"The documentation describes a feature with no corresponding code.",
        "drift":"The documentation was updated since the code was last verified.",
        "repair":"A quality improvement was identified in the document.",
    }
    desc = descriptions.get(itype, "A quality improvement was identified.")
    return f"<strong>{src} decision:</strong> {desc} Confidence: {conf}. Doc quality at check: {score}."

def _time_ago(iso):
    if not iso: return ""
    try:
        s = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
        if s < 120:   return "Just now"
        if s < 3600:  return f"{int(s/60)} min ago"
        if s < 86400: return f"{int(s/3600)} hrs ago"
        return f"{int(s/86400)} days ago"
    except Exception: return ""


# ── System state ────────────────────────────────────────────
def load_system_state() -> dict:
    fs = load_json(forge_state(), {})
    as_ = load_json(anvil_state(), {})
    fstats = fs.get("stats", {})
    runs = fs.get("run_history", [])
    quality_trend = [r.get("avg_quality",0) for r in runs[-10:] if isinstance(r.get("avg_quality"),float)]

    return {
        "forge_online":       forge_state().exists(),
        "forge_status":       "FORGE",
        "forge_last_run":     fstats.get("last_run","—"),
        "forge_docs":         len(fs.get("documents",{})),
        "forge_repairs":      fstats.get("total_repairs",0),
        "forge_total_runs":   fstats.get("total_runs",0),
        "anvil_online":       anvil_state().exists(),
        "anvil_status":       "ANVIL",
        "anvil_last_run":     as_.get("last_audit_time","—"),
        "anvil_files":        as_.get("runs",[{}])[-1:][0].get("files_scanned",0) if as_.get("runs") else 0,
        "anvil_issues":       as_.get("runs",[{}])[-1:][0].get("issues",0) if as_.get("runs") else 0,
        "anvil_patches":      as_.get("runs",[{}])[-1:][0].get("patches",0) if as_.get("runs") else 0,
        "avg_quality":        round(fstats.get("avg_quality",0),1),
        "last_run":           _time_ago(fstats.get("last_run","")),
        "forge_quality_trend": quality_trend,
        "anvil_issue_trend":  [],
    }

def load_activity(n=50) -> list:
    items = []
    for ledger in [forge_ledger(), anvil_ledger()]:
        if not ledger.exists(): continue
        src = "forge" if "forge" in str(ledger) else "anvil"
        lines = ledger.read_text().strip().split("\n")
        for line in lines[-n:]:
            try:
                d = json.loads(line)
                items.append({
                    "time":   _fmt_time(d.get("timestamp","")),
                    "source": src,
                    "type":   d.get("decision_type",""),
                    "file":   Path(d.get("doc_path","")).name if d.get("doc_path") else "",
                    "score":  d.get("score_after",""),
                    "icon":   _act_icon(d.get("decision_type","")),
                })
            except Exception: pass
    items.sort(key=lambda x: x.get("time",""), reverse=True)
    return items[:n]

def _fmt_time(iso):
    try: return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception: return ""

def _act_icon(t):
    return {"repair_accepted":"📝","passed_verification":"✅","contradiction":"⚠️",
            "held_for_review":"📋","approved_by_human":"✅","rejected_by_human":"✗",
            "committed":"💾","pattern_learned":"🧠","missing_impl":"❓"}.get(t,"📡")

def load_clients() -> list:
    # Build client list from state files
    clients = []
    fs = load_json(forge_state(), {})
    docs = fs.get("documents", {})
    # Group by inferred client
    client_id = os.environ.get("WECR8_CLIENT_ID", "local")
    client_name = os.environ.get("CLIENT_NAME", "Local Shop")
    pending = len([d for d in docs.values() if d.get("status") == "pending"])
    indexed = len([d for d in docs.values() if d.get("status") == "indexed"])
    avg_q   = fs.get("stats",{}).get("avg_quality",0)
    clients.append({
        "id":          client_id,
        "name":        client_name,
        "tier":        os.environ.get("CLIENT_TIER","starter"),
        "online":      True,
        "pending":     pending,
        "doc_quality": round(avg_q,0),
        "docs_indexed":indexed,
        "queries_week":0,
        "open_ncrs":   0,
    })
    return clients

def load_patterns() -> dict:
    decisions = load_decisions()
    approved  = sum(1 for d in decisions.values() if d.get("action") == "approve")
    rejected  = sum(1 for d in decisions.values() if d.get("action") == "reject")
    patterns  = load_json(forge_memory(), {})
    return {
        "total_approved": approved,
        "total_rejected": rejected,
        "learned":        len(patterns),
        "fix_types":      [],
        "approval_rates": [],
        "doc_gaps":       [],
    }


# ── Decision application ────────────────────────────────────
def apply_decision(item_id: str, source: str, action: str, note: str, reviewer: str):
    review_dir = forge_review() if source == "forge" else anvil_review()
    qfile = review_dir / f"{item_id}.json"
    if qfile.exists():
        try:
            d = json.loads(qfile.read_text())
            d["status"]      = {"approve":"approved","reject":"rejected","suggest":"pending"}[action]
            d["reviewed_by"] = reviewer
            d["reviewed_at"] = datetime.now().isoformat()
            d["review_notes"]= note
            if note and action == "suggest":
                d.setdefault("thread",[]).append({"from":"human","text":note,"time":_fmt_time(datetime.now().isoformat())})
            qfile.write_text(json.dumps(d, indent=2))
        except Exception as e: print(f"Error applying decision: {e}")

    if action == "reject":
        try:
            d = json.loads(qfile.read_text()) if qfile.exists() else {}
            before = d.get("content_before","")
            path   = d.get("doc_path","")
            if path and before:
                for candidate in [FORGE_PATH/path, ANVIL_PATH/path, Path(path)]:
                    if candidate.exists():
                        candidate.write_text(before, encoding="utf-8")
                        print(f"Restored: {candidate}")
                        break
        except Exception as e: print(f"Restore error: {e}")


# ── Email ───────────────────────────────────────────────────
def send_email(items: list):
    if not REVIEWER_EMAIL or not SMTP_USER or not items: return
    count   = len(items)
    urgent  = [i for i in items if i.get("priority") in ("urgent","high")]
    subject = f"{'⚡ URGENT: ' if urgent else ''}PANEL — {count} item{'s' if count>1 else ''} need your review"

    rows = "".join(f"""
      <tr><td style="padding:14px;border-bottom:1px solid #2a3548;">
        <div style="font-size:11px;color:#f5a623;font-weight:700;text-transform:uppercase;margin-bottom:3px;">
          {i.get('priority','').upper()} · {i.get('source','').upper()}
        </div>
        <div style="font-size:15px;font-weight:600;color:#e8edf5;margin-bottom:4px;">{i.get('title','')}</div>
        <div style="font-size:12px;color:#7a8ba8;">{i.get('file','')}</div>
      </td></tr>""" for i in items[:5])

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f1319;font-family:Helvetica,Arial,sans-serif;">
<div style="max-width:560px;margin:0 auto;padding:24px 16px;">
  <div style="background:#161c26;border:1px solid #2a3548;border-top:3px solid #f5a623;border-radius:6px;overflow:hidden;">
    <div style="padding:20px 24px 14px;border-bottom:1px solid #2a3548;">
      <div style="font-size:11px;letter-spacing:2px;color:#3d4f6a;text-transform:uppercase;margin-bottom:5px;">WeCr8 PANEL</div>
      <div style="font-size:20px;font-weight:700;color:#e8edf5;">{count} item{'s' if count>1 else ''} waiting for your review</div>
      <div style="font-size:13px;color:#7a8ba8;margin-top:5px;">{'Some are marked urgent. ' if urgent else ''}Please review when you have a moment.</div>
    </div>
    <table style="width:100%;border-collapse:collapse;">{rows}</table>
    <div style="padding:18px 24px;text-align:center;background:#0f1319;">
      <a href="{BASE_URL}" style="display:inline-block;background:#f5a623;color:#0f1319;
         padding:11px 26px;border-radius:5px;font-weight:700;font-size:14px;text-decoration:none;">
        Open PANEL →
      </a>
      <div style="font-size:11px;color:#3d4f6a;margin-top:10px;">Approve, reject, or ask questions directly in the browser.</div>
    </div>
  </div>
  <div style="text-align:center;font-size:10px;color:#3d4f6a;margin-top:14px;">WeCr8 Consulting · wecr8.info · PANEL Review System</div>
</div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"WeCr8 PANEL <{FROM_EMAIL}>"
        msg["To"]      = REVIEWER_EMAIL
        msg.attach(MIMEText(html, "html"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls(context=ctx); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(FROM_EMAIL, REVIEWER_EMAIL, msg.as_string())
        print(f"📧 Email sent to {REVIEWER_EMAIL}")
    except Exception as e: print(f"Email error (non-fatal): {e}")

def notify_loop():
    while True:
        try:
            notified = load_json(NOTIFIED_FILE, {})
            all_items = load_review_dir(forge_review(),"forge") + load_review_dir(anvil_review(),"anvil")
            new = [i for i in all_items if i["id"] not in notified]
            if new:
                features = load_features()
                if features.get("email_notify", True):
                    send_email(new)
                for i in new: notified[i["id"]] = datetime.now().isoformat()
                save_json(NOTIFIED_FILE, notified)
        except Exception as e: print(f"Notify error: {e}")
        time.sleep(300)


# ── HTTP Server ─────────────────────────────────────────────
class PANELHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args): pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type,Authorization")

    def json_resp(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.send_header("Connection","close")
        self.cors()
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def html_resp(self, path: Path):
        c = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(c))
        self.send_header("Connection","close")
        self.end_headers()
        self.wfile.write(c)
        self.wfile.flush()
        self.close_connection = True

    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/","/index.html"):
            panel = Path(__file__).parent / "panel.html"
            if panel.exists(): self.html_resp(panel)
            else: self.json_resp({"error":"panel.html not found"},404)
        elif p == "/api/review/queue":   self._queue()
        elif p == "/api/docs":           self._docs()
        elif p == "/api/docs/content":   self._doc_content()
        elif p == "/health":             self.json_resp({"status":"ok","time":datetime.now().isoformat()})
        else:                            self.json_resp({"error":"not found"},404)

    def _docs(self):
        fs   = load_json(forge_state(), {})
        docs = fs.get("documents", {})
        result = []
        for path, meta in docs.items():
            result.append({
                "path":         path,
                "name":         Path(path).name,
                "domain":       meta.get("domain", "—"),
                "doc_type":     meta.get("doc_type", "—"),
                "quality_score":meta.get("quality_score", 0),
                "status":       meta.get("status", "—"),
                "last_verified":meta.get("last_verified", ""),
                "last_repaired":meta.get("last_repaired", ""),
                "itar_sensitive":meta.get("itar_sensitive", False),
                "chunk_count":  meta.get("chunk_count", 0),
            })
        result.sort(key=lambda d: d.get("quality_score", 0) or 0, reverse=True)
        self.json_resp({"docs": result, "total": len(result)})

    def _doc_content(self):
        from urllib.parse import parse_qs
        qs       = parse_qs(urlparse(self.path).query)
        rel_path = (qs.get("path") or [""])[0]
        if not rel_path:
            self.json_resp({"error": "missing path"}, 400)
            return
        # Only serve files that live under FORGE_PATH to prevent path traversal
        forge_resolved = FORGE_PATH.resolve()
        candidates = [FORGE_PATH / rel_path, FORGE_PATH / "docs" / rel_path]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if (str(resolved).startswith(str(forge_resolved))
                        and resolved.exists() and resolved.is_file()):
                    content = resolved.read_text(encoding="utf-8", errors="replace")
                    self.json_resp({"path": rel_path, "content": content, "size": len(content)})
                    return
            except Exception:
                pass
        self.json_resp({"error": "file not found"}, 404)

    def do_POST(self):
        p    = urlparse(self.path).path
        ln   = int(self.headers.get("Content-Length",0))
        body = json.loads(self.rfile.read(ln)) if ln else {}
        if p == "/api/review/decide":          self._decide(body)
        elif p.startswith("/api/system/run/"): self._trigger(p)
        elif p == "/api/system/feature":       self._feature(body)
        else:                                  self.json_resp({"error":"not found"},404)

    def _queue(self):
        decisions = load_decisions()
        forge_items = load_review_dir(forge_review(),"forge")
        anvil_items = load_review_dir(anvil_review(),"anvil")
        all_items   = forge_items + anvil_items

        pending   = [i for i in all_items if i["id"] not in decisions]
        completed = []
        for iid, dec in list(decisions.items())[-30:]:
            item = next((i for i in all_items if i["id"]==iid),None)
            if item: completed.append({**item,**dec,"status":"done"})
            else: completed.append({"id":iid,"title":"Completed item",**dec,"status":"done"})

        self.json_resp({
            "pending":   pending,
            "completed": completed,
            "activity":  load_activity(50),
            "clients":   load_clients(),
            "patterns":  load_patterns(),
            "system":    load_system_state(),
        })

    def _decide(self, body):
        iid    = body.get("item_id","")
        action = body.get("action","")
        note   = body.get("note","")
        reviewer=body.get("reviewed_by",REVIEWER_NAME)
        if not iid or action not in ("approve","reject","suggest"):
            self.json_resp({"error":"invalid"},400); return
        source = "anvil" if (anvil_review()/f"{iid}.json").exists() else "forge"
        apply_decision(iid,source,action,note,reviewer)
        decisions = load_decisions()
        decisions[iid] = {"action":action,"note":note,"reviewed_by":reviewer,
                          "reviewed_at":datetime.now().strftime("%b %d %H:%M"),
                          "timestamp":datetime.now().isoformat(),"source":source}
        save_decisions(decisions)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {action.upper()}: {iid} ({source})")
        self.json_resp({"success":True})

    def _trigger(self, path):
        system = path.split("/")[-1]
        script = (FORGE_PATH/"forge.py") if system=="forge" else (ANVIL_PATH/"anvil.py")
        if script.exists():
            import subprocess
            subprocess.Popen([sys.executable,str(script),"--once"],
                             cwd=str(script.parent),
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            print(f"▶ Triggered {system.upper()} run")
        self.json_resp({"success":True,"system":system})

    def _feature(self, body):
        features = load_features()
        features[body.get("key","")] = body.get("value",False)
        save_json(FEATURES_FILE, features)
        self.json_resp({"success":True})


# ── Main ────────────────────────────────────────────────────
def main():
    global FORGE_PATH, ANVIL_PATH

    parser = argparse.ArgumentParser(description="PANEL Server")
    parser.add_argument("--port",  type=int, default=PORT)
    parser.add_argument("--forge", default=str(FORGE_PATH))
    parser.add_argument("--anvil", default=str(ANVIL_PATH))
    args = parser.parse_args()
    FORGE_PATH = Path(args.forge)
    ANVIL_PATH = Path(args.anvil)

    threading.Thread(target=notify_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), PANELHandler)
    print(f"\n⬛ PANEL — Production Agent Notification & Execution Layer")
    print(f"   URL:     http://localhost:{args.port}")
    print(f"   FORGE:   {FORGE_PATH}")
    print(f"   ANVIL:   {ANVIL_PATH}")
    print(f"   Email:   {REVIEWER_EMAIL or 'not configured'}")
    print(f"\nOpen http://localhost:{args.port} in your browser\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
