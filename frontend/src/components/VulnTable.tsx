import { useState } from "react";

import type { VulnList, VulnRow } from "../api/models";
import { Empty, RiskBadge } from "./ui";

const FILTERS = [
  { key: "all", label: "All", test: () => true },
  { key: "applies", label: "Applies to your code", test: (r: VulnRow) => r.verdict === "applies" },
  { key: "runtime", label: "Runtime", test: (r: VulnRow) => r.scope !== "dev" },
  { key: "development", label: "Development only", test: (r: VulnRow) => r.scope === "dev" },
  { key: "transitive", label: "Transitive", test: (r: VulnRow) => !r.direct },
] as const;

const VERDICT: Record<string, { label: string; color: string }> = {
  applies: { label: "Applies", color: "var(--critical)" },
  does_not_apply: { label: "Does not apply", color: "var(--low)" },
  not_shipped: { label: "Not shipped", color: "var(--ink)" },
  undetermined: { label: "Undetermined", color: "var(--high)" },
};
const SCOPE_LABEL: Record<string, string> = { runtime: "runtime", dev: "development only", optional: "optional", peer: "peer" };

export function severityRisk(severity: string): string {
  return ["critical", "high", "medium", "low"].includes(severity) ? severity : "none";
}

export function VulnTable({ data, showRepo = false }: { data: VulnList; showRepo?: boolean }) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const active = FILTERS.find((f) => f.key === filter) ?? FILTERS[0];
  const rows = data.rows.filter(active.test);
  if (data.rows.length === 0) {
    return <Empty title={`No known advisories in ${data.packages} packages`}>Pilot checks every package in the lockfile against OSV, and Dependabot alerts when a token can read them.</Empty>;
  }
  return (
    <>
      <div className="filters" role="group" aria-label="Filter advisories">
        {FILTERS.map((f) => (
          <button key={f.key} className="chip" aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>
            {f.label} {data.counts[f.key]}
          </button>
        ))}
      </div>
      <div className="tablewrap">
        <table className="wide">
          <thead>
            <tr>
              <th>Severity</th><th>Advisory</th>{showRepo ? <th>Repository</th> : null}<th>Package</th>
              <th>How it gets in</th><th>Applies to your code?</th><th>Fixed in</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const verdict = VERDICT[r.verdict] ?? { label: r.verdict, color: "var(--ink)" };
              return (
                <tr key={`${r.scan_id ?? ""}${r.vuln_id}${r.name}${r.version}`}>
                  <td><RiskBadge risk={severityRisk(r.severity)} /></td>
                  <td>
                    <b>{r.url ? <a href={r.url} target="_blank" rel="noreferrer">{r.vuln_id}</a> : r.vuln_id}</b>
                    <br /><span className="small muted">{r.summary}</span>
                    {r.sources.length > 1 ? <><br /><span className="small muted">Reported by {r.sources.join(" and ")}</span></> : null}
                  </td>
                  {showRepo ? <td>{r.repo}</td> : null}
                  <td><code>{r.name}</code> {r.version}</td>
                  <td className="path">{r.path.join(" › ")}<br /><span className="muted">{SCOPE_LABEL[r.scope] ?? r.scope}</span></td>
                  <td className="verdict"><b style={{ color: verdict.color }}>{verdict.label}</b><span>{r.reason}</span></td>
                  <td>{r.fixed_in ?? "No fix yet"}</td>
                </tr>
              );
            })}
            {rows.length === 0 ? <tr><td colSpan={showRepo ? 7 : 6} className="muted">No advisories match this filter.</td></tr> : null}
          </tbody>
        </table>
      </div>
    </>
  );
}
