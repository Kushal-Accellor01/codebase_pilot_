import { fixtures } from "./fixtures";
import type {
  DecisionRequest, DependencyEvent, DependencyRow, Graph, Overview, RunEvent, Scan, UpgradeDetail, UpgradeSummary,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
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
  graph: (runId: string, scan = "latest"): Promise<Graph> =>
    fixtureMode() ? delay(fixtures.graph) : request(`/scans/${scan}/graph?run_id=${runId}`),
  decide: (runId: string, body: DecisionRequest): Promise<{ status: string }> =>
    fixtureMode()
      ? delay({ status: "queued" })
      : request(`/upgrades/${runId}/decision`, { method: "POST", body: JSON.stringify(body) }),
  startScan: (repoUrl: string): Promise<{ job_id: string }> =>
    fixtureMode()
      ? delay({ job_id: "fixture" })
      : request("/scans", { method: "POST", body: JSON.stringify({ repo_url: repoUrl }) }),
  feedback: (body: {
    target_kind: "impact_finding" | "update_change";
    target_id: number;
    verdict: "correct" | "not_relevant";
    comment?: string;
  }): Promise<{ id: number }> =>
    fixtureMode() ? delay({ id: 1 }) : request("/feedback", { method: "POST", body: JSON.stringify(body) }),
  overrideSource: (body: { name: string; owner: string; repo: string; directory?: string }) =>
    fixtureMode()
      ? delay({ saved: `npm/${body.name}` })
      : request("/sources/override", { method: "POST", body: JSON.stringify({ ecosystem: "npm", ...body }) }),
  fixtureEvents: (): RunEvent[] => fixtures.events,
};
