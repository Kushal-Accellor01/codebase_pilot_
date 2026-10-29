import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";

import { api } from "../api/client";
import { useHealth, useScans } from "../api/hooks";
import type { RepoCheck, ScanScope } from "../api/models";
import { Pill } from "../components/ui";

const TILES: { key: keyof ScanScope; title: string; text: string }[] = [
  { key: "vulnerabilities", title: "Vulnerabilities", text: "Every package in the lockfile, direct and transitive, against OSV and Dependabot alerts" },
  { key: "breaking_changes", title: "Breaking changes and deprecations", text: "Release notes read for each upgrade, every claim quoted from the source" },
  { key: "internal_contracts", title: "Internal contracts", text: "OpenAPI, AsyncAPI, protobuf and topics your code uses. Not read in this version" },
  { key: "production_evidence", title: "Production evidence", text: "Runtime warnings in Azure Monitor or log files, matched to code lines" },
  { key: "fixes", title: "Tested fixes", text: "Run your tests on the new version and propose a fix, for allowed repositories" },
  { key: "sbom", title: "SBOM", text: "A CycloneDX bill of materials for the scanned commit, ready to download" },
];
const DEFAULT_SCOPE: ScanScope = {
  vulnerabilities: true, breaking_changes: true, internal_contracts: true, production_evidence: false, fixes: true, sbom: true,
};

export function isRepoLink(value: string): boolean {
  const v = value.trim();
  return /^(https?:\/\/|git@|ssh:\/\/)[^\s]+$/.test(v) || /^([A-Za-z]:[\\/]|\/|\.\.?[\\/])[^\s]*$/.test(v);
}

export default function ScanPage() {
  const navigate = useNavigate();
  const health = useHealth();
  const scans = useScans().data ?? [];
  const [repo, setRepo] = useState(scans.find((s) => s.repo_url)?.repo_url ?? "");
  const [branch, setBranch] = useState("");
  const [only, setOnly] = useState("");
  const [scope, setScope] = useState<ScanScope>(DEFAULT_SCOPE);
  const [touched, setTouched] = useState(false);
  const [check, setCheck] = useState<RepoCheck | null>(null);

  const valid = isRepoLink(repo);
  const checkAccess = useMutation({ mutationFn: () => api.checkRepo(repo.trim()), onSuccess: setCheck });
  const start = useMutation({
    mutationFn: () =>
      api.startScan(repo.trim(), {
        branch: branch.trim() || undefined, scope,
        only: only.split(",").map((s) => s.trim()).filter(Boolean),
      }),
    onSuccess: (accepted) => navigate(accepted.scan_request_id ? `/scan/${accepted.scan_request_id}` : "/scans/latest"),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (valid) start.mutate();
  }

  const ready = health.data?.status === "ok";
  return (
    <>
      <h1>Scan a repository</h1>
      <p className="lede">
        Paste a repository link. Pilot maps every dependency, checks each one for vulnerabilities and breaking changes,
        and shows which lines of your code a change reaches.
      </p>
      <div className="split">
        <form className="card" style={{ padding: 28, display: "flex", flexDirection: "column", gap: 24 }} onSubmit={submit} noValidate>
          <div>
            <label className="label" htmlFor="repo">Repository link</label>
            <input
              id="repo" className="field big" type="text" value={repo} aria-invalid={touched && !valid}
              aria-describedby="repo-help" placeholder="https://github.com/owner/repo"
              onChange={(e) => { setRepo(e.target.value); setCheck(null); }}
            />
            <div id="repo-help" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginTop: 10 }}>
              {touched && !valid ? (
                <span className="pill fail" role="alert">Enter a repository link or a local path</span>
              ) : check ? (
                <Pill pill={check.ok ? "done" : "fail"}>
                  {check.ok ? `${check.provider} · reachable` : check.allowed ? `${check.provider} · ${check.detail}` : "Not in EXEC_ALLOWED_REPOS"}
                </Pill>
              ) : null}
              <span className="small muted">Pilot installs and tests code only for repositories in <code>EXEC_ALLOWED_REPOS</code>.</span>
            </div>
            {check && !check.ok ? <p className="small" style={{ color: "var(--critical)", margin: "8px 0 0" }}>{check.detail}</p> : null}
          </div>
          <div className="row2">
            <div>
              <label className="label" htmlFor="branch">Branch <span className="muted" style={{ fontWeight: 500 }}>(optional)</span></label>
              <input id="branch" className="field" value={branch} placeholder="default branch" onChange={(e) => setBranch(e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="only">Only these packages <span className="muted" style={{ fontWeight: 500 }}>(optional)</span></label>
              <input id="only" className="field" value={only} placeholder="express, @acme/money" onChange={(e) => setOnly(e.target.value)} />
            </div>
          </div>
          <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
            <legend className="label" style={{ marginBottom: 10 }}>What should Pilot look for?</legend>
            <div className="tiles">
              {TILES.map((t) => (
                <label key={t.key} className="tile">
                  <input type="checkbox" checked={scope[t.key]} onChange={(e) => setScope({ ...scope, [t.key]: e.target.checked })} />
                  <span><b>{t.title}</b><small>{t.text}</small></span>
                </label>
              ))}
            </div>
          </fieldset>
          {start.isError ? <div className="error-box" role="alert">{(start.error as Error).message}</div> : null}
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center", borderTop: "1px solid var(--rule)", paddingTop: 22 }}>
            <button className="btn btn-primary" type="submit" disabled={start.isPending}>
              {start.isPending ? "Starting…" : "Start scan"}
            </button>
            <button className="btn" type="button" disabled={!valid || checkAccess.isPending} onClick={() => checkAccess.mutate()}>
              {checkAccess.isPending ? "Checking…" : "Check access first"}
            </button>
            <span className="small muted" style={{ flex: "1 1 240px" }}>Nothing leaves your systems until someone approves it.</span>
          </div>
        </form>
        <aside style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <section className="card" style={{ padding: "22px 22px 8px" }}>
            <h2 style={{ marginBottom: 4 }}>How Pilot reads your code</h2>
            <p className="small muted" style={{ margin: "0 0 6px" }}>Three layers, each checked by code before the next one uses it.</p>
            <div className="layer"><span className="n">1</span><div><h3>Structure</h3><p className="small muted" style={{ margin: 0 }}>Files, imports and manifests, parsed into a graph</p></div></div>
            <div className="layer"><span className="n">2</span><div><h3>Symbols and references</h3><p className="small muted" style={{ margin: 0 }}>Who calls what, from a SCIP index. Not enabled in this version</p></div></div>
            <div className="layer"><span className="n">3</span><div><h3>Meaning</h3><p className="small muted" style={{ margin: 0 }}>The model reads release notes; code verifies every quote, file and line</p></div></div>
          </section>
          <section className="card" style={{ padding: 22 }}>
            <h2 style={{ marginBottom: 10 }}>Recent scans</h2>
            {scans.length === 0 ? <p className="small muted" style={{ margin: 0 }}>No scans yet. The first one starts here.</p> : null}
            {scans.slice(0, 4).map((s) => (
              <Link key={s.id} to={`/scans/${s.id}`} style={{ display: "flex", justifyContent: "space-between", gap: 12, textDecoration: "none", color: "inherit", padding: "10px 0", borderTop: "1px solid var(--rule)" }}>
                <span><b style={{ display: "block" }}>{s.repo}</b><span className="small muted">{s.scanned_at?.slice(0, 16).replace("T", " ")} UTC</span></span>
                {s.critical > 0 ? <span className="pill fail">{s.critical} critical</span> : <span className="pill">{s.updates} to review</span>}
              </Link>
            ))}
          </section>
          <p className="small muted" style={{ margin: 0 }}>
            {ready ? <Pill pill="done">Server ready</Pill> : <Pill pill="wait">Checking the server…</Pill>} model mode: {health.data?.llm_mode ?? "…"}
          </p>
        </aside>
      </div>
    </>
  );
}
