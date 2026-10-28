"""Deterministic pattern search over the package scope (BS 7.1 step 3).

Rules for a symbol `obj.method`:
  exact receiver  \\bobj\\.method\\s*\\(   confidence 0.8
  any receiver    \\.method\\s*\\(         confidence 0.6   (req / request / r ...)
Bare identifiers and option-like receivers (options.extended) match `key:`          confidence 0.4
Path-syntax changes match route strings with a bare wildcard                       confidence 0.6
"""
import re
from dataclasses import dataclass
from pathlib import Path

from app.updates.models import ChangeRef

CODE_EXTENSIONS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
MAX_FILE_BYTES = 1_000_000
OPTION_RECEIVERS = {"options", "opts", "option", "config", "settings", "params", "defaults"}
ROUTE_WILDCARD = re.compile(r"""\.(get|post|put|patch|delete|all|use)\s*\(\s*['"][^'"]*\*""")
PATH_SYNTAX_HINT = re.compile(r"wildcard|path-to-regexp|route (path|match|syntax)|path syntax|regexp", re.I)
PATH_SYNTAX_SYMBOL = "path-syntax"


@dataclass
class Hit:
    file_path: str
    line: int
    snippet: str
    symbol: str
    confidence: float
    change_index: int  # index into the event's changes
    source: str = "pattern"


def normalise_symbol(raw: str) -> str:
    """'req.param(name)' / '`res.json()`' -> 'req.param'. Keeps dotted identifiers only."""
    text = raw.strip().strip("`").strip()
    text = re.split(r"[\s(\[]", text, maxsplit=1)[0]
    return text.rstrip(".,;:")


def min_args(raw: str) -> int:
    """'res.json(obj, status)' -> 2: the change is about a call with that many arguments."""
    m = re.search(r"\(([^)]*)\)", raw)
    if not m or not m.group(1).strip():
        return 0
    return len([a for a in m.group(1).split(",") if a.strip()])


def _compile(symbol: str, args: int = 0) -> list[tuple[re.Pattern, float]]:
    if "." in symbol and args >= 2:
        receiver, _, method = symbol.rpartition(".")
        # At least two arguments: a comma after the first argument, before any nested bracket.
        tail = r"\s*\([^(){}]*,"
        return [
            (re.compile(rf"\b{re.escape(receiver)}\.{re.escape(method)}{tail}"), 0.8),
            (re.compile(rf"\.{re.escape(method)}{tail}"), 0.6),
        ]
    if "." in symbol:
        receiver, _, method = symbol.rpartition(".")
        if not re.fullmatch(r"[\w$]+", method):
            return []
        if receiver.split(".")[-1].lower() in OPTION_RECEIVERS:
            return [(re.compile(rf"\b{re.escape(method)}\s*:"), 0.4)]
        return [
            (re.compile(rf"\b{re.escape(receiver)}\.{re.escape(method)}\s*\("), 0.8),
            (re.compile(rf"\.{re.escape(method)}\s*\("), 0.6),
        ]
    if re.fullmatch(r"[\w$]+", symbol):
        return [(re.compile(rf"\b{re.escape(symbol)}\s*:"), 0.4)]
    return []


def wants_route_patterns(change: ChangeRef) -> bool:
    text = " ".join([change.description, change.migration_hint or "", *change.api_symbols])
    return bool(PATH_SYNTAX_HINT.search(text)) or any(s.strip() == "*" for s in change.api_symbols)


def _is_comment(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith(("//", "*", "/*"))


def searchable(path: Path) -> bool:
    name = path.name
    if path.suffix not in CODE_EXTENSIONS or ".min." in name or "node_modules" in path.parts:
        return False
    try:
        return path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def search_scope(root: Path, files: list[str], changes: list[ChangeRef]) -> list[Hit]:
    """All pattern hits for every change in `files` (repo-relative POSIX paths) under `root`."""
    compiled: list[tuple[int, str, list[tuple[re.Pattern, float]]]] = []
    route_changes: list[int] = []
    for index, change in enumerate(changes):
        for raw in change.api_symbols:
            symbol = normalise_symbol(raw)
            patterns = _compile(symbol, min_args(raw)) if symbol else []
            if patterns:
                compiled.append((index, symbol, patterns))
        if wants_route_patterns(change):
            route_changes.append(index)

    hits: dict[tuple[str, int, str], Hit] = {}
    for rel in files:
        path = root / rel
        if not path.is_file() or not searchable(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, start=1):
            if _is_comment(text):
                continue
            for index, symbol, patterns in compiled:
                best = max((conf for pattern, conf in patterns if pattern.search(text)), default=None)
                if best is not None:
                    key = (rel, number, symbol)
                    if key not in hits or hits[key].confidence < best:
                        hits[key] = Hit(rel, number, text.strip()[:300], symbol, best, index)
            if route_changes and ROUTE_WILDCARD.search(text):
                key = (rel, number, PATH_SYNTAX_SYMBOL)
                hits.setdefault(key, Hit(rel, number, text.strip()[:300], PATH_SYNTAX_SYMBOL, 0.6, route_changes[0]))
    return sorted(hits.values(), key=lambda h: (h.file_path, h.line, h.symbol))
