"""G6: production evidence from Azure Monitor (Log Analytics), read-only KQL over console logs.

Needs `pip install azure-monitor-query azure-identity` and the **Log Analytics Reader** role on the workspace for
the identity running Pilot (DefaultAzureCredential: managed identity in Azure, `az login` locally). The table and
column names depend on how the Container Apps environment ships logs, so they are settings.
"""

import re
from collections.abc import Iterable
from datetime import datetime, timezone

from app.core.config import Settings
from app.telemetry.seeded import LogLine

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_APP = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_ROWS = 5000


def build_query(table: str, message_col: str, app_col: str, app_name: str, needle: str = "deprecated") -> str:
    """The KQL for one service. Every interpolated name is validated: nothing from outside reaches the query raw."""
    for ident in (table, message_col, app_col):
        if not _IDENT.match(ident):
            raise ValueError(f"unsafe column or table name: {ident!r}")
    if not _APP.match(app_name):
        raise ValueError(f"unsafe application name: {app_name!r}")
    if not re.fullmatch(r"[A-Za-z0-9 ._:-]{1,40}", needle):
        raise ValueError(f"unsafe search text: {needle!r}")
    return (
        f"{table}\n"
        f"| where {app_col} == '{app_name}' and {message_col} has '{needle}'\n"
        f"| project TimeGenerated, {message_col}\n"
        f"| order by TimeGenerated desc\n"
        f"| take {MAX_ROWS}"
    )


class AzureMonitorLogSource:
    def __init__(self, workspace_id: str, *, table: str, message_col: str, app_col: str, client=None):
        self._workspace = workspace_id
        self._table, self._message_col, self._app_col = table, message_col, app_col
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings, service: str, client=None) -> "AzureMonitorLogSource":
        source = cls(
            settings.azure_log_analytics_workspace_id or "",
            table=settings.azure_log_table,
            message_col=settings.azure_log_message_column,
            app_col=settings.azure_log_app_column,
            client=client,
        )
        source._service = service
        return source

    def _logs_client(self):
        if self._client is None:
            from azure.identity import DefaultAzureCredential  # lazy: only installed where Azure is used
            from azure.monitor.query import LogsQueryClient

            self._client = LogsQueryClient(DefaultAzureCredential())
        return self._client

    def query(self, service: str | None, start: datetime, end: datetime) -> Iterable[LogLine]:
        name = service or getattr(self, "_service", None)
        if not name:
            raise ValueError("an Azure Monitor query needs the service (container app) name")
        kql = build_query(self._table, self._message_col, self._app_col, name)
        result = self._logs_client().query_workspace(self._workspace, kql, timespan=(start, end))
        status = getattr(result, "status", None)
        if status is not None and str(getattr(status, "name", status)).upper() != "SUCCESS":
            raise RuntimeError(f"Log Analytics query incomplete: {getattr(result, 'partial_error', None)}")
        lines = []
        for table in result.tables:
            columns = [getattr(c, "name", c) for c in table.columns]
            for row in table.rows:
                record = dict(zip(columns, row))
                ts = record["TimeGenerated"]
                ts = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                lines.append(
                    LogLine(
                        ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                        name,
                        "warn",
                        str(record[self._message_col]),
                        None,
                    )
                )
        return lines
