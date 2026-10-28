import type { components } from "./types";

type S = components["schemas"];

export type Health = S["Health"];
export type Scan = S["ScanOut"];
export type DependencyRow = S["DependencyRow"];
export type BoardItem = S["BoardItem"];
export type WaitingItem = S["WaitingItem"];
export type Overview = S["Overview"];
export type UpgradeSummary = S["UpgradeSummary"];
export type UpgradeDetail = S["UpgradeDetail"];
export type Graph = S["Graph"];
export type GraphNode = S["GraphNode"];
export type DecisionRequest = S["DecisionRequest"];
export type Risk = S["BoardItem"]["risk"];

/** Shapes inside the loosely typed `event`, `findings`, `patches` ... parts of UpgradeDetail. */
export interface ChangeRef {
  id: number | null;
  kind: string;
  breaking: boolean;
  api_symbols: string[];
  description: string;
  migration_hint: string | null;
  version: string;
  evidence_url: string;
  evidence_quote: string;
}

export interface Vulnerability {
  id: string;
  severity: string;
  summary: string | null;
  fixed_in: string | null;
  url: string | null;
}

export interface ImportingFile {
  path: string;
  lines: number[];
}

export interface DependencyEvent {
  id: number;
  event_id: string;
  package: string;
  ecosystem: string;
  dependency_type: string;
  manifest_path: string;
  declared_constraint: string | null;
  current_version: string | null;
  current_version_basis: string;
  target_version: string | null;
  latest_version: string | null;
  update_type: string;
  requires_manifest_change: boolean;
  runtime_requirement_change: boolean;
  is_deprecated: boolean;
  deprecation_message: string | null;
  vulnerabilities: Vulnerability[];
  changes: ChangeRef[];
  summary: string | null;
  notes_coverage: number | null;
  source_repo: string | null;
  importer_count: number;
  importing_files: ImportingFile[];
  priority_score: number;
  risk_level: Risk;
  status: string;
  run_id: string | null;
  run_status: string | null;
  error: string | null;
}

export type Source = "pattern" | "runtime_deprecation" | "test_failure" | "llm";

export interface Finding {
  id: number | null;
  file_path: string;
  line: number;
  snippet: string;
  symbol: string | null;
  change_id: number | null;
  sources: Source[];
  confidence: number;
  confirmed: boolean | null;
  reason: string;
  owners: string[];
}

export interface Telemetry {
  source: string;
  signature: string;
  count: number;
  first_seen: string | null;
  last_seen: string | null;
  sample: string | null;
  related_symbol: string | null;
}

export interface Plan {
  severity: string;
  effort_hours: number;
  rationale: string;
  steps: string[];
  validation: string[];
}

export interface Validation {
  passed: number | boolean;
  tests_passed: number | null;
  tests_total: number | null;
  duration_ms?: number | null;
}

export interface Patch {
  id: number;
  attempt: number;
  method: string;
  diff: string;
  edits: { file: string; find: string; replace: string }[];
  manifest_change: string | null;
  rejected: { file: string; reason: string }[];
  validation: Validation | null;
}

export interface ProbeSummary {
  passed?: boolean | number;
  tests_passed?: number | null;
  tests_total?: number | null;
}

export interface RunEvent {
  id: number;
  run_id: string;
  node: string;
  status: "started" | "finished" | "waiting" | "error";
  summary: string | null;
  ts: string;
  data?: Record<string, unknown> | null;
}

/** Narrowed views of UpgradeDetail's loosely typed parts. */
export const asEvent = (d: UpgradeDetail) => d.event as unknown as DependencyEvent;
export const asFindings = (d: UpgradeDetail) => d.findings as unknown as Finding[];
export const asTelemetry = (d: UpgradeDetail) => d.telemetry as unknown as Telemetry[];
export const asPlan = (d: UpgradeDetail) => d.plan as unknown as Plan | null;
export const asPatches = (d: UpgradeDetail) => d.patches as unknown as Patch[];
export const asTests = (d: UpgradeDetail) =>
  d.tests as unknown as { current?: ProbeSummary | null; unpatched?: ProbeSummary | null; patched?: ProbeSummary | null };
