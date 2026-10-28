import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useDependencies } from "../api/hooks";
import type { DependencyRow } from "../api/models";
import { Async, Empty, RiskBadge } from "../components/ui";

const FILTERS = ["All", "Needs action", "Major", "Security", "Dev"] as const;
type Filter = (typeof FILTERS)[number];

export const REACH_LABEL: Record<string, string> = {
  reachable_in_code: "Reachable in code",
  imported: "Imported",
  declared_only: "Declared only",
};

const matches: Record<Filter, (d: DependencyRow) => boolean> = {
  All: () => true,
  "Needs action": (d) => !["up_to_date", "unsupported_ecosystem"].includes(d.status),
  Major: (d) => d.update_type === "major",
  Security: (d) => d.vuln_count > 0,
  Dev: (d) => d.dependency_type === "dev",
};

function Table({ rows }: { rows: DependencyRow[] }) {
  const [filter, setFilter] = useState<Filter>("All");
  const navigate = useNavigate();
  const shown = useMemo(() => rows.filter(matches[filter]), [rows, filter]);
  const open = (d: DependencyRow) => navigate(d.run_id ? `/upgrades/${d.run_id}` : `/dependencies/${d.id}`);
  return (
    <>
      <div className="filters" role="group" aria-label="Filter">
        {FILTERS.map((f) => (
          <button key={f} className="chip" aria-pressed={filter === f} onClick={() => setFilter(f)}>{f}</button>
        ))}
      </div>
      {shown.length === 0 ? (
        <Empty title="No dependencies match this filter" />
      ) : (
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>Risk</th><th>Package</th><th>Type</th><th>Current</th><th>Latest in range</th><th>Latest</th>
                <th>Update</th><th className="num">Advisories</th><th>Reachability</th><th className="num">Used in</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((d) => (
                <tr
                  key={d.id} className="link" tabIndex={0}
                  onClick={() => open(d)} onKeyDown={(e) => e.key === "Enter" && open(d)}
                >
                  <td><RiskBadge risk={d.risk_level} /></td>
                  <td><b>{d.name}</b></td>
                  <td>{d.dependency_type}</td>
                  <td>{d.current_version ?? "-"}</td>
                  <td>{d.latest_in_range ?? "-"}</td>
                  <td>{d.latest_version ?? "-"}</td>
                  <td>{d.update_type && d.update_type !== "none" ? d.update_type : "—"}</td>
                  <td className="num">{d.vuln_count || "—"}</td>
                  <td>{REACH_LABEL[d.reachability]}</td>
                  <td className="num">{d.importer_count} files</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function DependenciesPage() {
  const query = useDependencies();
  return (
    <>
      <h1>Dependencies</h1>
      <p className="lede">Every package this repository declares, what it is on now, and what is available.</p>
      <Async query={query} what="dependencies">{(rows) => <Table rows={rows} />}</Async>
    </>
  );
}
