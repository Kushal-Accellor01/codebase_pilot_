import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { useSources } from "../api/hooks";
import type { SourceRow } from "../api/models";
import { useToast } from "../components/Shell";
import { Async, Empty, Pill } from "../components/ui";

const KINDS = [
  { value: "repository", label: "GitHub repository", field: "Repository", placeholder: "owner/repo, for example acme/money" },
  { value: "changelog_url", label: "Changelog URL", field: "Changelog URL", placeholder: "https://example.com/changelog" },
  { value: "confluence", label: "Confluence page", field: "Page link", placeholder: "https://acme.atlassian.net/wiki/spaces/PAY/pages/123" },
  { value: "slack", label: "Slack channel", field: "Channel", placeholder: "#payments-changes" },
] as const;

const STATUS: Record<string, { label: string; pill: string }> = {
  verified: { label: "Verified", pill: "done" },
  confirm: { label: "Confirm", pill: "wait" },
  not_found: { label: "Not found", pill: "" },
  saved: { label: "Saved", pill: "run" },
};

export function SourceForm({ row, onDone }: { row: SourceRow; onDone: () => void }) {
  const [kind, setKind] = useState<(typeof KINDS)[number]["value"]>("repository");
  const [value, setValue] = useState("");
  const toast = useToast();
  const client = useQueryClient();
  const current = KINDS.find((k) => k.value === kind)!;
  const save = useMutation({
    mutationFn: () => {
      const text = value.trim();
      if (kind === "repository") {
        const [owner, repo] = text.replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "").split("/");
        return api.overrideSource({ name: row.name, kind, owner, repo });
      }
      return api.overrideSource({ name: row.name, kind, url: text });
    },
    onSuccess: () => {
      toast(`Saved the source for ${row.name}. It is used from the next scan.`);
      void client.invalidateQueries({ queryKey: ["sources"] });
      onDone();
    },
    onError: (error: Error) => toast(`Could not save: ${error.message}`),
  });
  const valid = kind === "repository" ? /^[\w.-]+\/[\w.-]+/.test(value.replace(/^https?:\/\/github\.com\//, "")) : value.trim().length > 2;
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (valid) save.mutate(); }} style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
      <div style={{ width: 200 }}>
        <label className="label small" htmlFor={`kind-${row.name}`}>Source type</label>
        <select id={`kind-${row.name}`} className="field" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
          {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
        </select>
      </div>
      <div style={{ flex: "1 1 240px" }}>
        <label className="label small" htmlFor={`val-${row.name}`}>{current.field}</label>
        <input id={`val-${row.name}`} className="field" value={value} placeholder={current.placeholder} onChange={(e) => setValue(e.target.value)} />
      </div>
      {kind !== "repository" ? (
        <p className="small muted" role="note" style={{ flexBasis: "100%", margin: 0 }}>
          Saved. Not read in this version: Pilot keeps this link but still reads the package's repository for changes.
        </p>
      ) : null}
      <button className="btn btn-primary btn-sm" type="submit" disabled={!valid || save.isPending}>Save</button>
    </form>
  );
}

function Row({ row }: { row: SourceRow }) {
  const [editing, setEditing] = useState(false);
  const toast = useToast();
  const client = useQueryClient();
  const confirm = useMutation({
    mutationFn: () => api.confirmSource(row.name),
    onSuccess: () => { toast(`Confirmed the source for ${row.name}.`); void client.invalidateQueries({ queryKey: ["sources"] }); },
    onError: (error: Error) => toast(`Could not confirm: ${error.message}`),
  });
  const status = STATUS[row.status] ?? { label: row.status, pill: "" };
  const ask = row.status === "confirm" || row.status === "not_found";
  return (
    <tr className={ask ? "ask" : undefined}>
      <td><b>{row.name}</b></td>
      <td><span className="kind-tag">{row.kind}</span></td>
      {editing || row.status === "not_found" ? (
        <td colSpan={3}>
          <SourceForm row={row} onDone={() => setEditing(false)} />
          {row.note ? <p className="small muted" style={{ margin: "8px 0 0" }}>{row.note}</p> : null}
        </td>
      ) : (
        <>
          <td>
            <span className="mono">{row.source ?? "—"}</span>
            <br /><span className="small muted">{row.detail}</span>
            {row.note ? <><br /><span className="small muted">{row.note}</span></> : null}
          </td>
          <td className="small">{row.found_by}</td>
          <td><Pill pill={status.pill}>{status.label}</Pill></td>
        </>
      )}
      <td>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {row.status === "confirm" ? <button className="btn btn-primary btn-sm" onClick={() => confirm.mutate()} disabled={confirm.isPending}>Confirm</button> : null}
          {row.status !== "not_found" ? <button className="btn btn-sm" onClick={() => setEditing(!editing)}>{editing ? "Cancel" : "Change"}</button> : null}
        </div>
      </td>
    </tr>
  );
}

export default function SourceMappingPage() {
  const query = useSources();
  return (
    <>
      <h1>Where each dependency publishes its changes</h1>
      <p className="lede">Pilot reads release notes and changelogs from these places. Check the ones it found and fill in the ones it could not; it reads them again on every new version.</p>
      <div className="legend-row" style={{ display: "flex", gap: 20, flexWrap: "wrap", margin: "0 0 14px", fontSize: 13, color: "var(--ink-2)" }} aria-label="Legend">
        <span><Pill pill="done">Verified</Pill> repository exists and matches the package</span>
        <span><Pill pill="wait">Confirm</Pill> found, but a person should check</span>
        <span><Pill pill="">Not found</Pill> tell Pilot where to look</span>
      </div>
      <Async query={query} what="source mapping">
        {(data) => data.rows.length === 0 ? (
          <Empty title="No dependencies yet">Scan a repository first, then check where its packages publish changes.</Empty>
        ) : (
          <>
            <div className="tablewrap">
              <table className="wide">
                <thead><tr><th>Dependency</th><th>Kind</th><th>Where changes come from</th><th>Found by</th><th>Status</th><th><span className="muted">Action</span></th></tr></thead>
                <tbody>{data.rows.map((r) => <Row key={`${r.ecosystem}/${r.name}`} row={r} />)}</tbody>
              </table>
            </div>
            <p className="small muted" style={{ margin: "14px 0 0" }}>
              Transitive packages are resolved when an upgrade or advisory needs them. Changes are saved to <code>config/package_sources.yaml</code> and used from the next scan.
            </p>
          </>
        )}
      </Async>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginTop: 30, paddingTop: 22, borderTop: "1px solid var(--rule)" }}>
        <Link className="btn" to="/scans/latest">Back to results</Link>
        <Link className="btn btn-primary" to="/setup/channels">Continue: connect channels</Link>
      </div>
    </>
  );
}
