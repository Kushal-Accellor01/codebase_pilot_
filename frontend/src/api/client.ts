import { fixtures } from "./fixtures";
import type {
  ChangeFeed, ChannelsView, DecisionRequest, Health, DependencyEvent, DependencyRow, Graph, Overview, RepoCheck, RepoOut, RunEvent,
  Scan, ScanRequestOut, ScanScope, ScanSummary, ScheduleOut, SourceList, UpgradeDetail, UpgradeSummary, VulnList,
} from "./models";

/** `VITE_API_MODE=fixtures` serves the recorded responses; anything else talks to the API. */
export const API_MODE: "fixtures" | "live" = import.meta.env.VITE_API_MODE === "fixtures" ? "fixtures" : "live";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // storage can be blocked; the page works without it
  }
}

/** Who is calling: the token when the server asks for one (PILOT_API_TOKEN) and the name shown as approver. */
function authHeaders(): Record<string, string> {
  const token = stored("pilotToken") ?? (import.meta.env.VITE_PILOT_API_TOKEN as string | undefined);
  const user = stored("pilotUser");
  return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(user ? { "X-Pilot-User": user } : {}) };
}

async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(init?.headers ?? {}) },
  });
  if (response.status === 401 && !retried && init?.method && init.method !== "GET") {
    const token = window.prompt("This server needs an API token (PILOT_API_TOKEN) for changes. Enter it:");
    if (token) {
      try {
        localStorage.setItem("pilotToken", token.trim());
      } catch {
        /* keep going; the retry below still sends it once */
      }
      return request<T>(path, { ...init, headers: { ...(init.headers ?? {}), Authorization: `Bearer ${token.trim()}` } }, true);
    }
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      /* the body was not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

const delay = <T>(value: T): Promise<T> => new Promise((resolve) => setTimeout(() => resolve(value), 60));
const fixtureMode = () => API_MODE === "fixtures";

export const api = {
  health: (): Promise<Health> =>
    fixtureMode() ? delay({ status: "ok", migrations: [], llm_mode: "replay", jobs_queued: 0, embedded_worker: true, model_configured: true }) : request("/health"),
  scans: (): Promise<Scan[]> => (fixtureMode() ? delay(fixtures.scans) : request("/scans")),
  overview: (scan = "latest"): Promise<Overview> =>
    fixtureMode() ? delay(fixtures.overview) : request(`/scans/${scan}/overview`),
  dependencies: (scan = "latest"): Promise<DependencyRow[]> =>
    fixtureMode() ? delay(fixtures.dependencies) : request(`/scans/${scan}/dependencies`),
  dependency: (id: number): Promise<DependencyEvent> =>
    fixtureMode() ? delay(fixtures.dependency) : request(`/dependency-updates/${id}`),
  upgrades: (status?: string): Promise<UpgradeSummary[]> =>
    fixtureMode()
      ? delay(status ? fixtures.upgrades.filter((u) => u.status === status) : fixtures.upgrades)
      : request(`/upgrades${status ? `?status=${status}` : ""}`),
  upgrade: (id: string): Promise<UpgradeDetail> =>
    fixtureMode() ? delay(fixtures.upgrade) : request(`/upgrades/${id}`),
  graph: (runId: string, scan: string): Promise<Graph> =>
    fixtureMode() ? delay(fixtures.graph) : request(`/scans/${scan}/graph?run_id=${runId}`),
  decide: (runId: string, body: DecisionRequest): Promise<{ status: string }> =>
    fixtureMode()
      ? delay({ status: "queued" })
      : request(`/upgrades/${runId}/decision`, { method: "POST", body: JSON.stringify(body) }),
  startScan: (repoUrl: string, extra: { branch?: string; scope?: ScanScope; only?: string[] } = {}):
    Promise<{ job_id: string; scan_request_id?: string | null; read_only?: boolean }> =>
    fixtureMode()
      ? delay({ job_id: "fixture", scan_request_id: fixtures.scanRequest.id, read_only: false })
      : request("/scans", {
        method: "POST",
        body: JSON.stringify({
          repo_url: repoUrl, only: extra.only?.length ? extra.only : null, branch: extra.branch || null,
          scope: extra.scope ?? null,
        }),
      }),
  checkRepo: (repoUrl: string): Promise<RepoCheck> =>
    fixtureMode()
      ? delay({ provider: "github", repo: "pilot-sandbox-shop", allowed: true, read_only: false, ok: true, detail: "3 branches", branches: ["main"] })
      : request("/scans/check", { method: "POST", body: JSON.stringify({ repo_url: repoUrl }) }),
  scanRequest: (id: string): Promise<ScanRequestOut> =>
    fixtureMode() ? delay(fixtures.scanRequest) : request(`/scan-requests/${id}`),
  cancelScan: (id: string): Promise<ScanRequestOut> =>
    fixtureMode() ? delay({ ...fixtures.scanRequest, status: "cancelled" }) : request(`/scan-requests/${id}/cancel`, { method: "POST" }),
  retryStage: (id: string, stage: string): Promise<ScanRequestOut> =>
    fixtureMode()
      ? delay(fixtures.scanRequest)
      : request(`/scan-requests/${id}/retry`, { method: "POST", body: JSON.stringify({ stage }) }),
  summary: (scan = "latest"): Promise<ScanSummary> =>
    fixtureMode() ? delay(fixtures.summary) : request(`/scans/${scan}/summary`),
  vulnerabilities: (scan = "all"): Promise<VulnList> =>
    fixtureMode() ? delay(fixtures.vulnerabilities) : request(scan === "all" ? "/vulnerabilities" : `/scans/${scan}/vulnerabilities`),
  sources: (scan = "latest"): Promise<SourceList> =>
    fixtureMode() ? delay(fixtures.sources) : request(`/scans/${scan}/sources`),
  confirmSource: (name: string) =>
    fixtureMode() ? delay({ confirmed: `npm/${name}` }) : request("/sources/confirm", { method: "POST", body: JSON.stringify({ ecosystem: "npm", name }) }),
  repos: (): Promise<RepoOut[]> => (fixtureMode() ? delay(fixtures.repos) : request("/repos")),
  channels: (repoId: string): Promise<ChannelsView> =>
    fixtureMode() ? delay(fixtures.channels) : request(`/repos/${repoId}/channels`),
  saveChannel: (repoId: string, kind: string, body: { enabled: boolean; config: Record<string, unknown>; secret_ref?: string | null }) =>
    fixtureMode() ? delay(fixtures.channels) : request<ChannelsView>(`/repos/${repoId}/channels/${kind}`, { method: "PUT", body: JSON.stringify(body) }),
  testChannel: (repoId: string, kind: string): Promise<{ ok: boolean; detail: string }> =>
    fixtureMode() ? delay({ ok: true, detail: "Connected (recorded)" }) : request(`/repos/${repoId}/channels/${kind}/test`, { method: "POST" }),
  installWebhook: (repoId: string): Promise<{ ok: boolean; detail: string }> =>
    fixtureMode() ? delay({ ok: true, detail: "Webhook installed (recorded)" }) : request(`/repos/${repoId}/webhook`, { method: "POST" }),
  publishConfluence: (repoId: string): Promise<{ ok: boolean; detail: string }> =>
    fixtureMode() ? delay({ ok: true, detail: "Report page created (recorded)" }) : request(`/repos/${repoId}/publish/confluence`, { method: "POST" }),
  updateSchedule: (id: string, body: { interval_minutes?: number; enabled?: boolean }): Promise<ScheduleOut> =>
    fixtureMode() ? delay(fixtures.channels.schedules[0]) : request(`/schedules/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  changes: (filter?: string): Promise<ChangeFeed> =>
    fixtureMode() ? delay(fixtures.changes) : request(`/changes${filter && filter !== "all" ? `?filter=${filter}` : ""}`),
  feedback: (body: {
    target_kind: "impact_finding" | "update_change";
    target_id: number;
    verdict: "correct" | "not_relevant";
    comment?: string;
  }): Promise<{ id: number }> =>
    fixtureMode() ? delay({ id: 1 }) : request("/feedback", { method: "POST", body: JSON.stringify(body) }),
  overrideSource: (body: { name: string; kind?: string; owner?: string; repo?: string; url?: string; directory?: string }) =>
    fixtureMode()
      ? delay({ saved: `npm/${body.name}` })
      : request("/sources/override", { method: "POST", body: JSON.stringify({ ecosystem: "npm", kind: "repository", ...body }) }),
  fixtureEvents: (): RunEvent[] => fixtures.events,
};
