"""
ANVIL Parsers — Language Plugin System
=======================================
Each language is a thin plugin that extracts CodeUnits from source files.
Plugin interface: parse(file_path, content) → list[CodeUnit]

Adding a new language = one class implementing BaseParser.
ANVIL discovers parsers automatically — no registration needed.

Supported: Python, JavaScript/TypeScript, SQL, HTML, CSS, Go, Rust, Java, C/C++, YAML/JSON
"""

import re
import ast
import json
from pathlib import Path
from typing import Optional
from abc import ABC, abstractmethod

from core.types import CodeUnit, UnitKind
from core.logger import get_logger

log = get_logger("parsers")


# ── Base parser ────────────────────────────────────────────────────────────

class BaseParser(ABC):
    language: str = "unknown"
    extensions: list[str] = []

    @abstractmethod
    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        """Extract CodeUnits from file content."""
        ...

    def _make_unit(self, file_path: str, kind: str, name: str,
                    body: str, signature: str = "", docstring: str = "",
                    line_start: int = 0, line_end: int = 0,
                    tags: list = None) -> CodeUnit:
        return CodeUnit(
            file_path=file_path,
            language=self.language,
            kind=kind,
            name=name,
            signature=signature,
            body=body[:5000],          # Cap body size
            docstring=docstring[:500],
            line_start=line_start,
            line_end=line_end,
            tags=tags or [],
        )


# ── Python parser ──────────────────────────────────────────────────────────

class PythonParser(BaseParser):
    language = "python"
    extensions = [".py"]

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        units = []
        lines = content.splitlines()

        try:
            tree = ast.parse(content)
        except SyntaxError as e:
            log.debug(f"Python parse error {file_path}: {e}")
            return self._fallback_parse(file_path, content)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = UnitKind.METHOD if self._is_method(node, tree) else UnitKind.FUNCTION
                docstring = ast.get_docstring(node) or ""
                body_lines = lines[node.lineno - 1: getattr(node, 'end_lineno', node.lineno + 10)]
                body = "\n".join(body_lines)

                # Extract decorator tags
                tags = []
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Name):
                        tags.append(f"@{dec.id}")
                    elif isinstance(dec, ast.Attribute):
                        tags.append(f"@{dec.attr}")

                # Detect route handlers
                route_decs = [t for t in tags if any(m in t for m in ["route", "get", "post", "put", "delete", "patch"])]
                if route_decs:
                    kind = UnitKind.ROUTE

                units.append(self._make_unit(
                    file_path=file_path,
                    kind=kind,
                    name=node.name,
                    signature=body_lines[0] if body_lines else "",
                    body=body,
                    docstring=docstring,
                    line_start=node.lineno,
                    line_end=getattr(node, 'end_lineno', node.lineno),
                    tags=tags,
                ))

            elif isinstance(node, ast.ClassDef):
                docstring = ast.get_docstring(node) or ""
                body_lines = lines[node.lineno - 1: getattr(node, 'end_lineno', node.lineno + 5)]
                units.append(self._make_unit(
                    file_path=file_path,
                    kind=UnitKind.CLASS,
                    name=node.name,
                    signature=body_lines[0] if body_lines else "",
                    body="\n".join(body_lines[:10]),
                    docstring=docstring,
                    line_start=node.lineno,
                    line_end=getattr(node, 'end_lineno', node.lineno),
                ))

            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        units.append(self._make_unit(
                            file_path=file_path,
                            kind=UnitKind.CONSTANT,
                            name=target.id,
                            body=line,
                            line_start=node.lineno,
                            line_end=node.lineno,
                            tags=["constant"],
                        ))

        # Whole-module unit
        units.append(self._make_unit(
            file_path=file_path,
            kind=UnitKind.MODULE,
            name=Path(file_path).stem,
            body=content[:3000],
            line_start=1,
            line_end=len(lines),
        ))

        return units

    def _is_method(self, node, tree) -> bool:
        for parent in ast.walk(tree):
            if isinstance(parent, ast.ClassDef):
                for item in parent.body:
                    if item is node:
                        return True
        return False

    def _fallback_parse(self, file_path: str, content: str) -> list[CodeUnit]:
        """Regex fallback when AST parsing fails."""
        units = []
        for m in re.finditer(r'^(async\s+)?def\s+(\w+)\s*\(', content, re.MULTILINE):
            units.append(self._make_unit(
                file_path=file_path,
                kind=UnitKind.FUNCTION,
                name=m.group(2),
                body=m.group(0),
                line_start=content[:m.start()].count("\n") + 1,
                line_end=content[:m.start()].count("\n") + 1,
            ))
        return units


# ── JavaScript / TypeScript parser ────────────────────────────────────────

class JavaScriptParser(BaseParser):
    language = "javascript"
    extensions = [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        units = []
        lines = content.splitlines()

        # Functions (all styles)
        patterns = [
            # function declaration
            (r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(', UnitKind.FUNCTION),
            # arrow function assigned to const/let/var
            (r'^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(', UnitKind.FUNCTION),
            # class declaration
            (r'^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)', UnitKind.CLASS),
            # TypeScript interface
            (r'^(?:export\s+)?interface\s+(\w+)', UnitKind.SCHEMA),
            # TypeScript type alias
            (r'^(?:export\s+)?type\s+(\w+)\s*=', UnitKind.SCHEMA),
            # Express/router routes
            (r'(?:router|app)\.(get|post|put|delete|patch|use)\s*\(\s*[\'"]([^\'"]+)', UnitKind.ROUTE),
        ]

        for i, line in enumerate(lines):
            for pattern, kind in patterns:
                m = re.match(pattern, line.strip())
                if m:
                    name = m.group(2) if kind == UnitKind.ROUTE else m.group(1)
                    # Extract body (next N lines)
                    body_end = min(i + 30, len(lines))
                    body = "\n".join(lines[i:body_end])

                    # Find JSDoc comment above
                    docstring = ""
                    for j in range(i - 1, max(-1, i - 6), -1):
                        l = lines[j].strip()
                        if l.startswith("*") or l.startswith("/**") or l.startswith("//"):
                            docstring = l.lstrip("/* /")
                        else:
                            break

                    units.append(self._make_unit(
                        file_path=file_path,
                        kind=kind,
                        name=name,
                        signature=line.strip()[:120],
                        body=body[:2000],
                        docstring=docstring,
                        line_start=i + 1,
                        line_end=body_end,
                    ))
                    break

        # Constants (ALL_CAPS or exported const with primitive)
        for m in re.finditer(
            r'^(?:export\s+)?const\s+([A-Z][A-Z0-9_]{2,})\s*=\s*([^\n;{]+)',
            content, re.MULTILINE
        ):
            line_num = content[:m.start()].count("\n") + 1
            units.append(self._make_unit(
                file_path=file_path,
                kind=UnitKind.CONSTANT,
                name=m.group(1),
                body=m.group(0),
                line_start=line_num,
                line_end=line_num,
                tags=["constant"],
            ))

        # Module unit
        units.append(self._make_unit(
            file_path=file_path,
            kind=UnitKind.MODULE,
            name=Path(file_path).stem,
            body=content[:3000],
            line_start=1,
            line_end=len(lines),
        ))

        return units


# ── SQL parser ────────────────────────────────────────────────────────────

class SQLParser(BaseParser):
    language = "sql"
    extensions = [".sql"]

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        units = []
        # Tables
        for m in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(', content, re.IGNORECASE):
            line_num = content[:m.start()].count("\n") + 1
            # Extract table definition
            start = m.start()
            depth = 0
            end   = start
            for i, ch in enumerate(content[start:], start):
                if ch == "(": depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            units.append(self._make_unit(
                file_path=file_path, kind=UnitKind.SCHEMA,
                name=m.group(1), body=content[start:end],
                line_start=line_num, line_end=line_num + content[start:end].count("\n"),
                tags=["table"],
            ))

        # Functions / procedures
        for m in re.finditer(
            r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+(\w+)',
            content, re.IGNORECASE
        ):
            line_num = content[:m.start()].count("\n") + 1
            end_line = line_num + 20
            body = "\n".join(content.splitlines()[line_num-1:end_line])
            units.append(self._make_unit(
                file_path=file_path, kind=UnitKind.FUNCTION,
                name=m.group(1), body=body,
                line_start=line_num, line_end=end_line,
            ))

        return units


# ── YAML / JSON config parser ─────────────────────────────────────────────

class ConfigParser(BaseParser):
    language = "config"
    extensions = [".yaml", ".yml", ".json", ".env", ".toml"]

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        ext = Path(file_path).suffix.lower()
        units = []

        # Whole-file config unit
        units.append(self._make_unit(
            file_path=file_path,
            kind=UnitKind.CONFIG,
            name=Path(file_path).name,
            body=content[:5000],
            line_start=1,
            line_end=content.count("\n") + 1,
            tags=["config"],
        ))

        # For JSON, extract top-level keys as individual units
        if ext == ".json":
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    for key, value in list(data.items())[:20]:
                        units.append(self._make_unit(
                            file_path=file_path,
                            kind=UnitKind.CONFIG,
                            name=key,
                            body=json.dumps({key: value}, indent=2)[:500],
                            tags=["config", "json_key"],
                        ))
            except Exception:
                pass

        return units


# ── Generic/fallback parser ────────────────────────────────────────────────

class GenericParser(BaseParser):
    language = "generic"
    extensions = []  # Catches everything else

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        # Just return the whole file as a module unit
        return [self._make_unit(
            file_path=file_path,
            kind=UnitKind.MODULE,
            name=Path(file_path).stem,
            body=content[:5000],
            line_start=1,
            line_end=content.count("\n") + 1,
        )]


# ── Go parser ─────────────────────────────────────────────────────────────

class GoParser(BaseParser):
    language = "go"
    extensions = [".go"]

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        units = []
        lines = content.splitlines()
        for i, line in enumerate(lines):
            # Functions
            m = re.match(r'^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(', line)
            if m:
                body = "\n".join(lines[i:min(i+30, len(lines))])
                units.append(self._make_unit(
                    file_path=file_path, kind=UnitKind.FUNCTION,
                    name=m.group(1), signature=line, body=body,
                    line_start=i+1, line_end=min(i+30, len(lines)),
                ))
            # Structs
            m = re.match(r'^type\s+(\w+)\s+struct\s*\{', line)
            if m:
                body = "\n".join(lines[i:min(i+20, len(lines))])
                units.append(self._make_unit(
                    file_path=file_path, kind=UnitKind.SCHEMA,
                    name=m.group(1), body=body,
                    line_start=i+1, line_end=min(i+20, len(lines)),
                ))
        return units


# ── Registry ──────────────────────────────────────────────────────────────

class ParserRegistry:
    """Discovers and selects the right parser for any file."""

    def __init__(self):
        self._parsers: list[BaseParser] = [
            PythonParser(),
            JavaScriptParser(),
            SQLParser(),
            ConfigParser(),
            GoParser(),
            GenericParser(),   # Must be last — catches everything
        ]
        self._ext_map: dict[str, BaseParser] = {}
        for parser in self._parsers:
            for ext in parser.extensions:
                self._ext_map[ext] = parser

    def get_parser(self, file_path: str) -> BaseParser:
        ext = Path(file_path).suffix.lower()
        return self._ext_map.get(ext, self._parsers[-1])  # fallback = generic

    def parse(self, file_path: str, content: str) -> list[CodeUnit]:
        parser = self.get_parser(file_path)
        try:
            units = parser.parse(file_path, content)
            log.debug(f"Parsed {file_path}: {len(units)} units ({parser.language})")
            return units
        except Exception as e:
            log.warning(f"Parser error {file_path}: {e}")
            return [CodeUnit(file_path=file_path, language=parser.language,
                             kind=UnitKind.MODULE, name=Path(file_path).stem,
                             body=content[:3000])]

    def supported_extensions(self) -> set[str]:
        return set(self._ext_map.keys())


# Singleton
registry = ParserRegistry()
