import type { ReactNode } from "react";

import type { Risk } from "../api/models";

export const RISK_LABEL: Record<string, string> = {
  critical: "Critical", high: "High", medium: "Medium", low: "Low", none: "Up to date",
};

export const SOURCE_LABEL: Record<string, string> = {
  pattern: "Code search", runtime_deprecation: "Runtime warning", test_failure: "Failing test", llm: "Model review",
};

export function RiskBadge({ risk }: { risk: Risk | string }) {
  return <span className={`risk ${risk}`}>{RISK_LABEL[risk] ?? risk}</span>;
}

export function Pill({ pill, children }: { pill: string; children: ReactNode }) {
  return <span className={`pill ${pill}`}>{children}</span>;
}

export function Arrow() {
  return (
    <svg width="22" height="14" viewBox="0 0 22 14" aria-hidden="true">
      <path d="M1 7h18M14 2l5 5-5 5" fill="none" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}

export function Journey({ from, to, kind }: { from?: string | null; to?: string | null; kind: string }) {
  if (!to || kind === "none" || from === to) {
    return (
      <span className="journey">
        <span>{from ?? "-"}</span>
        <span className="kind">current</span>
      </span>
    );
  }
  return (
    <span className="journey" aria-label={`from ${from} to ${to}, ${kind} update`}>
      <span className="from">{from}</span>
      <Arrow />
      <span>{to}</span>
      <span className="kind">{kind}</span>
    </span>
  );
}

export function Loading({ what = "page" }: { what?: string }) {
  return (
    <div role="status" aria-label={`Loading ${what}`}>
      <div className="skeleton" style={{ width: "60%", height: 34 }} />
      <div className="skeleton" style={{ width: "80%" }} />
      <div className="skeleton" style={{ height: 120 }} />
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="error-box" role="alert">
      <b>Could not load this.</b> {message}
      {onRetry ? <> <button type="button" className="btn btn-sm" onClick={onRetry}>Try again</button></> : null}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <b>{title}</b>
      {children ? <p className="small" style={{ margin: "6px 0 0" }}>{children}</p> : null}
    </div>
  );
}

/** Loading, error and data in one place so no page forgets a state. */
export function Async<T>({
  query, what, children,
}: {
  query: { isLoading: boolean; isError: boolean; error: unknown; data: T | undefined; refetch?: () => unknown };
  what?: string;
  children: (data: T) => ReactNode;
}) {
  if (query.isLoading) return <Loading what={what} />;
  if (query.isError) return <ErrorBox error={query.error} onRetry={query.refetch ? () => { void query.refetch?.(); } : undefined} />;
  if (query.data === undefined) return <Empty title="Nothing here yet" />;
  return <>{children(query.data)}</>;
}

export function highlight(snippet: string, symbol: string | null) {
  const name = symbol?.split("(")[0].trim();
  if (!name || name === "path-syntax") return <>{snippet}</>;
  const at = snippet.indexOf(name);
  if (at < 0) return <>{snippet}</>;
  return (
    <>
      {snippet.slice(0, at)}
      <mark>{name}</mark>
      {snippet.slice(at + name.length)}
    </>
  );
}

export function plural(n: number, noun: string): string {
  return n === 1 ? `${n} ${noun}` : `${n} ${noun}s`;
}

export function shortTime(ts: string): string {
  const match = /T(\d\d:\d\d:\d\d)/.exec(ts);
  return match ? match[1] : ts;
}
