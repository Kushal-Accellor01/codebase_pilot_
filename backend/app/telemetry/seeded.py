"""Step 4: production evidence from logs (BS 7.2). Deterministic: the LLM writes nothing here.

LogSource is the seam: SeededLogSource reads JSON-lines files now; Azure Monitor / Datadog adapters implement
the same query().
"""
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.core.contracts import Finding, TelemetryEvidence

WINDOW_DAYS = 7


@dataclass
class LogLine:
    ts: datetime
    service: str
    level: str
    msg: str
    route: str | None = None


class LogSource(Protocol):
    def query(self, service: str | None, start: datetime, end: datetime) -> Iterable[LogLine]: ...


def _parse_ts(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SeededLogSource:
    """JSON lines: {"ts": "...", "service": "shop-api", "level": "warn", "msg": "...", "route": "/orders/:id"}"""

    def __init__(self, directory: str | Path):
        self._dir = Path(directory)

    def _all(self) -> list[LogLine]:
        lines = []
        for path in sorted(self._dir.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                    lines.append(LogLine(_parse_ts(row["ts"]), row.get("service", ""), row.get("level", ""),
                                         row.get("msg", ""), row.get("route")))
                except (ValueError, KeyError):
                    continue  # one bad line never hides the rest
        return lines

    def latest(self) -> datetime | None:
        return max((line.ts for line in self._all()), default=None)

    def query(self, service: str | None, start: datetime, end: datetime) -> Iterable[LogLine]:
        return [ln for ln in self._all() if start <= ln.ts <= end and (service is None or ln.service == service)]


_NUMBER = re.compile(r"\b\d+(?:\.\d+)*\b")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
_TIMESTAMP = re.compile(r"^\w{3}, \d{1,2} \w{3} \d{4} [\d:]+ GMT\s*")


def signature(message: str) -> str:
    """Normalise ids, numbers and timestamps so repeats of one event group together."""
    text = _TIMESTAMP.sub("", message.strip())
    text = _HEX.sub("<id>", text)
    return _NUMBER.sub("<n>", text)


def _matches(symbol: str | None, message: str) -> bool:
    """Whole-identifier match: 'res.send' must not match 'res.sendfile'."""
    if not symbol:
        return False
    return re.search(r"(?<![\w.$])" + re.escape(symbol) + r"(?![\w$])", message.replace("`", "")) is not None


def collect_evidence(
    source: LogSource, findings: list[Finding], *, service: str | None = None, now: datetime | None = None,
    window_days: int = WINDOW_DAYS, source_name: str = "seeded_logs",
) -> list[TelemetryEvidence]:
    """Signatures in the last `window_days` of logs that mention a finding's symbol.

    The window ends at `now`, or at the newest log line when `now` is omitted (seeded files are frozen in time).
    """
    symbols = {f.symbol for f in findings if f.symbol and f.symbol != "path-syntax"}
    if not symbols:
        return []
    end = now or getattr(source, "latest", lambda: None)() or datetime.now(timezone.utc)
    start = end - timedelta(days=window_days)

    groups: dict[tuple[str, str], list[LogLine]] = defaultdict(list)
    for line in source.query(service, start, end):
        for symbol in sorted(symbols):
            if _matches(symbol, line.msg):
                groups[(signature(line.msg), symbol)].append(line)
                break

    evidence = []
    for (sig, symbol), lines in groups.items():
        lines.sort(key=lambda ln: ln.ts)
        evidence.append(
            TelemetryEvidence(
                source=source_name, signature=sig, count=len(lines),
                first_seen=lines[0].ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                last_seen=lines[-1].ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                sample=lines[-1].msg, related_symbol=symbol,
            )
        )
    return sorted(evidence, key=lambda e: (-e.count, e.signature))
