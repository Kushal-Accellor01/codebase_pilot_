import dependencies from "./fixtures/dependencies.json";
import dependency from "./fixtures/dependency.json";
import events from "./fixtures/events.json";
import graph from "./fixtures/graph.json";
import overview from "./fixtures/overview.json";
import scans from "./fixtures/scans.json";
import upgrade from "./fixtures/upgrade.json";
import upgrades from "./fixtures/upgrades.json";
import type {
  DependencyEvent, DependencyRow, Graph, Overview, RunEvent, Scan, UpgradeDetail, UpgradeSummary,
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
};
