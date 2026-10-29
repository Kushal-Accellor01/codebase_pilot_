import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
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

type Theme = "system" | "light" | "dark";

function storedTheme(): Theme {
  try {
    const value = localStorage.getItem("pilotTheme");
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: Theme): void {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  try {
    if (theme === "system") localStorage.removeItem("pilotTheme");
    else localStorage.setItem("pilotTheme", theme);
  } catch {
    /* storage blocked: the choice lasts until the page closes */
  }
}

function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>(storedTheme);
  useEffect(() => { applyTheme(theme); }, [theme]);
  return (
    <div role="group" aria-label="Colour theme" style={{ display: "inline-flex", gap: 4 }}>
      {(["system", "light", "dark"] as const).map((value) => (
        <button
          key={value} type="button" className="btn btn-sm" aria-pressed={theme === value}
          style={theme === value ? { borderWidth: 2, fontWeight: 700 } : undefined} onClick={() => setTheme(value)}
        >
          {value === "system" ? "System" : value === "light" ? "Light" : "Dark"}
        </button>
      ))}
    </div>
  );
}

function TopBar() {
  const scans = useScans().data;
  const scan = scans?.find((s) => s.has_nodes);
  const toast = useToast();
  const client = useQueryClient();
  const start = useMutation({
    mutationFn: (repoUrl: string) => api.startScan(repoUrl),
    onSuccess: () => {
      toast("Scan started. Results update as each step finishes.");
      void client.invalidateQueries();
    },
    onError: (error: Error) => toast(`Could not start the scan: ${error.message}`),
  });
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
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <ThemeSwitch />
        <button className="btn" onClick={() => scan?.repo_url && start.mutate(scan.repo_url)} disabled={!scan?.repo_url || start.isPending}>
          Scan now
        </button>
        <Link className="btn btn-primary" to="/scan">New scan</Link>
      </div>
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
      <a className="skip" href="#page" onClick={(e) => { e.preventDefault(); page.current?.focus(); }}>Skip to the page</a>
      <div className="shell">
        <Rail />
        <main id="main">
          <TopBar />
          <div className="page" id="page" tabIndex={-1} ref={page}>
            <Outlet />
          </div>
        </main>
      </div>
    </Toaster>
  );
}
