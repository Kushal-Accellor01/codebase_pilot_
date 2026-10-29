import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import { useChannels, useRepos } from "../api/hooks";
import type { ChannelOut, ChannelsView, ScheduleOut } from "../api/models";
import { useToast } from "../components/Shell";
import { Async, Empty, Pill } from "../components/ui";

const STATUS: Record<string, { label: string; pill: string }> = {
  connected: { label: "Connected", pill: "done" },
  not_connected: { label: "Not connected", pill: "" },
  needs_permission: { label: "Needs permission", pill: "wait" },
  error: { label: "Problem", pill: "fail" },
};
const SOURCE_LABEL: Record<string, { name: string; watches: string }> = {
  rescan: { name: "Repository re-scan", watches: "Inside: anything the webhook missed" },
  dependabot_alerts: { name: "Dependabot alerts", watches: "Outside: security advisories" },
  releases: { name: "New releases", watches: "Outside: registry versions and release notes" },
  internal_sources: { name: "Internal sources", watches: "Inside: Confluence, Slack, schema registry (not read yet)" },
};
const INTERVALS = [[15, "15 minutes"], [30, "30 minutes"], [60, "Hour"], [360, "6 hours"], [1440, "24 hours"]] as const;

function useChannelActions(repoId: string) {
  const toast = useToast();
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: ["channels", repoId] });
  const save = useMutation({
    mutationFn: (v: { kind: string; enabled: boolean; config: Record<string, unknown>; secret_ref?: string | null }) =>
      api.saveChannel(repoId, v.kind, { enabled: v.enabled, config: v.config, secret_ref: v.secret_ref }),
    onSuccess: () => { toast("Saved."); void refresh(); },
    onError: (error: Error) => toast(`Could not save: ${error.message}`),
  });
  const test = useMutation({
    mutationFn: (kind: string) => api.testChannel(repoId, kind),
    onSuccess: (r) => { toast(r.detail); void refresh(); },
    onError: (error: Error) => toast(`Test failed: ${error.message}`),
  });
  const hook = useMutation({
    mutationFn: () => api.installWebhook(repoId),
    onSuccess: (r) => { toast(r.detail); void refresh(); },
    onError: (error: Error) => toast(`Could not install: ${error.message}`),
  });
  const publish = useMutation({
    mutationFn: () => api.publishConfluence(repoId),
    onSuccess: (r) => { toast(r.detail); void refresh(); },
    onError: (error: Error) => toast(error.message),
  });
  return { save, test, hook, publish };
}

function Card({ title, channel, children, footer, text, status: forced }: { title: string; channel?: ChannelOut; children?: ReactNode; footer?: ReactNode; text: string; status?: string }) {
  const status = STATUS[forced ?? channel?.status ?? "not_connected"] ?? STATUS.not_connected;
  return (
    <article className="conn">
      <header><h3>{title}</h3><Pill pill={status.pill}>{status.label}</Pill></header>
      <p>{text}</p>
      {children}
      {channel?.last_error && channel.status !== "connected" ? <p className="err" role="alert">{channel.last_error}</p> : null}
      {footer ? <footer>{footer}</footer> : null}
    </article>
  );
}

function Secret({ channel }: { channel?: ChannelOut }) {
  if (!channel?.secret_ref) return null;
  return (
    <div className="secretbox">
      <code>{channel.secret_ref}</code>
      <span className="muted">{channel.secret_set ? "server secret is set" : "not set on the server"}</span>
    </div>
  );
}

function Field({ id, label, value, onChange, placeholder }: { id: string; label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <div>
      <label className="label small" htmlFor={id}>{label}</label>
      <input id={id} className="field" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

function View({ repoId, view }: { repoId: string; view: ChannelsView }) {
  const by = Object.fromEntries(view.channels.map((c) => [c.kind, c])) as Record<string, ChannelOut | undefined>;
  const { save, test, hook, publish } = useChannelActions(repoId);
  const cfg = (kind: string, key: string) => String(by[kind]?.config?.[key] ?? "");
  const [slack, setSlack] = useState(cfg("slack", "channel"));
  const [space, setSpace] = useState(cfg("confluence", "space_id"));
  const [mail, setMail] = useState(cfg("email", "recipients"));
  const [botMode, setBotMode] = useState(cfg("bot_prs", "mode") || "comment_only");
  const [dash, setDash] = useState(by.dashboard_issue?.enabled ?? false);
  const [readPages, setReadPages] = useState(by.confluence?.enabled ?? false);
  const connected = view.channels.filter((c) => c.status === "connected").length;
  const hookInstalled = by.github_webhook?.status === "connected";
  return (
    <>
      <section className="sec" style={{ marginTop: 20 }} aria-labelledby="h-sources">
        <div className="sechead"><h2 id="h-sources">Change sources</h2><span className="small muted">Tokens stay on the server; this page only names them</span></div>
        <div className="group">Outside the company</div>
        <div className="grid2">
          <Card title="New releases" status="connected" text="Checks every package for new versions and reads their release notes and changelogs.">
            <p className="small muted">npm registry or <code>NPM_REGISTRY_URL</code>, OSV for advisories. Frequency is set under Schedules below.</p>
          </Card>
          <Card
            title="Dependabot alerts" channel={by.dependabot}
            text="Reads open security alerts for this repository and adds them to the advisories of each scan."
            footer={<button className="btn btn-sm" onClick={() => test.mutate("dependabot")} disabled={test.isPending}>Test connection</button>}
          >
            <Secret channel={by.dependabot} />
            <p className="small muted">Token permission: Dependabot alerts, read.</p>
          </Card>
          <Card
            title="Dependabot and Renovate pull requests" channel={by.bot_prs}
            text="When a bot opens an upgrade pull request, Pilot can explain its impact on that pull request."
            footer={<button className="btn btn-sm btn-primary" disabled={save.isPending} onClick={() => save.mutate({ kind: "bot_prs", enabled: botMode !== "off", config: { mode: botMode } })}>Save</button>}
          >
            <label className="label small" htmlFor="botmode">On the bot's pull request</label>
            <select id="botmode" className="field" value={botMode} onChange={(e) => setBotMode(e.target.value)}>
              <option value="off">Do not comment</option>
              <option value="comment_only">Comment with the impact and a link to Pilot's fix</option>
              <option value="comment_and_commit">Comment and push the verified fix, after approval</option>
            </select>
            <p className="small muted">Applies when the server runs repository actions (<code>PILOT_REPO_ACTIONS</code>) or you run <code>cli bot-prs</code>.</p>
          </Card>
        </div>
        <div className="group">Inside the company</div>
        <div className="grid2">
          <Card
            title="GitHub push webhook" channel={by.github_webhook}
            text="Re-scans the repository on every push to its default branch, so merged fixes close their upgrades at once."
            footer={<>
              <button className="btn btn-primary btn-sm" onClick={() => hook.mutate()} disabled={hook.isPending || !view.webhook.public_url_set || !view.webhook.secret_set}>{hookInstalled ? "Check the webhook" : "Install webhook"}</button>
              <button className="btn btn-sm" onClick={() => test.mutate("github_webhook")} disabled={test.isPending || !hookInstalled}>Send a test ping</button>
            </>}
          >
            <div>
              <label className="label small" htmlFor="hookurl">Payload URL</label>
              <input id="hookurl" className="field" readOnly value={view.webhook.url ?? "Set PILOT_PUBLIC_URL to show the address GitHub will call"} />
            </div>
            <p className="small muted">
              Event: push. Signed with <code>GITHUB_WEBHOOK_SECRET</code> ({view.webhook.secret_set ? "set" : "not set"}). Token permission: Webhooks, write.
              {!view.webhook.public_url_set ? " Disabled until PILOT_PUBLIC_URL is set." : ""}
            </p>
          </Card>
          <Card
            title="Confluence" channel={by.confluence}
            text="Publishes Pilot's report for this repository as one Confluence page, after you press Publish."
            footer={<>
              <button className="btn btn-sm btn-primary" disabled={save.isPending} onClick={() => save.mutate({ kind: "confluence", enabled: readPages, config: { ...(by.confluence?.config ?? {}), space_id: space } })}>Save</button>
              <button className="btn btn-sm" onClick={() => test.mutate("confluence")} disabled={test.isPending}>Test connection</button>
              <button className="btn btn-sm" onClick={() => publish.mutate()} disabled={publish.isPending || !space}>Publish report</button>
            </>}
          >
            <Field id="space" label="Space id" value={space} onChange={setSpace} placeholder="numeric space id" />
            <Secret channel={by.confluence} />
            <label className="check"><input type="checkbox" checked={readPages} onChange={(e) => setReadPages(e.target.checked)} /> Keep this connection switched on</label>
            <p className="small muted">Reading change announcements from Confluence pages arrives in a later release.</p>
          </Card>
          <Card
            title="Slack" channel={by.slack}
            text="Posts when an upgrade needs a decision and when a pull request is opened. Messages carry names and counts, never code."
            footer={<>
              <button className="btn btn-sm btn-primary" disabled={save.isPending} onClick={() => save.mutate({ kind: "slack", enabled: !!slack, config: { channel: slack } })}>Save</button>
              <button className="btn btn-sm" onClick={() => test.mutate("slack")} disabled={test.isPending}>Test connection</button>
            </>}
          >
            <Field id="slack" label="Channel" value={slack} onChange={setSlack} placeholder="#payments-changes" />
            <Secret channel={by.slack} />
          </Card>
          <Card title="Specs, topics and relationships" channel={by.schema_registry} text="OpenAPI, AsyncAPI and protobuf files, Kafka schemas and declared service links.">
            <Empty title="Not read in this version">These sources are built in a later release. Today Pilot reports package changes only.</Empty>
          </Card>
        </div>
      </section>

      <section className="sec" aria-labelledby="h-notify">
        <div className="sechead"><h2 id="h-notify">Where Pilot reports</h2><span className="small muted">Only after a person decides, except notifications</span></div>
        <div className="grid2">
          <Card
            title="Email" channel={by.email} text="Sends the same short notifications as Slack."
            footer={<>
              <button className="btn btn-sm btn-primary" disabled={save.isPending} onClick={() => save.mutate({ kind: "email", enabled: !!mail.trim(), config: { recipients: mail } })}>Save</button>
              <button className="btn btn-sm" onClick={() => test.mutate("email")} disabled={test.isPending}>Test connection</button>
            </>}
          >
            <Field id="mail" label="Recipients" value={mail} onChange={setMail} placeholder="team@acme.com, lead@acme.com" />
            <Secret channel={by.email} />
          </Card>
          <Card
            title="GitHub dependency dashboard" channel={by.dashboard_issue}
            text="One issue in the repository listing every upgrade, its risk and its pull request, updated in place."
            footer={<button className="btn btn-sm btn-primary" disabled={save.isPending} onClick={() => save.mutate({ kind: "dashboard_issue", enabled: dash, config: {} })}>Save</button>}
          >
            <label className="check"><input type="checkbox" checked={dash} onChange={(e) => setDash(e.target.checked)} /> Publish after every scan</label>
            <p className="small muted">Posts only when the server runs repository actions (<code>PILOT_REPO_ACTIONS</code>) or you run <code>cli issues</code>.</p>
          </Card>
        </div>
      </section>

      <Schedules repoId={repoId} schedules={view.schedules} />
      <div className="note" role="note">
        <span aria-hidden="true">🔒</span>
        <span>Tokens are never sent to the browser or stored in Pilot's database. Each connection names a secret in the server's environment, and Test connection checks it from the server. {connected} of {view.channels.length} channels connected.</span>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginTop: 30, paddingTop: 22, borderTop: "1px solid var(--rule)" }}>
        <Link className="btn" to="/setup/sources">Back</Link>
        <Link className="btn btn-primary" to="/updates">Open the update feed</Link>
      </div>
    </>
  );
}

function Schedules({ repoId, schedules }: { repoId: string; schedules: ScheduleOut[] }) {
  const toast = useToast();
  const client = useQueryClient();
  const update = useMutation({
    mutationFn: (v: { id: string; interval_minutes?: number; enabled?: boolean }) => api.updateSchedule(v.id, { interval_minutes: v.interval_minutes, enabled: v.enabled }),
    onSuccess: () => { toast("Schedule saved."); void client.invalidateQueries({ queryKey: ["channels", repoId] }); },
    onError: (error: Error) => toast(`Could not save: ${error.message}`),
  });
  return (
    <section className="sec" aria-labelledby="h-sched">
      <div className="sechead"><h2 id="h-sched">Schedules</h2><span className="small muted">Each trigger queues the same job; repeats within a minute are merged</span></div>
      <div className="tablewrap">
        <table className="wide">
          <thead><tr><th>Trigger</th><th>Watches</th><th>How often</th><th>On</th><th>Next run</th></tr></thead>
          <tbody>
            {schedules.map((s) => {
              const label = SOURCE_LABEL[s.source] ?? { name: s.source, watches: "" };
              const ready = s.source !== "internal_sources";
              return (
                <tr key={s.id}>
                  <td><b>{label.name}</b></td>
                  <td>{label.watches}</td>
                  <td>
                    <select className="field" style={{ width: "auto", minHeight: 36 }} aria-label={`How often: ${label.name}`} value={INTERVALS.some(([m]) => m === s.interval_minutes) ? s.interval_minutes : ""} onChange={(e) => update.mutate({ id: s.id, interval_minutes: Number(e.target.value) })}>
                      {!INTERVALS.some(([m]) => m === s.interval_minutes) ? <option value="">{s.interval_minutes} minutes</option> : null}
                      {INTERVALS.map(([m, text]) => <option key={m} value={m}>{text}</option>)}
                    </select>
                  </td>
                  <td>
                    <label className="check">
                      <input type="checkbox" checked={s.enabled} disabled={!ready} onChange={(e) => update.mutate({ id: s.id, enabled: e.target.checked })} />
                      {ready ? (s.enabled ? "On" : "Off") : "Waiting for a source"}
                    </label>
                  </td>
                  <td className="small muted">{s.next_run_at ? s.next_run_at.slice(0, 16).replace("T", " ") + " UTC" : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="small muted" style={{ margin: "10px 0 0" }}>Schedules run inside the API process when <code>PILOT_EMBEDDED_WORKER=1</code>.</p>
    </section>
  );
}

export default function ChannelsPage() {
  const repos = useRepos();
  const [picked, setPicked] = useState<string | null>(null);
  const repoId = picked ?? repos.data?.[0]?.repo_id;
  const query = useChannels(repoId);
  return (
    <>
      <h1>Tell Pilot where changes come from</h1>
      <p className="lede">Every source feeds the same pipeline: a detected change goes through impact analysis, then a plan, a tested fix and a pull request that a person approves.</p>
      <Async query={repos} what="repositories">
        {(list) => list.length === 0 ? (
          <Empty title="No repository yet">Scan a repository first; channels are set up per repository.</Empty>
        ) : (
          <>
            {list.length > 1 ? (
              <div style={{ maxWidth: 360 }}>
                <label className="label small" htmlFor="repo-pick">Repository</label>
                <select id="repo-pick" className="field" value={repoId} onChange={(e) => setPicked(e.target.value)}>
                  {list.map((r) => <option key={r.repo_id} value={r.repo_id}>{r.repo}</option>)}
                </select>
              </div>
            ) : <p className="small muted">Repository: <b>{list[0].repo}</b></p>}
            <Async query={query} what="channels">{(view) => <View key={repoId} repoId={repoId!} view={view} />}</Async>
          </>
        )}
      </Async>
    </>
  );
}
