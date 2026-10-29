import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { API_MODE, api } from "../api/client";
import { useOverview, useScans } from "../api/hooks";

type ToastFn = (message: string) => void;
const ToastContext = createContext<ToastFn>(() => undefined);
export const useToast = () => useContext(ToastContext);

function Toaster({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = useCallback((text: string) => {
    setMessage(text);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMessage(null), 3500);
  }, []);
  return (
    <ToastContext.Provider value={show}>
      {children}
      <div role="status" aria-live="polite">
        {message ? <div className="toast">{message}</div> : null}
      </div>
    </ToastContext.Provider>
  );
}

function Rail() {
  const waiting = useOverview().data?.approvals_waiting ?? 0;
  return (
    <nav className="rail" aria-label="Main">
      <div className="brand">
        <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 18 L12 4 L21 18" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinejoin="round" />
          <path d="M8 18h8" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
        </svg>
        Codebase Pilot
      </div>
      <div className="nav">
        <NavLink to="/" end>Overview</NavLink>
        <NavLink to="/scan">Scan a repository</NavLink>
        <NavLink to="/dependencies">Dependencies</NavLink>
        <NavLink to="/vulnerabilities">Vulnerabilities</NavLink>
        <NavLink to="/updates">Updates</NavLink>
        <NavLink to="/upgrades">Upgrades</NavLink>
        <NavLink to="/approvals">
          Approvals
          {waiting > 0 ? <span className="count" aria-label={`${waiting} waiting`}>{waiting}</span> : null}
        </NavLink>
        <div className="railgroup">Setup</div>
        <NavLink to="/setup/sources">Source mapping</NavLink>
        <NavLink to="/setup/channels">Channels and schedules</NavLink>
      </div>
      <div className="rail-foot">{API_MODE === "fixtures" ? "Showing recorded data" : "Live"}</div>
    </nav>
  );
}

function TopBar() {
  const scans = useScans().data;
  const scan = scans?.find((s) => s.has_nodes);
  const toast = useToast();
  const client = useQueryClient();
  const [target, setTarget] = useState("");
  const start = useMutation({
    mutationFn: (repoUrl: string) => api.startScan(repoUrl),
    onSuccess: () => {
      setTarget("");
      toast("Scan started. Results update as each step finishes.");
      void client.invalidateQueries();
    },
    onError: (error: Error) => toast(`Could not start the scan: ${error.message}`),
  });
  const repoToScan = target.trim() || scan?.repo_url || "";
  return (
    <div className="topbar">
      <div className="scope">
        <span className="select" aria-label="Repository">{scan?.repo ?? "No repository scanned yet"}</span>
        {scan ? (
          <span>
            Scanned {scan.scanned_at?.slice(0, 16).replace("T", " ")} UTC from commit{" "}
            <code>{scan.commit_sha?.slice(0, 7)}</code>
          </span>
        ) : null}
      </div>
      <form
        className="scanform"
        onSubmit={(event) => {
          event.preventDefault();
          if (repoToScan) start.mutate(repoToScan);
        }}
      >
        <input
          aria-label="Repository to scan"
          placeholder={scan?.repo_url ? "Another repository URL (empty: scan again)" : "https://github.com/owner/repo"}
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        />
        <button className="btn" type="submit" disabled={!repoToScan || start.isPending}>
          {target.trim() ? "Scan this repository" : "Scan now"}
        </button>
      </form>
    </div>
  );
}

export function Shell() {
  const page = useRef<HTMLDivElement>(null);
  const location = useLocation();
  useEffect(() => {
    page.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [location.pathname]);
  return (
    <Toaster>
      <div className="shell">
        <Rail />
        <main>
          <TopBar />
          <div className="page" id="page" tabIndex={-1} ref={page}>
            <Outlet />
          </div>
        </main>
      </div>
    </Toaster>
  );
}
