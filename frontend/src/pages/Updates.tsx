import { useState } from "react";
import { Link } from "react-router-dom";

import { useChanges } from "../api/hooks";
import type { ChangeCard } from "../api/models";
import { Async, Empty } from "../components/ui";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "needs_decision", label: "Needs a decision" },
  { key: "outside", label: "Outside" },
  { key: "inside", label: "Inside" },
  { key: "security", label: "Security" },
] as const;

export function Card({ card }: { card: ChangeCard }) {
  const to = card.run_id ? `/upgrades/${card.run_id}` : null;
  const stop = card.tracker.includes("stop");
  return (
    <li className="item" data-testid="change-card">
      <span className="bar" style={{ background: card.severity === "none" ? undefined : `var(--${card.severity})` }} aria-hidden="true" />
      <div className="body">
        <div>
          <div className="meta">
            <span className="srcbadge">{card.source_label}</span>
            <span>{card.origin === "external" ? "outside" : "inside"}</span>
            {card.severity !== "none" ? <><span>·</span><span>{card.severity}</span></> : null}
            <span>·</span><span>{card.detected_at.slice(0, 10)}</span>
          </div>
          <h3>{card.title}</h3>
          {card.quote ? (
            <blockquote>
              {card.quote.text}
              <cite>
                {card.quote.source}
                {card.quote.url ? <> · <a href={card.quote.url} target="_blank" rel="noreferrer">source</a></> : null}
              </cite>
            </blockquote>
          ) : null}
          {card.impact ? <p className="impact">{card.impact}</p> : null}
        </div>
        <div className="right">
          <span className="sidelabel">Step {card.step} of {card.step_total} · {card.step_label}</span>
          <div className="track" role="img" aria-label={`Step ${card.step} of ${card.step_total}: ${card.step_label}`}>
            {card.tracker.map((state, i) => <span key={i} className={state === "todo" ? undefined : state} />)}
          </div>
          {to ? (
            <Link className={card.action.kind === "approve" ? "btn btn-primary" : "btn"} to={to}>{card.action.label}</Link>
          ) : <span className="small muted">{stop ? "Needs a person" : card.action.label}</span>}
        </div>
      </div>
    </li>
  );
}

export default function UpdatesPage() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("all");
  const query = useChanges(filter);
  return (
    <>
      <h1>What changed, and what it means for your code</h1>
      <p className="lede">
        Changes from outside (new releases, advisories, Dependabot) and from your own pushes arrive in one shape and go through the same
        impact check. Every quote is checked against its source.
      </p>
      <Async query={query} what="the update feed">
        {(data) => (
          <>
            <div className="filters" role="group" aria-label="Filter changes">
              {FILTERS.map((f) => (
                <button key={f.key} className="chip" aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>
                  {f.label} {f.key === "all" ? data.counts.all : data.counts[f.key]}
                </button>
              ))}
            </div>
            {data.cards.length === 0 ? (
              <Empty title={filter === "all" ? "No changes yet" : "Nothing matches this filter"}>
                {filter === "all" ? "Scan a repository and Pilot lists every upgrade and advisory that needs attention here." : "Choose All to see every change."}
              </Empty>
            ) : (
              <ol className="feed" aria-label="Changes">{data.cards.map((c) => <Card key={c.event_id} card={c} />)}</ol>
            )}
          </>
        )}
      </Async>
    </>
  );
}
