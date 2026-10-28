"""CODEOWNERS parsing with GitHub semantics (C1): the last matching rule wins."""
import re
from pathlib import Path

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

Rule = tuple[str, list[str]]


def load_codeowners(repo_root: str | Path | None) -> list[Rule]:
    """Rules from the first CODEOWNERS file found, in file order. No file -> []."""
    if repo_root is None:
        return []
    for location in LOCATIONS:
        path = Path(repo_root) / location
        if path.is_file():
            return parse_codeowners(path.read_text(encoding="utf-8", errors="replace"))
    return []


def parse_codeowners(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        rules.append((pattern, owners))
    return rules


def _to_regex(pattern: str) -> re.Pattern:
    """Translate one CODEOWNERS pattern into a regex over POSIX paths."""
    anchored = pattern.startswith("/")
    directory = pattern.endswith("/")
    body = pattern.strip("/")
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out.append("/.*")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    core = "".join(out)
    # An unanchored pattern without a slash matches at any depth ("*.js", "docs").
    prefix = "" if anchored or "/" in body else "(?:.*/)?"
    tail = "/.*" if directory else "(?:/.*)?"
    return re.compile(f"^{prefix}{core}{tail}$")


def owners_for(path: str, rules: list[Rule]) -> list[str]:
    """Owners of `path` (POSIX, repo-relative). The last matching rule wins; none matching -> []."""
    path = path.replace("\\", "/").lstrip("/")
    result: list[str] = []
    for pattern, owners in rules:
        if _to_regex(pattern).match(path):
            result = owners
    return result


def owners_by_file(paths: list[str], rules: list[Rule]) -> dict[str, list[str]]:
    return {p: owners_for(p, rules) for p in paths}


def team_for(paths: list[str], rules: list[Rule]) -> str | None:
    """The owner that covers most of `paths` (ties: alphabetical); None when nobody owns them."""
    counts: dict[str, int] = {}
    for path in paths:
        for owner in owners_for(path, rules):
            counts[owner] = counts.get(owner, 0) + 1
    return sorted(counts, key=lambda o: (-counts[o], o))[0] if counts else None
