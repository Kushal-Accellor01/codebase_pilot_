import { Link, useNavigate } from "react-router-dom";

import { useUpgrades } from "../api/hooks";
import type { UpgradeSummary } from "../api/models";
import { Async, Empty, Pill, RiskBadge } from "../components/ui";

function Table({ rows }: { rows: UpgradeSummary[] }) {
  const navigate = useNavigate();
  return (
    <div className="tablewrap">
      <table>
        <thead>
          <tr><th>Risk</th><th>Upgrade</th><th>Status</th><th className="num">Effort</th></tr>
        </thead>
        <tbody>
          {rows.map((u) => (
            <tr
              key={u.id} className="link" tabIndex={0}
              onClick={() => navigate(`/upgrades/${u.id}`)} onKeyDown={(e) => e.key === "Enter" && navigate(`/upgrades/${u.id}`)}
            >
              <td><RiskBadge risk={u.risk} /></td>
              <td><b>{u.package}</b> {u.from} to {u.to}</td>
              <td><Pill pill={u.pill}>{u.status_label}</Pill></td>
              <td className="num">{u.effort_hours ? `${u.effort_hours} h` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function UpgradesPage() {
  const query = useUpgrades();
  return (
    <>
      <h1>Upgrades</h1>
      <p className="lede">One row per dependency upgrade Pilot is analysing or has finished.</p>
      <Async query={query} what="upgrades">
        {(rows) => rows.length === 0 ? (
          <Empty title="No upgrades yet">Scan a repository and Pilot will queue the ones that need attention.</Empty>
        ) : <Table rows={rows} />}
      </Async>
    </>
  );
}

export function ApprovalsPage() {
  const query = useUpgrades("awaiting_approval");
  return (
    <>
      <h1>Approvals</h1>
      <p className="lede">Upgrades whose fix passed its tests and need a person before Pilot opens a pull request.</p>
      <Async query={query} what="approvals">
        {(rows) => rows.length === 0 ? (
          <Empty title="Nothing is waiting">Approved and rejected upgrades are listed under Upgrades.</Empty>
        ) : (
          <div className="tablewrap">
            <table>
              <thead><tr><th>Upgrade</th><th>Risk</th><th>Waiting since</th><th /></tr></thead>
              <tbody>
                {rows.map((u) => (
                  <tr key={u.id}>
                    <td><b>{u.package}</b> {u.from} to {u.to}</td>
                    <td><RiskBadge risk={u.risk} /></td>
                    <td>{u.updated_at.slice(0, 16).replace("T", " ")} UTC</td>
                    <td className="num"><Link to={`/upgrades/${u.id}`}>Review</Link></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
    </>
  );
}
