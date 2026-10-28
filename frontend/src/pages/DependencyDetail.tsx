import { useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { useDependency, useFeedback } from "../api/hooks";
import type { DependencyEvent } from "../api/models";
import { Async, Empty, Journey, RiskBadge } from "../components/ui";
import { useToast } from "../components/Shell";

function SourceForm({ name }: { name: string }) {
  const toast = useToast();
  const client = useQueryClient();
  const [value, setValue] = useState("");
  const save = useMutation({
    mutationFn: () => {
      const [owner, repo] = value.replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "").split("/");
      return api.overrideSource({ name, owner, repo });
    },
    onSuccess: () => {
      toast("Saved. The next run will use this repository.");
      void client.invalidateQueries();
    },
    onError: (e: Error) => toast(`Could not save: ${e.message}`),
  });
  const valid = /^[\w.-]+\/[\w.-]+$/.test(value.replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, ""));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (valid) save.mutate();
  };
  return (
    <form onSubmit={submit} style={{ maxWidth: 460 }}>
      <h3>Tell Pilot where {name} lives</h3>
      <p className="small muted">Pilot could not find this package's source repository, so it cannot read release notes.</p>
      <label htmlFor="source">GitHub repository (owner/name)</label>
      <input id="source" type="text" value={value} onChange={(e) => setValue(e.target.value)} placeholder="expressjs/express" />
      <p><button className="btn btn-primary" disabled={!valid || save.isPending}>Save repository</button></p>
    </form>
  );
}

function Detail({ d }: { d: DependencyEvent }) {
  const feedback = useFeedback();
  const toast = useToast();
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const notRelevant = (id: number) =>
    feedback.mutate(
      { target_kind: "update_change", target_id: id, verdict: "not_relevant" },
      { onSuccess: () => { setDismissed((s) => new Set(s).add(id)); toast("Thanks. Marked as not relevant."); } },
    );
  return (
    <>
      <div className="crumbs"><Link to="/dependencies">Dependencies</Link> / {d.package}</div>
      <h1>{d.package}</h1>
      <p style={{ margin: "0 0 8px" }}>
        <Journey from={d.current_version} to={d.target_version} kind={d.update_type} />
      </p>
      <div className="facts">
        <div className="fact">Risk<b><RiskBadge risk={d.risk_level} /></b></div>
        <div className="fact">Type<b>{d.dependency_type}</b></div>
        <div className="fact">Declared<b className="mono">{d.declared_constraint ?? "-"}</b></div>
        <div className="fact">Version known from<b>{d.current_version_basis.replace(/_/g, " ")}</b></div>
        <div className="fact">Used in<b>{d.importer_count} files</b></div>
      </div>
      {d.summary ? <p className="lede" style={{ marginTop: 20 }}>{d.summary}</p> : null}
      {d.run_id ? <p><Link to={`/upgrades/${d.run_id}`}>Open the upgrade run</Link></p> : null}

      {d.status === "source_unknown" ? <SourceForm name={d.package} /> : null}
      {d.status === "notes_unavailable" ? (
        <Empty title="No release notes found">Pilot can still test the upgrade, but it cannot list what changed.</Empty>
      ) : null}
      {d.error ? <div className="error-box" role="alert">{d.error}</div> : null}

      <h2 style={{ marginTop: 28 }}>What changed</h2>
      {d.changes.length === 0 ? (
        <Empty title="No change list">Nothing was extracted from release notes for this upgrade.</Empty>
      ) : (
        d.changes.map((c, i) => (
          <div key={c.id ?? i} className={`change ${c.breaking ? "breaking" : ""}`}>
            <h3>
              {c.description}
              <span className="small muted">in {c.version}</span>
              {c.breaking ? <span className="small">breaking</span> : null}
            </h3>
            <blockquote>{c.evidence_quote}</blockquote>
            <a className="small" href={c.evidence_url} target="_blank" rel="noreferrer">Open the release notes</a>
            {c.id != null && !dismissed.has(c.id) ? (
              <>
                {" · "}
                <button className="linkbtn" onClick={() => notRelevant(c.id!)}>Not relevant</button>
              </>
            ) : null}
          </div>
        ))
      )}

      <h2 style={{ marginTop: 28 }}>Vulnerabilities</h2>
      {d.vulnerabilities.length === 0 ? (
        <p className="small muted">No known advisories for {d.current_version}.</p>
      ) : (
        <div className="tablewrap">
          <table>
            <thead><tr><th>Advisory</th><th>Severity</th><th>Fixed in</th><th>Summary</th></tr></thead>
            <tbody>
              {d.vulnerabilities.map((v) => (
                <tr key={v.id}>
                  <td><a href={v.url ?? "#"} target="_blank" rel="noreferrer">{v.id}</a></td>
                  <td>{v.severity}</td><td>{v.fixed_in ?? "no fix yet"}</td><td>{v.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 style={{ marginTop: 28 }}>Files that import it</h2>
      {d.importing_files.length === 0 ? (
        <p className="small muted">No file imports this package directly (build tools and view engines often load by name).</p>
      ) : (
        <ul>{d.importing_files.map((f) => (
          <li key={f.path}><code>{f.path}</code>{f.lines.length ? <span className="muted"> line {f.lines.join(", ")}</span> : null}</li>
        ))}</ul>
      )}
    </>
  );
}

export default function DependencyDetailPage() {
  const id = Number(useParams().id);
  const query = useDependency(id);
  return <Async query={query} what="dependency">{(d) => <Detail d={d} />}</Async>;
}
