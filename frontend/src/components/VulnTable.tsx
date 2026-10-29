import { useMemo, useState } from "react";

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

const LEGEND: [string, string][] = [
  ["Applies", "the vulnerable package is on a path your code loads at runtime"],
  ["Does not apply", "it is only reachable through optional packages your code never imports"],
  ["Not shipped", "it is only reachable through development tools"],
  ["Undetermined", "Pilot cannot decide, for example a dynamic require or no lockfile"],
];
const SEVERITY_ORDER = ["critical", "high", "medium", "low"];
const VERDICT_ORDER = ["applies", "undetermined", "does_not_apply", "not_shipped"];
type SortKey = "severity" | "package" | "verdict";

function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button" className="btn btn-sm" aria-label={`Copy ${label}`} style={{ marginLeft: 6, minHeight: 28, padding: "0 8px" }}
      onClick={() => { void navigator.clipboard?.writeText(text).then(() => { setDone(true); window.setTimeout(() => setDone(false), 1500); }); }}
    >
      {done ? "Copied" : "Copy"}
    </button>
  );
}

function SortHead({ label, sort, by, set }: { label: string; sort: SortKey | null; by: SortKey; set: (k: SortKey) => void }) {
  return (
    <th aria-sort={sort === by ? "ascending" : "none"}>
      <button type="button" className="linkbtn" onClick={() => set(by)}>{label}{sort === by ? " ▲" : ""}</button>
    </th>
  );
}

export function VulnTable({ data, showRepo = false }: { data: VulnList; showRepo?: boolean }) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const active = FILTERS.find((f) => f.key === filter) ?? FILTERS[0];
  const [sort, setSort] = useState<SortKey | null>(null);
  const rows = useMemo(() => {
    const filtered = data.rows.filter(active.test);
    if (!sort) return filtered;
    const rank = (r: VulnRow) => sort === "severity" ? SEVERITY_ORDER.indexOf(r.severity)
      : sort === "verdict" ? VERDICT_ORDER.indexOf(r.verdict) : 0;
    const key = (r: VulnRow) => (sort === "package" ? 0 : (rank(r) < 0 ? 99 : rank(r)));
    return [...filtered].sort((a, b) => key(a) - key(b) || (sort === "package" ? a.name.localeCompare(b.name) : 0));
  }, [data.rows, active, sort]);
  if (data.rows.length === 0 && data.packages === 0) {
    return <Empty title="No packages were checked">This repository has no lockfile (package-lock.json), so Pilot cannot list its packages. Commit a lockfile and scan again.</Empty>;
  }
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
      <details className="legend" style={{ margin: "0 0 12px" }}>
        <summary className="small">What do the verdicts mean?</summary>
        <dl className="small" style={{ margin: "8px 0 0" }}>
          {LEGEND.map(([word, text]) => <div key={word}><dt style={{ display: "inline", fontWeight: 700 }}>{word}: </dt><dd style={{ display: "inline", margin: 0 }}>{text}</dd></div>)}
        </dl>
      </details>
      <div className="tablewrap">
        <table className="wide sticky">
          <thead>
            <tr>
              <SortHead label="Severity" sort={sort} by="severity" set={setSort} /><th>Advisory</th>{showRepo ? <th>Repository</th> : null}
              <SortHead label="Package" sort={sort} by="package" set={setSort} />
              <th>How it gets in</th><SortHead label="Applies to your code?" sort={sort} by="verdict" set={setSort} /><th>Fixed in</th>
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
                  <td><code>{r.name}</code> {r.version}<CopyButton text={`${r.name}@${r.version}`} label={`${r.name}@${r.version}`} /></td>
                  <td className="path">{r.path.join(" › ")}<br /><span className="muted">{SCOPE_LABEL[r.scope] ?? r.scope}</span></td>
                  <td className="verdict"><b style={{ color: verdict.color }}>{verdict.label}</b><span>{r.reason}</span></td>
                  <td>{r.fixed_in ? <>{r.fixed_in}<CopyButton text={`npm install ${r.name}@${r.fixed_in}`} label="install command" /></> : "No fix yet"}</td>
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
