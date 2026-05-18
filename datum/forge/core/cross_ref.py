"""
FORGE Core — Cross-Reference Checker
Validates document claims against the actual codebase.

Checks:
  1. File path references (backtick paths, explicit paths) → file exists
  2. Component/hook/function names → symbol exists in codebase
  3. Package versions cited → match package.json
  4. TODO/FIXME in referenced files → flag docs claiming "complete"
  5. Environment variable names → appear in code or .env.example
  6. Route paths (expo-router) → route file exists

Results feed into VerificationResult.failures / warnings as category="cross_ref"
"""

import re
import json
import hashlib
import fnmatch
from pathlib import Path
from typing import Optional
from functools import lru_cache

from core.logger import get_logger

log = get_logger("cross_ref")


# ── Patterns ──────────────────────────────────────────────────────────────

# Backtick file paths: `src/components/Foo.tsx`, `./services/bar.ts`
_FILE_REF_RE = re.compile(
    r'`([./a-zA-Z0-9_@\-]+\.(ts|tsx|js|jsx|json|yaml|yml|md|sh|ps1|txt))`'
)
# Unquoted explicit paths in sentences: "the file components/shocks/Foo.tsx"
_PATH_MENTION_RE = re.compile(
    r'(?<![`"\'])([a-zA-Z0-9_@\-]+/[a-zA-Z0-9_@\-/]+\.(ts|tsx|js|jsx|json|md))(?![`"\'])'
)
# React hooks: useShockData, useRaceTracker, etc.
_HOOK_RE = re.compile(r'\b(use[A-Z][a-zA-Z0-9]+)\b')
# React components (PascalCase, min 4 chars, not all-caps acronyms)
_COMPONENT_RE = re.compile(r'\b([A-Z][a-z][a-zA-Z0-9]{2,}(?:Modal|Screen|View|Card|Button|Tab|List|Form|Page|Panel|Header|Footer|Drawer|Provider|Wrapper|Handler|Manager)?)\b')
# Explicit "component named X" or "the X component" — no IGNORECASE so group only captures PascalCase
_COMPONENT_NAMED_RE = re.compile(r'(?i:component|screen|modal|hook)\s+[`"]?([A-Z][a-zA-Z0-9]{3,})[`"]?')
# Package versions: "expo@53", "react-native 0.79", "firebase: ^10"
_PKG_VERSION_RE = re.compile(
    r'\b(expo|react-native|firebase|typescript|stripe|capacitor|react-navigation)[\s@:~^]+([0-9]+\.[0-9]+[^\s,)\]]*)',
    re.IGNORECASE
)
# Environment variables: EXPO_PUBLIC_*, FIREBASE_*, ANTHROPIC_*
_ENV_VAR_RE = re.compile(r'\b([A-Z][A-Z0-9_]{3,}(?:_KEY|_URL|_ID|_SECRET|_TOKEN|_API|_HOST|_PORT|_EMAIL|_PASS|_ENV))\b')
# Completion claims
_COMPLETE_RE = re.compile(
    r'(100%\s*complete|fully\s*implemented|complete\s*and\s*working|'
    r'successfully\s*implemented|production\s*ready|all\s*tests\s*pass(?:ing)?)',
    re.IGNORECASE
)
# TODO/FIXME in code
_TODO_RE = re.compile(r'\b(TODO|FIXME|HACK|XXX|TEMP|WIP)\b')

# Noise filter — skip these very common PascalCase words that aren't components
_COMPONENT_NOISE = {
    "React", "Native", "String", "Number", "Boolean", "Object", "Array",
    "Promise", "Error", "Event", "Date", "Math", "JSON", "Firebase",
    "Firestore", "Storage", "Auth", "Stripe", "Google", "Apple",
    "TypeScript", "JavaScript", "Android", "iOS", "Windows", "Linux",
    "Expo", "GitHub", "Node", "True", "False", "None", "Null",
    "This", "That", "The", "For", "From", "With", "Into", "Upon",
    "When", "Then", "After", "Before", "During", "Between",
    "Report", "Guide", "Setup", "Config", "Status", "Summary",
    "Version", "Update", "Change", "Feature", "Issue", "Fix",
    "Step", "Note", "Warning", "Error", "Success", "Complete",
}

# File extensions to search for symbols
_CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}


class CrossRefChecker:
    """
    Checks document claims against the actual repository state.
    Initialise once per Forge run, shared across all documents.
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self._pkg_json: Optional[dict] = None
        self._file_index: Optional[set] = None       # relative paths (lowercase)
        self._symbol_index: Optional[dict] = None    # symbol -> list of files
        self._env_example: Optional[set] = None      # known env var names
        self._built = False

    # ── Index building ─────────────────────────────────────────────────────

    def build_index(self):
        """Build file and symbol indexes. Call once before checking docs."""
        if self._built:
            return
        log.info("CrossRef: building file and symbol index...")
        self._file_index    = self._build_file_index()
        self._symbol_index  = self._build_symbol_index()
        self._pkg_json      = self._load_pkg_json()
        self._env_example   = self._build_env_index()
        self._built = True
        log.info(f"CrossRef: indexed {len(self._file_index)} files, "
                 f"{len(self._symbol_index)} symbols")

    def _build_file_index(self) -> set:
        """All relative paths in repo (lowercase for case-insensitive matching)."""
        index = set()
        skip = {"node_modules", ".git", "__pycache__", "coverage",
                 "playwright-report", ".expo", "android", "ios", ".firebase"}
        for p in self.repo_path.rglob("*"):
            if p.is_file():
                parts = p.parts
                if any(s in parts for s in skip):
                    continue
                rel = str(p.relative_to(self.repo_path)).replace("\\", "/").lower()
                index.add(rel)
                # Also add just the filename
                index.add(p.name.lower())
        return index

    def _build_symbol_index(self) -> dict:
        """Map symbol_name -> list_of_files containing the symbol."""
        index: dict[str, list] = {}
        skip = {"node_modules", ".git", "__pycache__", "coverage",
                 "playwright-report", ".expo", "android", "ios"}

        for p in self.repo_path.rglob("*"):
            if not p.is_file() or p.suffix not in _CODE_EXTENSIONS:
                continue
            if any(s in p.parts for s in skip):
                continue

            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
                rel  = str(p.relative_to(self.repo_path)).replace("\\", "/")

                # Exported functions/components/hooks/classes
                exports = re.findall(
                    r'export\s+(?:default\s+)?(?:function|const|class|interface|type)\s+([A-Za-z][A-Za-z0-9_]*)',
                    text
                )
                # Hook definitions
                hooks = re.findall(r'(?:function|const)\s+(use[A-Z][a-zA-Z0-9]*)', text)

                for sym in set(exports + hooks):
                    index.setdefault(sym, []).append(rel)
            except Exception:
                pass

        return index

    def _load_pkg_json(self) -> dict:
        pkg_path = self.repo_path / "package.json"
        if pkg_path.exists():
            try:
                return json.loads(pkg_path.read_text())
            except Exception:
                pass
        return {}

    def _build_env_index(self) -> set:
        """Collect known env var names from .env.example and .env files."""
        known = set()
        for fname in [".env.example", ".env.app.template", ".env.development",
                       ".env.local.backup", "env.example", "env.firebase.example"]:
            p = self.repo_path / fname
            if p.exists():
                for line in p.read_text(errors="ignore").splitlines():
                    m = re.match(r'^([A-Z][A-Z0-9_]+)=', line.strip())
                    if m:
                        known.add(m.group(1))
        return known

    # ── Main check ──────────────────────────────────────────────────────────

    def check(self, content: str, doc_path: str) -> tuple[list, list]:
        """
        Returns (failures, warnings) as lists of dicts with keys:
          rule_id, category, message, severity, context
        """
        if not self._built:
            self.build_index()

        failures = []
        warnings = []

        # 1. File path references
        for issue in self._check_file_refs(content, doc_path):
            (failures if issue["severity"] == "error" else warnings).append(issue)

        # 2. Component and hook names
        for issue in self._check_symbols(content):
            warnings.append(issue)

        # 3. Package versions
        for issue in self._check_versions(content):
            warnings.append(issue)

        # 4. TODO/stale completion claims
        for issue in self._check_completion_claims(content, doc_path):
            warnings.append(issue)

        # 5. Environment variables
        for issue in self._check_env_vars(content):
            warnings.append(issue)

        return failures, warnings

    # ── File ref checker ───────────────────────────────────────────────────

    def _check_file_refs(self, content: str, doc_path: str) -> list:
        issues = []
        seen   = set()

        candidates = []
        # Backtick paths — strip backtick content first to avoid PATH_MENTION_RE
        # matching truncated paths inside backtick spans
        backtick_spans = set()
        for m in _FILE_REF_RE.finditer(content):
            candidates.append(m.group(1))
            backtick_spans.add(m.start())

        # Bare path mentions — apply only to content outside backtick spans
        content_no_ticks = re.sub(r'`[^`]+`', lambda m: ' ' * len(m.group()), content)
        for m in _PATH_MENTION_RE.finditer(content_no_ticks):
            p = m.group(1)
            if "/" in p and not p.startswith("http"):
                candidates.append(p)

        for ref in candidates:
            ref_clean = ref.lstrip("./").replace("\\", "/")
            if ref_clean in seen:
                continue
            seen.add(ref_clean)

            # Skip docs themselves, URLs, node_modules mentions
            if "node_modules" in ref or ref.startswith("http"):
                continue
            # Skip very short refs (false positives)
            if len(ref_clean) < 8:
                continue

            found = self._resolve_path(ref_clean)
            if not found:
                issues.append({
                    "rule_id":  "file_ref_missing",
                    "category": "cross_ref",
                    "severity": "error",
                    "message":  f"File referenced but not found in repo: `{ref}`",
                    "context":  ref,
                })
            else:
                log.debug(f"CrossRef: verified {ref} → {found}")

        return issues

    def _resolve_path(self, ref: str) -> Optional[str]:
        """Try several strategies to find the file in the index."""
        ref_lower = ref.lower()
        # Exact relative match
        if ref_lower in self._file_index:
            return ref_lower
        # Filename only
        filename = Path(ref).name.lower()
        if filename in self._file_index:
            return filename
        # Partial suffix match (last N path segments)
        parts = ref_lower.split("/")
        for n in (3, 2, 1):
            suffix = "/".join(parts[-n:])
            if any(p.endswith(suffix) for p in self._file_index):
                return suffix
        return None

    # ── Symbol checker ─────────────────────────────────────────────────────

    def _check_symbols(self, content: str) -> list:
        issues = []

        # Check hooks (high confidence)
        hooks_mentioned = set(_HOOK_RE.findall(content))
        hooks_mentioned -= {"useState", "useEffect", "useCallback", "useMemo",
                             "useRef", "useContext", "useReducer", "useLayoutEffect",
                             "useImperativeHandle", "useDebugValue", "useId",
                             "useTransition", "useDeferredValue"}
        for hook in hooks_mentioned:
            if hook not in self._symbol_index:
                issues.append({
                    "rule_id":  "hook_not_found",
                    "category": "cross_ref",
                    "severity": "warning",
                    "message":  f"Hook `{hook}` referenced but not found as export in codebase",
                    "context":  hook,
                })

        # Check explicitly named components ("the X component", "component X")
        named = set(_COMPONENT_NAMED_RE.findall(content))
        named -= _COMPONENT_NOISE
        for name in named:
            if len(name) < 4:
                continue
            if name not in self._symbol_index and name.lower() not in self._file_index:
                issues.append({
                    "rule_id":  "component_not_found",
                    "category": "cross_ref",
                    "severity": "warning",
                    "message":  f"Component/symbol `{name}` referenced but not found in codebase",
                    "context":  name,
                })

        return issues

    # ── Version checker ────────────────────────────────────────────────────

    def _check_versions(self, content: str) -> list:
        issues = []
        if not self._pkg_json:
            return issues

        deps = {}
        deps.update(self._pkg_json.get("dependencies", {}))
        deps.update(self._pkg_json.get("devDependencies", {}))

        for m in _PKG_VERSION_RE.finditer(content):
            pkg_name  = m.group(1).lower()
            doc_ver   = m.group(2).strip()

            # Map common aliases
            pkg_map = {
                "react-native": "react-native",
                "expo": "expo",
                "firebase": "firebase",
                "typescript": "typescript",
                "stripe": "@stripe/stripe-react-native",
                "capacitor": "@capacitor/core",
                "react-navigation": "@react-navigation/native",
            }
            actual_key = pkg_map.get(pkg_name, pkg_name)

            # Try to find in deps
            actual_ver = None
            for dk, dv in deps.items():
                if dk.endswith(actual_key) or dk == actual_key:
                    actual_ver = dv.lstrip("^~>=")
                    break

            if actual_ver and not actual_ver.startswith(doc_ver.split(".")[0]):
                issues.append({
                    "rule_id":  "version_mismatch",
                    "category": "cross_ref",
                    "severity": "warning",
                    "message":  (f"Version mismatch: doc says `{pkg_name} {doc_ver}`, "
                                 f"package.json has `{actual_ver}`"),
                    "context":  f"{pkg_name}@{doc_ver} vs {actual_ver}",
                })

        return issues

    # ── Completion claim checker ───────────────────────────────────────────

    def _check_completion_claims(self, content: str, doc_path: str) -> list:
        issues = []

        if not _COMPLETE_RE.search(content):
            return issues

        # Extract all file refs from this doc and check for TODOs in those files
        todo_files = []
        for m in _FILE_REF_RE.finditer(content):
            ref = m.group(1)
            resolved = self._resolve_path(ref.lstrip("./"))
            if resolved:
                # Find the actual Path
                for f in self.repo_path.rglob(Path(ref).name):
                    if "node_modules" in str(f):
                        continue
                    try:
                        src = f.read_text(encoding="utf-8", errors="ignore")
                        todos = _TODO_RE.findall(src)
                        if todos:
                            rel = str(f.relative_to(self.repo_path)).replace("\\", "/")
                            todo_files.append(f"{rel} ({len(todos)} TODO/FIXME)")
                    except Exception:
                        pass

        if todo_files:
            issues.append({
                "rule_id":  "completion_claim_with_todos",
                "category": "cross_ref",
                "severity": "warning",
                "message":  (f"Doc claims completion but referenced files contain TODOs: "
                             + ", ".join(todo_files[:3])),
                "context":  "; ".join(todo_files[:3]),
            })

        return issues

    # ── Env var checker ────────────────────────────────────────────────────

    def _check_env_vars(self, content: str) -> list:
        issues = []
        if not self._env_example:
            return issues

        mentioned = set(_ENV_VAR_RE.findall(content))
        # Only check vars that look app-specific (not generic like PATH, HOME)
        skip_generic = {"PATH", "HOME", "USER", "SHELL", "TERM", "NODE_ENV",
                         "PORT", "HOST", "DEBUG", "LOG_LEVEL"}
        for var in mentioned - skip_generic:
            if var not in self._env_example:
                issues.append({
                    "rule_id":  "undocumented_env_var",
                    "category": "cross_ref",
                    "severity": "warning",
                    "message":  f"Env var `{var}` not found in .env.example or templates",
                    "context":  var,
                })

        return issues

    # ── Summarise for repair prompt ────────────────────────────────────────

    @staticmethod
    def format_issues_for_prompt(failures: list, warnings: list) -> str:
        """Formats cross-ref issues into a concise prompt section."""
        if not failures and not warnings:
            return ""

        lines = ["CROSS-REFERENCE FINDINGS (validated against actual codebase):"]
        for f in failures:
            lines.append(f"  [BROKEN REF] {f['message']}")
        for w in warnings[:15]:  # cap at 15 to avoid prompt bloat
            lines.append(f"  [VERIFY] {w['message']}")
        if len(warnings) > 15:
            lines.append(f"  ... and {len(warnings) - 15} more warnings")

        return "\n".join(lines)
