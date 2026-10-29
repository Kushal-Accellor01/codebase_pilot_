import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { API_MODE, api } from "./client";
import type { DecisionRequest, RunEvent } from "./models";

const live = API_MODE === "live";
export const TERMINAL = ["pr_opened", "rejected", "report_only", "needs_engineer", "issue_opened", "failed", "resolved"];

export const useOverview = () =>
  useQuery({ queryKey: ["overview"], queryFn: () => api.overview(), refetchInterval: live ? 5000 : false });

export const useDependencies = () =>
  useQuery({ queryKey: ["dependencies"], queryFn: () => api.dependencies(), refetchInterval: live ? 10000 : false });

export const useDependency = (id: number) => useQuery({ queryKey: ["dependency", id], queryFn: () => api.dependency(id) });

export const useUpgrades = (status?: string) =>
  useQuery({
    queryKey: ["upgrades", status ?? "all"],
    queryFn: () => api.upgrades(status),
    refetchInterval: live ? 5000 : false,
  });

/** Polls while the run is working or a decision is being processed; stops at a pause or a final state. */
export const useUpgrade = (id: string) =>
  useQuery({
    queryKey: ["upgrade", id],
    queryFn: () => api.upgrade(id),
    refetchInterval: (query) => {
      if (!live) return false;
      const data = query.state.data;
      const status = data?.run.status;
      if (data?.decision_pending) return 2000; // the worker has not picked the decision up yet
      return status && (TERMINAL.includes(status) || status === "awaiting_approval") ? false : 2000;
    },
  });

/** The graph belongs to the scan the run started on, not the latest scan. */
export const useGraph = (runId: string, scanId: string, enabled: boolean) =>
  useQuery({ queryKey: ["graph", runId, scanId], queryFn: () => api.graph(runId, scanId), enabled });

export const useScans = () => useQuery({ queryKey: ["scans"], queryFn: () => api.scans() });

export function useDecision(runId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DecisionRequest) => api.decide(runId, body),
    onSuccess: () => {
      // The worker picks the decision up shortly; refetch now and let the polling in useUpgrade follow.
      void client.invalidateQueries({ queryKey: ["upgrade", runId] });
      void client.invalidateQueries({ queryKey: ["overview"] });
      void client.invalidateQueries({ queryKey: ["upgrades"] });
    },
  });
}

export function useFeedback() {
  return useMutation({ mutationFn: api.feedback });
}

/** The run trace: server-sent events in live mode (replayed from the table), the recorded trace otherwise. */
export function useRunEvents(runId: string, status: string | undefined): RunEvent[] {
  const [events, setEvents] = useState<RunEvent[]>(() => (live ? [] : api.fixtureEvents()));
  const client = useQueryClient();

  useEffect(() => {
    if (!live) return;
    setEvents([]);
    const source = new EventSource(`/api/upgrades/${runId}/events`);
    source.addEventListener("run_event", (message) => {
      const event = JSON.parse((message as MessageEvent).data) as Omit<RunEvent, "id">;
      const id = Number((message as MessageEvent).lastEventId);
      setEvents((previous) => (previous.some((e) => e.id === id) ? previous : [...previous, { ...event, id }]));
    });
    source.addEventListener("end", () => {
      source.close();
      void client.invalidateQueries({ queryKey: ["upgrade", runId] });
    });
    source.onerror = () => source.close();
    return () => source.close();
    // Reconnect when the run moves on (a decision restarts the trace after the pause).
  }, [runId, status, client]);

  return events;
}

export const SCAN_DONE = ["complete", "failed", "cancelled"];

/**
 * The scan request and its stages. Live: a server-sent event stream (replayed after a reconnect) tells the page to
 * refetch the moment a stage changes; if the stream cannot open or drops, it polls every 1.5 s instead.
 */
export function useScanRequest(id: string) {
  const client = useQueryClient();
  const [streaming, setStreaming] = useState(false);
  const query = useQuery({
    queryKey: ["scan-request", id],
    queryFn: () => api.scanRequest(id),
    refetchInterval: (q) => (!live || streaming || SCAN_DONE.includes(q.state.data?.status ?? "") ? false : 1500),
  });
  const status = query.data?.status;
  const finished = status !== undefined && SCAN_DONE.includes(status);
  useEffect(() => {
    if (!live || !id || finished || typeof EventSource === "undefined") return;
    const source = new EventSource(`/api/scan-requests/${id}/events`);
    const refresh = () => { void client.invalidateQueries({ queryKey: ["scan-request", id] }); };
    source.onopen = () => setStreaming(true);
    source.addEventListener("scan_event", refresh);
    source.addEventListener("end", () => { source.close(); setStreaming(false); refresh(); });
    source.onerror = () => { source.close(); setStreaming(false); };
    return () => { source.close(); setStreaming(false); };
  }, [id, finished, client]);
  return query;
}

export function useRetryStage(requestId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (stage: string) => api.retryStage(requestId, stage),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["scan-request", requestId] }); },
  });
}

export const useScanSummary = (scan: string) =>
  useQuery({ queryKey: ["summary", scan], queryFn: () => api.summary(scan), refetchInterval: live ? 8000 : false });

export const useVulns = (scan: string) =>
  useQuery({ queryKey: ["vulns", scan], queryFn: () => api.vulnerabilities(scan), refetchInterval: live ? 15000 : false });

export const useSources = (scan = "latest") => useQuery({ queryKey: ["sources", scan], queryFn: () => api.sources(scan) });

export const useRepos = () => useQuery({ queryKey: ["repos"], queryFn: () => api.repos() });

export const useChannels = (repoId: string | undefined) =>
  useQuery({ queryKey: ["channels", repoId], queryFn: () => api.channels(repoId!), enabled: !!repoId });

export const useChanges = (filter: string) =>
  useQuery({ queryKey: ["changes", filter], queryFn: () => api.changes(filter), refetchInterval: live ? 10000 : false });

export const useHealth = () => useQuery({ queryKey: ["health"], queryFn: () => api.health(), refetchInterval: live ? 30000 : false });
