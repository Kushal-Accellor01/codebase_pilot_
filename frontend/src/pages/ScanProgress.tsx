import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { SCAN_DONE, useScanRequest } from "../api/hooks";
import type { ScanStage } from "../api/models";
import { Async, Pill } from "../components/ui";

const STAGE_TEXT: Record<string, { title: string; layer?: string }> = {
  clone: { title: "Clone the repository" },
  layer1: { title: "Structure graph", layer: "Layer 1" },
  sbom: { title: "Bill of materials" },
  internal: { title: "Internal dependencies" },
  layer2: { title: "Symbols and references", layer: "Layer 2" },
  vulnerabilities: { title: "Vulnerabilities" },
  versions: { title: "Versions and sources" },
  changes: { title: "What changed", layer: "Layer 3" },
  impact: { title: "Impact on your code" },
};
const WHEN: Record<string, string> = { done: "done", skipped: "skipped", running: "running", queued: "queued", error: "failed" };

function Icon({ status }: { status: string }) {
  if (status === "done") {
    return (
      <span className="ico done" aria-hidden="true">
        <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2l2.2 2.2 4.8-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </span>
    );
  }
  return <span className={`ico ${status}`} aria-hidden="true">{status === "error" ? "!" : null}</span>;
}

export function Stage({ stage }: { stage: ScanStage }) {
  const text = STAGE_TEXT[stage.stage] ?? { title: stage.stage };
  return (
    <li className={`stage ${stage.status}`} aria-current={stage.status === "running" ? "step" : undefined}>
      <Icon status={stage.status} />
      <div>
        <b>{text.title}{text.layer ? <span className="tag">{text.layer}</span> : null}</b>
        <p>{stage.summary ?? (stage.status === "queued" ? "Waiting for the earlier steps" : "")}</p>
      </div>
      <span className="small when" style={{ color: stage.status === "running" ? "var(--medium)" : stage.status === "error" ? "var(--critical)" : "var(--ink-3)", fontWeight: 600 }}>
        {WHEN[stage.status] ?? stage.status}
      </span>
    </li>
  );
}

export default function ScanProgressPage() {
  const { requestId = "" } = useParams();
  const query = useScanRequest(requestId);
  const client = useQueryClient();
  const navigate = useNavigate();
  const cancel = useMutation({
    mutationFn: () => api.cancelScan(requestId),
    onSuccess: (data) => client.setQueryData(["scan-request", requestId], data),
  });
  return (
    <Async query={query} what="scan">
      {(scan) => {
        const ended = SCAN_DONE.includes(scan.status);
        const percent = Math.round((scan.steps_done / scan.steps_total) * 100);
        const pill = scan.status === "failed" ? "fail" : scan.status === "cancelled" ? "" : scan.status === "complete" ? "done" : "run";
        const label = { queued: "Queued", running: "Scanning", complete: "Scan complete", failed: "Scan failed", cancelled: "Cancelled" }[scan.status] ?? scan.status;
        return (
          <>
            <div className="crumbs"><Link to="/scan">Scan a repository</Link> / {scan.repo}</div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16, flexWrap: "wrap" }}>
              <div>
                <h1>{ended ? (scan.status === "complete" ? `Scanned ${scan.repo}` : `Scan of ${scan.repo} ${scan.status}`) : `Scanning ${scan.repo}`}</h1>
                <p className="lede" style={{ marginBottom: 0 }}>
                  {scan.branch ? <>Branch <code>{scan.branch}</code>. </> : null}
                  You can leave this page; the scan keeps going and the results appear under Dependencies and Vulnerabilities.
                </p>
              </div>
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                <Pill pill={pill}>{label}</Pill>
                {!ended ? (
                  <button className="btn" onClick={() => cancel.mutate()} disabled={cancel.isPending}>Cancel scan</button>
                ) : null}
              </div>
            </div>
            <div className="progress" role="progressbar" aria-label="Scan progress" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
              <span style={{ width: `${percent}%` }} />
            </div>
            <p className="small muted" role="status" aria-live="polite" style={{ margin: "0 0 26px" }}>
              {scan.steps_done} of {scan.steps_total} steps done{scan.stages.find((s) => s.status === "running") ? `. Now: ${STAGE_TEXT[scan.stages.find((s) => s.status === "running")!.stage]?.title ?? ""}` : ""}
            </p>
            {scan.error ? <div className="error-box" role="alert" style={{ marginBottom: 20 }}>{scan.error}</div> : null}
            <div className="split wide">
              <ol className="card stages" aria-label="Scan steps">
                {scan.stages.map((s) => <Stage key={s.stage} stage={s} />)}
              </ol>
              <aside style={{ display: "flex", flexDirection: "column", gap: 20 }}>
                <section>
                  <h2>Found so far</h2>
                  <div className="found">
                    <div><b>{scan.found.packages}</b><span>packages in the lockfile</span></div>
                    <div><b>{scan.found.declared}</b><span>declared in package.json</span></div>
                    <div><b>{scan.found.imports}</b><span>imports traced</span></div>
                    <div><b>{scan.found.internal_contracts}</b><span>internal contracts</span></div>
                  </div>
                </section>
                <section>
                  <h2>Activity</h2>
                  <pre className="log" aria-label="Scan log">
                    {scan.stages.filter((s) => s.status !== "queued").map((s) => `${s.stage.padEnd(16)}${(WHEN[s.status] ?? s.status).padEnd(9)}${s.summary ?? ""}`).join("\n") || "Waiting for the worker…"}
                  </pre>
                </section>
              </aside>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 28 }}>
              <button className="btn btn-primary" disabled={!scan.scan_id} onClick={() => navigate(`/scans/${scan.scan_id}`)}>
                {ended ? "See the results" : "See results so far"}
              </button>
            </div>
          </>
        );
      }}
    </Async>
  );
}
