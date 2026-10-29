import { Link, useParams } from "react-router-dom";

import { API_MODE } from "../api/client";
import { useDependencies, useScanSummary, useUpgrades, useVulns } from "../api/hooks";
import type { DependencyRow } from "../api/models";
import { Async, Empty, Journey, Pill, RiskBadge, plural } from "../components/ui";
import { VulnTable } from "../components/VulnTable";

function Dependencies({ rows }: { rows: DependencyRow[] }) {
  return (
    <div className="two">
      <div className="card" style={{ overflow: "hidden" }}>
        <div style={{ padding: "16px 18px", display: "flex", justifyContent: "space-between", gap: 8 }}>
          <h3 style={{ margin: 0 }}>External packages</h3><span className="small muted">{rows.length} declared</span>
        </div>
        {rows.length === 0 ? <Empty title="No declared packages" /> : null}
        {rows.map((d) => (
          <div className="deprow" key={d.id}>
            <b><Link to={d.run_id ? `/upgrades/${d.run_id}` : `/dependencies/${d.id}`}>{d.name}</Link><span className="kind-tag">{d.dependency_type}</span></b>
            {d.risk_level !== "none" ? <RiskBadge risk={d.risk_level} /> : <span className="small muted">No action</span>}
            <p>{d.current_version ?? "unknown version"}{d.recommended_target && d.recommended_target !== d.current_version ? ` → ${d.recommended_target}` : ""} · imported by {plural(d.importer_count, "file")}</p>
          </div>
        ))}
      </div>
      <div className="card" style={{ overflow: "hidden" }}>
        <div style={{ padding: "16px 18px", display: "flex", justifyContent: "space-between", gap: 8 }}>
          <h3 style={{ margin: 0 }}>Internal contracts</h3><span className="small muted">0 found</span>
        </div>
        <div style={{ padding: "0 18px 18px" }}>
          <Empty title="Not read in this version">
            OpenAPI and AsyncAPI specs, API clients, message topics and private packages are read in a later release. Nothing was guessed.
          </Empty>
        </div>
      </div>
    </div>
  );
}

export default function ScanResultsPage() {
  const { scanId = "latest" } = useParams();
  const summary = useScanSummary(scanId);
  const vulns = useVulns(scanId);
  const deps = useDependencies();
  const waiting = useUpgrades("awaiting_approval");
  return (
    <Async query={summary} what="results">
      {(s) => {
        const needs = s.needs_decision;
        return (
          <>
            <div className="crumbs"><Link to="/scan">Scans</Link> / {s.repo}{s.branch ? ` / ${s.branch}` : ""}</div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16, flexWrap: "wrap" }}>
              <div>
                <h1>{needs > 0 ? `${plural(needs, "upgrade")} ${needs === 1 ? "needs" : "need"} a decision` : s.repo}</h1>
                <p className="lede" style={{ marginBottom: 0 }}>
                  {s.repo}: {s.packages} packages, {s.declared} of them declared directly. Pilot checked each one for advisories and breaking changes.
                  {s.commit_sha ? <> Commit <code>{s.commit_sha.slice(0, 7)}</code>.</> : null}
                </p>
              </div>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                {s.has_sbom ? <a className="btn" href={`/api/scans/${s.scan_id}/sbom`} download>Download SBOM</a> : <button className="btn" disabled title="No bill of materials for this scan">Download SBOM</button>}
                <Link className="btn" to="/scan">Scan again</Link>
              </div>
            </div>
            <div className="note dashed" role="note" hidden={API_MODE !== "fixtures"}>Recorded sample data. Advisory rows are samples; a real scan fills them from OSV and Dependabot.</div>
            <div className="split" style={{ marginTop: 22 }}>
              <div style={{ minWidth: 0 }}>
                <div className="strip">
                  <a href="#vulns"><b>{s.advisories}</b><span>{plural(s.advisories, "advisory")}, {s.advisories_apply} apply to your code</span></a>
                  <a href="#upgrade"><b style={{ color: needs ? "var(--critical)" : undefined }}>{needs}</b><span>{needs === 1 ? "upgrade needs" : "upgrades need"} a decision</span></a>
                  <a href="#deps"><b>{s.internal_contracts}</b><span>internal contracts watched</span></a>
                  <Link to="/setup/sources"><b style={{ color: s.sources_to_confirm ? "var(--high)" : undefined }}>{s.sources_to_confirm}</b><span>sources to confirm</span></Link>
                </div>

                <section className="sec" id="upgrade" aria-labelledby="h-upgrade">
                  <div className="sechead"><h2 id="h-upgrade">Needs a decision</h2><span className="small muted">Sorted by risk, then effort</span></div>
                  <Async query={waiting} what="upgrades">
                    {(runs) => runs.length === 0 ? <Empty title="Nothing is waiting for you">Approved and rejected upgrades are under Upgrades.</Empty> : (
                      <div className="board">
                        {runs.map((r) => (
                          <Link key={r.id} className="board-row" to={`/upgrades/${r.id}`}>
                            <span className={`bar ${r.risk}`} aria-hidden="true" />
                            <span className="pkg">{r.package}</span>
                            <Journey from={r.from} to={r.to} kind="major" />
                            <span className="why">{r.status_label}</span>
                            <span className="state"><RiskBadge risk={r.risk} /></span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </Async>
                </section>

                <section className="sec" id="vulns" aria-labelledby="h-vulns">
                  <div className="sechead"><h2 id="h-vulns">Vulnerabilities</h2><span className="small muted">OSV and Dependabot alerts, every package in the lockfile</span></div>
                  <Async query={vulns} what="vulnerabilities">{(data) => <VulnTable data={data} />}</Async>
                </section>

                <section className="sec" id="deps" aria-labelledby="h-deps">
                  <div className="sechead"><h2 id="h-deps">Dependencies</h2><span className="small muted">What your code relies on, from outside and inside the company</span></div>
                  <Async query={deps} what="dependencies">{(rows) => <Dependencies rows={rows} />}</Async>
                </section>
              </div>
              <aside className="card" style={{ padding: 22 }}>
                <h2 style={{ marginBottom: 4 }}>Finish setting up</h2>
                <p className="small muted" style={{ margin: "0 0 10px" }}>So Pilot keeps watching after this scan.</p>
                <ol className="steps">
                  <li><span className="stepdot on" aria-hidden="true">✓</span><div><b>First scan</b><br /><span className="small muted">Done</span></div></li>
                  <li><span className="stepdot" aria-hidden="true">2</span><div><b>Confirm where changes come from</b><br /><span className="small muted">{s.sources_to_confirm === 0 ? "Every source is confirmed" : plural(s.sources_to_confirm, "package") + " need a source"}</span><br /><Link className="btn btn-primary btn-sm" to="/setup/sources" style={{ marginTop: 10 }}>Review source mapping</Link></div></li>
                  <li><span className="stepdot" aria-hidden="true">3</span><div><b>Connect channels</b><br /><span className="small muted">Webhook, Dependabot, Confluence, Slack</span><br /><Link to="/setup/channels" className="small">Set up channels</Link></div></li>
                  <li><span className="stepdot" aria-hidden="true">4</span><div><b>Watch the update feed</b><br /><span className="small muted">New changes, their impact and fixes</span><br /><Link to="/updates" className="small">Open the feed</Link></div></li>
                </ol>
                <p className="small muted" style={{ marginTop: 16 }}>
                  {s.scan_request_id ? <Link to={`/scan/${s.scan_request_id}`}>See how this scan ran</Link> : "Started from the command line."}
                </p>
                <Pill pill="done">Scan complete</Pill>
              </aside>
            </div>
          </>
        );
      }}
    </Async>
  );
}
