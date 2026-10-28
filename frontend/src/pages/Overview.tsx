import { Link } from "react-router-dom";

import { useOverview } from "../api/hooks";
import type { BoardItem, Overview as OverviewData } from "../api/models";
import { Async, Empty, Journey, Pill, RISK_LABEL, RiskBadge } from "../components/ui";

const RISKS = ["critical", "high", "medium", "low", "none"] as const;

function Row({ item }: { item: BoardItem }) {
  const href = item.run_id ? `/upgrades/${item.run_id}` : `/dependencies/${item.id}`;
  const [before, after] = item.why_bold && item.why.includes(item.why_bold) ? item.why.split(item.why_bold) : [item.why, ""];
  return (
    <Link className="board-row" role="listitem" to={href}>
      <span className={`bar ${item.risk}`} aria-hidden="true" />
      <span className="pkg">
        {item.package}
        <small>{item.type} dependency</small>
      </span>
      <Journey from={item.from} to={item.to} kind={item.kind} />
      <span className="why">
        {before}
        {item.why_bold ? <b>{item.why_bold}</b> : null}
        {after}
      </span>
      <span className="state">
        <RiskBadge risk={item.risk} />
        <br />
        <Pill pill={item.pill}>{item.status_label}</Pill>
      </span>
    </Link>
  );
}

function Content({ data }: { data: OverviewData }) {
  const total = Math.max(data.total, 1);
  return (
    <>
      <h1>{data.headline}</h1>
      <p className="lede">{data.lede}</p>
      <div className="overview">
        <div>
          {data.upgrades.length === 0 ? (
            <Empty title="No upgrades need attention">Everything Pilot checked is up to date or low risk.</Empty>
          ) : (
            <div className="board" role="list" aria-label="Upgrades by risk">
              <div className="board-head">
                <h2 style={{ margin: 0 }}>Upgrades</h2>
                <span className="small muted">Sorted by risk, then effort</span>
              </div>
              {data.upgrades.map((u) => <Row key={`${u.id}`} item={u} />)}
            </div>
          )}
          <h2 style={{ marginTop: 32 }}>Most affected files</h2>
          {data.affected_files.length === 0 ? (
            <Empty title="No affected files">No line of your code uses an API that changes.</Empty>
          ) : (
            <div className="tablewrap">
              <table>
                <thead>
                  <tr><th>File</th><th className="num">Lines</th><th>Packages</th><th>Strongest evidence</th></tr>
                </thead>
                <tbody>
                  {data.affected_files.map((f) => (
                    <tr key={f.path}>
                      <td className="mono">{f.path}</td>
                      <td className="num">{f.lines}</td>
                      <td>{f.packages.join(", ")}</td>
                      <td>{f.strongest_evidence}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <aside className="side">
          <section className="waiting">
            <h2>Waiting for you</h2>
            {data.waiting.length === 0 ? (
              <p className="small muted">Nothing is waiting for a decision.</p>
            ) : (
              data.waiting.map((w) => (
                <Link key={w.id} to={`/upgrades/${w.run_id}`}>
                  <span className="t">{w.package} {w.from} to {w.to}</span>
                  <br />
                  <span className="small muted">{w.detail}</span>
                </Link>
              ))
            )}
          </section>
          <section>
            <h2>Risk across {data.total} {data.total === 1 ? "dependency" : "dependencies"}</h2>
            <div
              className="stack" role="img"
              aria-label={RISKS.map((k) => `${data.risk[k] ?? 0} ${RISK_LABEL[k].toLowerCase()}`).join(", ")}
            >
              {RISKS.map((k) => (
                <span key={k} style={{ width: `${((data.risk[k] ?? 0) / total) * 100}%`, background: `var(--${k === "none" ? "rule" : k})` }} />
              ))}
            </div>
            <dl className="legend">
              {RISKS.map((k) => (
                <div key={k} style={{ display: "contents" }}>
                  <dt><span className="dot" style={{ background: `var(--${k === "none" ? "rule" : k})` }} />{RISK_LABEL[k]}</dt>
                  <dd>{data.risk[k] ?? 0}</dd>
                </div>
              ))}
            </dl>
          </section>
          <section>
            <h2>Security</h2>
            {data.security.length === 0 ? (
              <p className="small muted" style={{ margin: 0 }}>No known advisories for the versions in use.</p>
            ) : (
              data.security.map((s) => (
                <p key={s.package} className="small" style={{ margin: "0 0 8px" }}>
                  <RiskBadge risk={s.severity === "critical" || s.severity === "high" ? s.severity : "medium"} />{" "}
                  {s.count} {s.count === 1 ? "advisory" : "advisories"} in <b>{s.package} {s.version}</b>
                  {s.fixed_in ? <>, fixed in {s.fixed_in}.</> : "."}
                </p>
              ))
            )}
          </section>
        </aside>
      </div>
    </>
  );
}

export default function OverviewPage() {
  const query = useOverview();
  return <Async query={query} what="overview">{(data) => <Content data={data} />}</Async>;
}
