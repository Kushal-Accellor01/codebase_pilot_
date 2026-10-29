import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { api } from "../api/client";
import { fixtures } from "../api/fixtures";
import { isRepoLink, linkProblem } from "../pages/Scan";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("Scan a repository", () => {
  it("accepts links and paths, and rejects anything else", () => {
    for (const ok of ["https://github.com/acme/shop", "git@github.com:acme/shop.git", "C:\\work\\shop", "/srv/repos/shop"]) {
      expect(isRepoLink(ok)).toBe(true);
    }
    for (const bad of ["", "shop", "acme shop", "javascript:alert(1)"]) expect(isRepoLink(bad)).toBe(false);
  });

  it("gives the exact reason a link cannot be scanned", () => {
    expect(linkProblem("")).toMatch(/Enter a repository link/);
    expect(linkProblem("https://github.com/acme")).toMatch(/Add the repository name/);
    expect(linkProblem("https://github.com/acme/shop")).toBeNull();
  });

  it("starts empty, and Enter on the empty field shows the error and starts nothing", async () => {
    const start = vi.spyOn(api, "startScan");
    const user = userEvent.setup();
    renderAt("/scan");
    const field = await screen.findByLabelText("Repository link");
    expect(field).toHaveValue("");
    await user.type(field, "{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a repository link");
    expect(start).not.toHaveBeenCalled();
  });

  it("offers recent repositories as chips that fill the field", async () => {
    const user = userEvent.setup();
    renderAt("/scan");
    const group = await screen.findByRole("group", { name: "Recent repositories" });
    await user.click(within(group).getAllByRole("button")[0]);
    expect(screen.getByLabelText("Repository link")).not.toHaveValue("");
  });

  it("says a scan of an unlisted public repository is read-only", async () => {
    vi.spyOn(api, "checkRepo").mockResolvedValue({
      provider: "github", repo: "shop", allowed: true, read_only: true, ok: true, detail: "3 branches", branches: ["main"],
    });
    const user = userEvent.setup();
    renderAt("/scan");
    await user.type(await screen.findByLabelText("Repository link"), "https://github.com/acme/shop");
    await user.click(screen.getByRole("button", { name: "Check access first" }));
    expect(await screen.findByRole("note")).toHaveTextContent("Read-only scan: Pilot will not install or test code");
  });

  it("explains an invalid link and does not start a scan", async () => {
    const start = vi.spyOn(api, "startScan");
    const user = userEvent.setup();
    renderAt("/scan");
    const field = await screen.findByLabelText("Repository link");
    await user.clear(field);
    await user.type(field, "not a link");
    await user.click(screen.getByRole("button", { name: "Start scan" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a repository link or a local path");
    expect(start).not.toHaveBeenCalled();
  });

  it("starts a scan with the chosen scope and opens the progress page", async () => {
    const start = vi.spyOn(api, "startScan").mockResolvedValue({ job_id: "j", scan_request_id: fixtures.scanRequest.id, read_only: false });
    const user = userEvent.setup();
    renderAt("/scan");
    const field = await screen.findByLabelText("Repository link");
    await user.clear(field);
    await user.type(field, "https://github.com/acme/shop");
    await user.type(screen.getByLabelText(/Branch/), "release");
    await user.click(screen.getByRole("checkbox", { name: /SBOM/ }));
    await user.click(screen.getByRole("button", { name: "Start scan" }));
    expect(start).toHaveBeenCalledWith("https://github.com/acme/shop", {
      branch: "release", only: [],
      scope: {
        vulnerabilities: true, breaking_changes: true, internal_contracts: false, production_evidence: false, fixes: true, sbom: false,
      },
    });
    expect(await screen.findByRole("list", { name: "Scan steps" })).toBeInTheDocument();
  });

  it("checks access before scanning and shows why a repository is refused", async () => {
    vi.spyOn(api, "checkRepo").mockResolvedValue({
      provider: "github", repo: "shop", allowed: false, read_only: false, ok: false, detail: "This repository is not in EXEC_ALLOWED_REPOS.", branches: [],
    });
    const user = userEvent.setup();
    renderAt("/scan");
    const field = await screen.findByLabelText("Repository link");
    await user.clear(field);
    await user.type(field, "https://github.com/evil/repo");
    await user.click(screen.getByRole("button", { name: "Check access first" }));
    expect(await screen.findByText("Not in EXEC_ALLOWED_REPOS")).toBeInTheDocument();
  });
});

describe("Scan progress", () => {
  it("lists the nine steps, says why steps were skipped and links to the results", async () => {
    renderAt(`/scan/${fixtures.scanRequest.id}`);
    const steps = await screen.findByRole("list", { name: "Scan steps" });
    expect(within(steps).getAllByRole("listitem")).toHaveLength(9);
    expect(within(steps).getByText(/symbol index/i)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Scan progress" })).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByRole("button", { name: "See the results" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Cancel scan" })).not.toBeInTheDocument();
  });

  it("shows a running scan with a cancel button and a failed step", async () => {
    const stages = fixtures.scanRequest.stages.map((s, i) => (i === 2 ? { ...s, status: "error", summary: "boom" } : i > 5 ? { ...s, status: "queued", summary: null } : s));
    vi.spyOn(api, "scanRequest").mockResolvedValue({ ...fixtures.scanRequest, status: "running", stages, steps_done: 5, error: "sbom: boom" });
    const cancel = vi.spyOn(api, "cancelScan").mockResolvedValue({ ...fixtures.scanRequest, status: "cancelled" });
    const user = userEvent.setup();
    renderAt(`/scan/${fixtures.scanRequest.id}`);
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("sbom: boom");
    await user.click(screen.getByRole("button", { name: "Cancel scan" }));
    expect(cancel).toHaveBeenCalled();
  });

  it("has an error state for an unknown scan", async () => {
    vi.spyOn(api, "scanRequest").mockRejectedValue(new Error("not found: scan request x"));
    renderAt("/scan/x");
    expect(await screen.findByRole("alert")).toHaveTextContent("not found");
  });
});

describe("Results and vulnerabilities", () => {
  it("lists a failed step at the top and retries only that step", async () => {
    const failed = {
      ...fixtures.scanRequest,
      stages: fixtures.scanRequest.stages.map((s) => (s.stage === "vulnerabilities" ? { ...s, status: "error", summary: "RuntimeError: OSV is down" } : s)),
    };
    vi.spyOn(api, "scanRequest").mockResolvedValue(failed);
    const retry = vi.spyOn(api, "retryStage").mockResolvedValue(fixtures.scanRequest);
    const user = userEvent.setup();
    renderAt("/scans/latest");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("One step failed");
    expect(alert).toHaveTextContent("OSV is down");
    await user.click(within(alert).getByRole("button", { name: "Retry this step" }));
    expect(retry).toHaveBeenCalledWith(failed.id, "vulnerabilities");
  });

  it("shows the four numbers, filters advisories and offers the SBOM", async () => {
    const user = userEvent.setup();
    renderAt("/scans/latest");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(/needs a decision/);
    expect(screen.getByRole("link", { name: "Download SBOM" })).toHaveAttribute("href", expect.stringContaining("/sbom"));
    const table = screen.getByRole("table", { name: "" });
    expect(within(table).getAllByRole("row").length).toBeGreaterThan(2);
    await user.click(screen.getByRole("button", { name: /Applies to your code/ }));
    const rows = within(screen.getAllByRole("table")[0]).getAllByRole("row");
    expect(rows).toHaveLength(2); // header + the one advisory that applies
    expect(rows[1]).toHaveTextContent("Applies");
    await user.click(screen.getByRole("button", { name: /Development only/ }));
    const devRows = within(screen.getAllByRole("table")[0]).getAllByRole("row");
    expect(devRows).toHaveLength(4); // header + the three development-only advisories
    expect(screen.getByText("Not shipped")).toBeInTheDocument();
    expect(screen.queryByText("lib/notify.js")).not.toBeInTheDocument();
  });

  it("says plainly when there are no advisories", async () => {
    vi.spyOn(api, "vulnerabilities").mockResolvedValue({ ...fixtures.vulnerabilities, rows: [], counts: { all: 0, applies: 0, runtime: 0, development: 0, transitive: 0 } });
    renderAt("/vulnerabilities");
    expect(await screen.findByText(/No known advisories in \d+ packages/)).toBeInTheDocument();
  });

  it("lists advisories across repositories with a repository column", async () => {
    renderAt("/vulnerabilities");
    expect(await screen.findByRole("columnheader", { name: "Repository" })).toBeInTheDocument();
  });

  it("disables the SBOM download when the scan has none", async () => {
    vi.spyOn(api, "summary").mockResolvedValue({ ...fixtures.summary, has_sbom: false });
    renderAt("/scans/latest");
    expect(await screen.findByRole("button", { name: "Download SBOM" })).toBeDisabled();
  });
});

describe("Source mapping", () => {
  const rows = [
    { ecosystem: "npm", name: "mocha", kind: "npm · dev", source: null, detail: null, found_by: "Not found", status: "not_found", note: null },
    { ecosystem: "npm", name: "orders", kind: "npm · runtime", source: "github.com/acme/orders", detail: "GitHub releases", found_by: "deps.dev", status: "confirm", note: null },
    { ecosystem: "npm", name: "express", kind: "npm · runtime", source: "github.com/expressjs/express", detail: "GitHub releases", found_by: "Registry metadata", status: "verified", note: null },
  ];

  it("asks about what needs a person first, confirms, and saves an override", async () => {
    vi.spyOn(api, "sources").mockResolvedValue({ scan_id: "s", to_confirm: 2, rows });
    const confirm = vi.spyOn(api, "confirmSource").mockResolvedValue({ confirmed: "npm/orders" });
    const override = vi.spyOn(api, "overrideSource").mockResolvedValue({ saved: "npm/mocha" });
    const user = userEvent.setup();
    renderAt("/setup/sources");
    await user.click(await screen.findByRole("button", { name: "Confirm" }));
    expect(confirm).toHaveBeenCalledWith("orders");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled(); // nothing typed yet
    await user.type(screen.getByLabelText("Repository", { selector: "input" }), "mochajs/mocha");
    await user.click(save);
    expect(override).toHaveBeenCalledWith({ name: "mocha", kind: "repository", owner: "mochajs", repo: "mocha" });
  });

  it("offers the other source types and sends them as such", async () => {
    vi.spyOn(api, "sources").mockResolvedValue({ scan_id: "s", to_confirm: 1, rows: [rows[0]] });
    const override = vi.spyOn(api, "overrideSource").mockResolvedValue({ saved: "npm/mocha" });
    const user = userEvent.setup();
    renderAt("/setup/sources");
    await user.selectOptions(await screen.findByLabelText("Source type"), "slack");
    await user.type(screen.getByLabelText("Channel"), "#payments-changes");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(override).toHaveBeenCalledWith({ name: "mocha", kind: "slack", url: "#payments-changes" });
  });

  it("has an empty state before any scan", async () => {
    vi.spyOn(api, "sources").mockResolvedValue({ scan_id: "s", to_confirm: 0, rows: [] });
    renderAt("/setup/sources");
    expect(await screen.findByText("No dependencies yet")).toBeInTheDocument();
  });
});

describe("Channels and schedules", () => {
  it("shows secret names but never secret values, and tests a connection", async () => {
    const test = vi.spyOn(api, "testChannel").mockResolvedValue({ ok: true, detail: "Token valid for Acme" });
    const user = userEvent.setup();
    const { container } = renderAt("/setup/channels");
    expect(await screen.findByRole("heading", { name: "Change sources" })).toBeInTheDocument();
    expect(screen.getAllByText("SLACK_BOT_TOKEN").length).toBeGreaterThan(0);
    expect(container.textContent).not.toMatch(/xoxb-|ghp_|Bearer /);
    const slack = screen.getByText("Slack").closest("article")!;
    await user.click(within(slack).getByRole("button", { name: "Test connection" }));
    expect(test).toHaveBeenCalledWith(expect.any(String), "slack");
  });

  it("disables the webhook install until the server knows its public address", async () => {
    const view = { ...fixtures.channels, webhook: { url: null, public_url_set: false, secret_set: true, token_set: true } };
    vi.spyOn(api, "channels").mockResolvedValue(view);
    renderAt("/setup/channels");
    expect(await screen.findByRole("button", { name: "Install webhook" })).toBeDisabled();
    expect(screen.getByText(/PILOT_PUBLIC_URL/, { selector: "p" })).toBeInTheDocument();
  });

  it("saves a schedule change", async () => {
    const update = vi.spyOn(api, "updateSchedule").mockResolvedValue(fixtures.channels.schedules[0]);
    const user = userEvent.setup();
    renderAt("/setup/channels");
    const select = await screen.findByLabelText("How often: Dependabot alerts");
    await user.selectOptions(select, "60");
    expect(update).toHaveBeenCalledWith(expect.stringContaining("dependabot_alerts"), { interval_minutes: 60, enabled: undefined });
  });

  it("explains an empty state", async () => {
    vi.spyOn(api, "repos").mockResolvedValue([]);
    renderAt("/setup/channels");
    expect(await screen.findByText("No repository yet")).toBeInTheDocument();
  });
});

describe("Update feed", () => {
  it("shows the express change with its quote, impact and tracker", async () => {
    renderAt("/updates");
    const card = await screen.findByTestId("change-card");
    expect(card).toHaveTextContent("express 4.21.2 → 5.3.0 changes APIs your code uses");
    expect(card).toHaveTextContent("7 lines in 5 files");
    expect(within(card).getByText(/checked word for word/)).toBeInTheDocument();
    expect(within(card).getByRole("img", { name: /Step 5 of 6/ })).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Review and approve" })).toHaveAttribute("href", expect.stringContaining("/upgrades/"));
  });

  it("filters through the API and has an empty state", async () => {
    const changes = vi.spyOn(api, "changes").mockResolvedValue({ counts: { all: 0, needs_decision: 0, outside: 0, inside: 0, security: 0 }, cards: [] });
    const user = userEvent.setup();
    renderAt("/updates");
    expect(await screen.findByText("No changes yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Security/ }));
    expect(changes).toHaveBeenCalledWith("security");
    expect(await screen.findByText("Nothing matches this filter")).toBeInTheDocument();
  });
});

describe("Navigation", () => {
  it("has the new pages and the setup group in the rail", async () => {
    renderAt("/scan");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    for (const name of ["Scan a repository", "Vulnerabilities", "Updates", "Source mapping", "Channels and schedules"]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(within(nav).getByRole("link", { name: "Scan a repository" })).toHaveAttribute("aria-current", "page");
  });
});
