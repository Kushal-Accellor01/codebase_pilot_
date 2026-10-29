import dependencies from "./fixtures/dependencies.json";
import dependency from "./fixtures/dependency.json";
import events from "./fixtures/events.json";
import graph from "./fixtures/graph.json";
import overview from "./fixtures/overview.json";
import scans from "./fixtures/scans.json";
import upgrade from "./fixtures/upgrade.json";
import upgrades from "./fixtures/upgrades.json";
import channels from "./fixtures/channels.json";
import changes from "./fixtures/changes.json";
import repos from "./fixtures/repos.json";
import scanRequest from "./fixtures/scan_request.json";
import sources from "./fixtures/sources.json";
import summary from "./fixtures/summary.json";
import vulnerabilities from "./fixtures/vulnerabilities.json";
import type {
  ChangeFeed, ChannelsView, DependencyEvent, DependencyRow, Graph, Overview, RepoOut, RunEvent, Scan, ScanRequestOut,
  ScanSummary, SourceList, UpgradeDetail, UpgradeSummary, VulnList,
} from "./models";

/** Real API responses captured from the offline golden path (`npm run gen:assets`). */
export const fixtures = {
  scans: scans as unknown as Scan[],
  overview: overview as unknown as Overview,
  dependencies: dependencies as unknown as DependencyRow[],
  dependency: dependency as unknown as DependencyEvent,
  upgrades: upgrades as unknown as UpgradeSummary[],
  upgrade: upgrade as unknown as UpgradeDetail,
  graph: graph as unknown as Graph,
  events: events as unknown as RunEvent[],
  scanRequest: scanRequest as unknown as ScanRequestOut,
  repos: repos as unknown as RepoOut[],
  summary: summary as unknown as ScanSummary,
  vulnerabilities: vulnerabilities as unknown as VulnList,
  sources: sources as unknown as SourceList,
  channels: channels as unknown as ChannelsView,
  changes: changes as unknown as ChangeFeed,
};
