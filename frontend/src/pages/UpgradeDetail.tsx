import { useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { useFeedback, useGraph, useHealth, useRunEvents, useUpgrade } from "../api/hooks";
import {
  asEvent, asFindings, asPatches, asPlan, asTelemetry, asTests,
  type Finding, type ProbeSummary, type RunEvent, type UpgradeDetail,
} from "../api/models";
import { BlastRadius } from "../components/BlastRadius";
import { DecisionDialog } from "../components/DecisionDialog";
import { useToast } from "../components/Shell";
import {
  Async, Empty, ErrorBox, Journey, Loading, Pill, RiskBadge, SOURCE_LABEL, highlight, plural, shortTime,
} from "../components/ui";

const TABS = ["Affected code", "What changed", "Blast radius", "Production evidence", "Plan", "Fix and tests", "Activity"] as const;
type Tab = (typeof TABS)[number];
const ALL_SOURCES = ["pattern", "runtime_deprecation", "test_failure"] as const;

function testsText(p: ProbeSummary | null | undefined): { big: string; small: string; color: string } {
  if (!p) return { big: "Not run", small: "", color: "var(--ink-3)" };
  const ok = Boolean(p.passed);
  const counts = p.tests_total ? `${p.tests_passed ?? 0} of ${p.tests_total} pass` : ok ? "Passed" : "Failed";
  return { big: counts, small: ok ? "All green" : "Some tests fail", color: ok ? "var(--low)" : "var(--critical)" };
}

/** B-S5: with no model, plan text comes from templates and no fix is proposed; say so instead of showing nothing. */
function NoModelNotice({ what }: { what: "plan" | "fix" }) {
  const health = useHealth().data;
  if (!health || health.model_configured) return null;
  return (
    <p className="small" role="note" style={{ padding: "10px 14px", border: "1px solid var(--rule)", borderRadius: 8, background: "var(--panel)" }}>
      <b>No model configured:</b>{" "}
      {what === "plan" ? "this plan is from templates." : "no fix was proposed."} Set <code>LLM_PROVIDER</code> and its key in <code>backend/.env</code> to
      let Pilot read release notes and write fixes.
    </p>
  );
}

// --- tabs ---------------------------------------------------------------------------------------------------------

function AffectedCode({ detail, only, clear }: { detail: UpgradeDetail; only: string | null; clear: () => void }) {
  const findings = asFindings(detail);
  const changes = new Map(asEvent(detail).changes.map((c) => [c.id, c]));
  const feedback = useFeedback();
  const toast = useToast();
  const [gone, setGone] = useState<Set<number>>(new Set());
  const [showRejected, setShowRejected] = useState(false);

  const active = findings.filter((f) => f.confirmed !== false && !(f.id != null && gone.has(f.id)));
  const rejected = findings.filter((f) => f.confirmed === false);
  const visible = (showRejected ? [...active, ...rejected] : active).filter((f) => !only || f.file_path === only);
  const byFile = useMemo(() => {
    const map = new Map<string, Finding[]>();
    visible.forEach((f) => map.set(f.file_path, [...(map.get(f.file_path) ?? []), f]));
    return [...map.entries()];
  }, [visible]);

  const because = (f: Finding) => f.reason || (f.change_id != null ? changes.get(f.change_id)?.description : "") || "";
  const dismiss = (f: Finding) =>
    f.id != null &&
    feedback.mutate(
      { target_kind: "impact_finding", target_id: f.id, verdict: "not_relevant" },
      { onSuccess: () => { setGone((s) => new Set(s).add(f.id!)); toast("Thanks. Marked as not relevant."); } },
    );

  if (active.length === 0 && rejected.length === 0) return <Empty title="No affected lines">Nothing in this repository uses an API that changes.</Empty>;
  return (
    <>
      {only ? <p className="small">Showing <code>{only}</code>. <button className="linkbtn" onClick={clear}>Show all files</button></p> : null}
      {byFile.map(([file, rows]) => (
        <div className="file" key={file}>
          <div className="file-head">
            <span className="mono">{file}</span>
            <span className="muted">{plural(rows.length, "line")}{rows[0].owners.length ? ` · ${rows[0].owners.join(" ")}` : ""}</span>
          </div>
          {rows.map((f) => (
            <div className="finding" key={`${f.line}-${f.symbol}`}>
              <span className="ln">{f.line}</span>
              <div>
                <div className="snippet">{highlight(f.snippet, f.symbol)}</div>
                {because(f) ? <div className="because">{because(f)}</div> : null}
                {f.confirmed === false ? <div className="because">Checked and marked not affected.</div> : null}
                {f.id != null && f.confirmed !== false ? (
                  <button className="linkbtn" onClick={() => dismiss(f)}>Not relevant</button>
                ) : null}
              </div>
              <div className="srcs" aria-label="Found by">
                {ALL_SOURCES.map((s) => (
                  <span key={s} className={`src ${f.sources.includes(s) ? "on" : ""}`}>
                    {f.sources.includes(s) ? "✓ " : ""}{SOURCE_LABEL[s]}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      ))}
      {rejected.length ? (
        <p className="small muted">
          {plural(rejected.length, "line")} from code search {rejected.length === 1 ? "was" : "were"} checked and marked not affected.{" "}
          <button className="linkbtn" onClick={() => setShowRejected((v) => !v)}>{showRejected ? "Hide them" : "Show them"}</button>
        </p>
      ) : null}
    </>
  );
}

function WhatChanged({ detail }: { detail: UpgradeDetail }) {
  const event = asEvent(detail);
  const findings = asFindings(detail).filter((f) => f.confirmed !== false);
  if (event.changes.length === 0) return <Empty title="No change list">No release notes were available for this range.</Empty>;
  return (
    <>
      {event.changes.map((c, i) => {
        const hits = findings.filter((f) => f.change_id != null && f.change_id === c.id).length;
        return (
          <div key={c.id ?? i} className={`change ${c.breaking ? "breaking" : ""}`}>
            <h3>
              {c.description}
              <span className="small muted">in {c.version}</span>
              {hits ? <span className="small">affects {plural(hits, "line")} here</span> : <span className="small muted">nothing here uses it</span>}
            </h3>
            <blockquote>{c.evidence_quote}</blockquote>
            <a className="small" href={c.evidence_url} target="_blank" rel="noreferrer">Open the release notes</a>
          </div>
        );
      })}
    </>
  );
}

function Production({ detail }: { detail: UpgradeDetail }) {
  const rows = asTelemetry(detail);
  if (rows.length === 0) return <Empty title="Nothing seen in production logs">No log line matched the affected APIs in the last 7 days.</Empty>;
  const total = rows.reduce((n, r) => n + r.count, 0);
  return (
    <>
      <p>{plural(total, "log line")} matched the affected APIs in the last 7 days.</p>
      <div className="tablewrap">
        <table>
          <thead><tr><th>Log message</th><th className="num">Seen</th><th>Last seen</th><th>API</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.signature}>
                <td className="mono">{r.signature}</td>
                <td className="num">{r.count.toLocaleString()}</td>
                <td>{r.last_seen?.replace("T", " ").replace("Z", " UTC") ?? "—"}</td>
                <td className="mono">{r.related_symbol}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function PlanTab({ detail }: { detail: UpgradeDetail }) {
  const plan = asPlan(detail);
  if (!plan) return <Empty title="No plan yet">The plan is written once the impact analysis is done.</Empty>;
  return (
    <>
      <NoModelNotice what="plan" />
      <p><b>Why {plan.severity}:</b> {plan.rationale}</p>
      <p className="small muted">Estimated effort if done by hand: about {plan.effort_hours} h</p>
      <h3>Steps</h3>
      <ol className="steps">{plan.steps.map((s) => <li key={s}>{s}</li>)}</ol>
      <h3>How to check it worked</h3>
      <ol className="steps">{plan.validation.map((s) => <li key={s}>{s}</li>)}</ol>
    </>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <div className="diff" aria-label="Proposed changes">
      {lines.map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")
          ? "hunk" : line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
        return <div key={i} className={cls}>{line || " "}</div>;
      })}
    </div>
  );
}

function FixTests({ detail }: { detail: UpgradeDetail }) {
  const patches = asPatches(detail);
  const tests = asTests(detail);
  const run = detail.run;
  const latest = patches[patches.length - 1];
  const boxes: [string, ProbeSummary | null | undefined][] = [
    [`On ${run.from}`, tests.current],
    [`On ${run.to} without the fix`, tests.unpatched],
    [`On ${run.to} with the fix`, tests.patched],
  ];
  return (
    <>
      {latest ? null : <NoModelNotice what="fix" />}
      {latest ? (
        <>
          <DiffView diff={latest.diff} />
          {latest.rejected.length ? (
            <p className="small muted">{plural(latest.rejected.length, "proposed edit")} {latest.rejected.length === 1 ? "was" : "were"} rejected by the safety rules and not applied.</p>
          ) : null}
        </>
      ) : <Empty title="No fix was attempted">{detail.run.status === "report_only" ? "This run is report only." : "The fix is written after the plan."}</Empty>}
      <div className="tests">
        {boxes.map(([title, probe]) => {
          const t = testsText(probe);
          return (
            <div className="testbox" key={title}>
              <span className="small muted">{title}</span><br />
              <b style={{ color: t.color }}>{t.big}</b>
              <div className="small muted">{t.small}</div>
            </div>
          );
        })}
      </div>
      {latest ? <p className="small muted">Attempt {latest.attempt} of 3 · method {latest.method.replace("_", " ")}</p> : null}
    </>
  );
}

function Activity({ runId, status }: { runId: string; status: string }) {
  const events = useRunEvents(runId, status);
  if (events.length === 0) return <Loading what="activity" />;
  return (
    <ol className="trace" aria-label="Activity">
      {events.filter((e: RunEvent) => e.status !== "started").map((e) => (
        <li key={e.id} className={e.status === "waiting" ? "wait" : e.status === "error" ? "error" : ""}>
          <time dateTime={e.ts}>{shortTime(e.ts)}</time>
          <div><b>{e.node.replace(/_/g, " ")}</b></div>
          {e.summary ? <div className="small muted">{e.summary}</div> : null}
        </li>
      ))}
    </ol>
  );
}

// --- page --------------------------------------------------------------------------------------------------------------

function Tabs({ tab, setTab, id }: { tab: Tab; setTab: (t: Tab) => void; id: string }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, index: number) => {
    const next = e.key === "ArrowRight" ? (index + 1) % TABS.length : e.key === "ArrowLeft" ? (index - 1 + TABS.length) % TABS.length
      : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next]);
    refs.current[next]?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label="Upgrade details">
      {TABS.map((t, i) => (
        <button
          key={t} role="tab" id={`${id}-tab-${i}`} aria-selected={tab === t} aria-controls={`${id}-panel`}
          tabIndex={tab === t ? 0 : -1} ref={(el) => { refs.current[i] = el; }}
          onClick={() => setTab(t)} onKeyDown={(e) => onKey(e, i)}
        >{t}</button>
      ))}
    </div>
  );
}

function Detail({ detail }: { detail: UpgradeDetail }) {
  const [tab, setTab] = useState<Tab>("Affected code");
  const [only, setOnly] = useState<string | null>(null);
  const [decision, setDecision] = useState<"approved" | "rejected" | null>(null);
  const id = useId();
  const run = detail.run;
  const event = asEvent(detail);
  const graph = useGraph(run.id, run.scan_id, tab === "Blast radius");
  const patches = asPatches(detail);
  const prLabel = detail.needs_engineer ? "an issue with the evidence" : `a pull request on ${event.source_repo ?? "the repository"}`;
  const pr = detail.pull_request as { url?: string; state?: string; branch?: string; kind?: string } | null;
  const owner = asFindings(detail).find((f) => f.owners.length)?.owners[0];

  return (
    <>
      <div className="crumbs"><Link to="/upgrades">Upgrades</Link> / {run.package}</div>
      <div className="detail-head">
        <div>
          <div className="journey">
            <span className="pkg" style={{ fontSize: 34 }}>{run.package}</span>
            <Journey from={run.from} to={run.to} kind={event.update_type} />
          </div>
          <div className="facts">
            <div className="fact">Risk<b><RiskBadge risk={run.risk} /></b></div>
            <div className="fact">Effort if done by hand<b>{run.effort_hours ? `about ${run.effort_hours} h` : "—"}</b></div>
            <div className="fact">Status<b><Pill pill={run.pill}>{run.status_label}</Pill></b></div>
            {owner ? <div className="fact">Owner<b>{owner}</b></div> : null}
          </div>
        </div>
        <div className="actions">
          <button className="btn" disabled={!detail.can_decide} onClick={() => setDecision("rejected")}>Reject</button>
          <button className="btn btn-primary" disabled={!detail.can_decide} onClick={() => setDecision("approved")}>
            {detail.decision_pending ? "Decision sent" : detail.needs_engineer ? "Approve and open issue" : "Approve and open PR"}
          </button>
          {detail.decision_pending ? <span className="small" role="status">Waiting for the worker to pick it up…</span> : null}
        </div>
      </div>

      {run.error ? <div className="error-box" role="alert" style={{ marginTop: 16 }}>{run.error}</div> : null}
      {pr ? (
        <p className="small" style={{ marginTop: 14 }}>
          {pr.state === "local" ? (
            <>A local {pr.kind === "issue" ? "issue" : "pull request"} was written{pr.branch ? <> on branch <code>{pr.branch}</code></> : null}: <code>{pr.url}</code></>
          ) : (
            <><a href={pr.url} target="_blank" rel="noreferrer">Open the {pr.kind === "issue" ? "issue" : "pull request"}</a></>
          )}
        </p>
      ) : null}

      <div className="signals" aria-label="Evidence summary">
        <div className="signal"><b>{detail.evidence.pattern_lines}</b><span>lines found by code search</span></div>
        <div className="signal"><b>{detail.evidence.runtime_warnings}</b><span>runtime deprecation warnings</span></div>
        <div className="signal"><b>{detail.evidence.failing_places}</b><span>places fail on {run.to} without the fix</span></div>
        <div className="signal"><b>{detail.evidence.production_lines.toLocaleString()}</b><span>matching production log lines, 7 days</span></div>
      </div>

      <Tabs tab={tab} setTab={setTab} id={id} />
      <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-tab-${TABS.indexOf(tab)}`} tabIndex={0}>
        {tab === "Affected code" ? <AffectedCode detail={detail} only={only} clear={() => setOnly(null)} /> : null}
        {tab === "What changed" ? <WhatChanged detail={detail} /> : null}
        {tab === "Blast radius" ? (
          <Async query={graph} what="blast radius">
            {(g) => <BlastRadius graph={g} onSelect={(path) => { setOnly(path); setTab("Affected code"); }} />}
          </Async>
        ) : null}
        {tab === "Production evidence" ? <Production detail={detail} /> : null}
        {tab === "Plan" ? <PlanTab detail={detail} /> : null}
        {tab === "Fix and tests" ? <FixTests detail={detail} /> : null}
        {tab === "Activity" ? <Activity runId={run.id} status={run.status} /> : null}
      </div>
      {patches.length === 0 && run.status === "report_only" ? (
        <p className="note">Report only: the repository is not allowed to run code, or the severity is below the configured minimum.</p>
      ) : null}
      <DecisionDialog runId={run.id} decision={decision} prLabel={prLabel} onClose={() => setDecision(null)} />
    </>
  );
}

export default function UpgradeDetailPage() {
  const id = useParams().id ?? "";
  const query = useUpgrade(id);
  if (query.isError) return <ErrorBox error={query.error} />;
  return <Async query={query} what="upgrade">{(detail) => <Detail detail={detail} />}</Async>;
}
