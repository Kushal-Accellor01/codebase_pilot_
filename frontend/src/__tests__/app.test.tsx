import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { api } from "../api/client";
import { fixtures } from "../api/fixtures";
import { layout } from "../components/BlastRadius";
import { Async } from "../components/ui";

const runId = fixtures.upgrades[0].id;

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

describe("Overview", () => {
  it("leads with the decision, then the board", async () => {
    renderAt("/");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(
      /^1 upgrade needs a decision\. Express 5 would break 7 lines, and 4 of them already warn in production\.$/,
    );
    const board = screen.getByRole("list", { name: "Upgrades by risk" });
    const row = within(board).getByRole("listitem");
    expect(row).toHaveAttribute("href", `/upgrades/${runId}`);
    expect(row).toHaveTextContent("express");
    expect(row).toHaveTextContent("4.21.2");
    expect(row).toHaveTextContent("5.3.0");
    expect(row).toHaveTextContent("Critical");
    expect(row).toHaveTextContent("Awaiting approval");
    expect(within(row).getByText(/7 lines in 5 files/).tagName).toBe("B");
  });

  it("shows who is waiting, the risk stack and the affected files", async () => {
    renderAt("/");
    const waiting = (await screen.findByRole("heading", { name: "Waiting for you" })).closest("section")!;
    expect(within(waiting).getByRole("link")).toHaveTextContent("express 4.21.2 to 5.3.0");
    expect(screen.getByRole("img", { name: /1 critical, 0 high, 0 medium, 0 low, 2 up to date/ })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "routes/orders.js" })).toBeInTheDocument();
    expect(screen.queryByText("lib/notify.js")).not.toBeInTheDocument(); // the negative control is never listed
    expect(screen.getByText("No known advisories for the versions in use.")).toBeInTheDocument();
  });

  it("marks the current page in the navigation and shows the waiting count", async () => {
    renderAt("/");
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByLabelText("1 waiting")).toBeInTheDocument();
  });
});

describe("Dependencies", () => {
  it("filters the table and says when nothing matches", async () => {
    const user = userEvent.setup();
    renderAt("/dependencies");
    expect(await screen.findByRole("cell", { name: "express" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Reachable in code")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Dev" }));
    expect(screen.getByText("No dependencies match this filter")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Major" }));
    expect(screen.getByRole("cell", { name: "express" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Security" }));
    expect(screen.queryByRole("cell", { name: "express" })).not.toBeInTheDocument();
  });

  it("opens an upgrade run from its row", async () => {
    const user = userEvent.setup();
    renderAt("/dependencies");
    await user.click(await screen.findByRole("cell", { name: "express" }));
    expect(await screen.findByRole("tab", { name: "Affected code" })).toBeInTheDocument();
  });
});

describe("Upgrade detail", () => {
  it("shows the header, the evidence strip and the affected code first", async () => {
    renderAt(`/upgrades/${runId}`);
    expect(await screen.findByRole("button", { name: "Approve and open PR" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    const strip = screen.getByLabelText("Evidence summary");
    const evidence = fixtures.upgrade.evidence;
    expect(within(strip).getByText("runtime deprecation warnings").previousSibling).toHaveTextContent(String(evidence.runtime_warnings));
    expect(within(strip).getByText(/matching production log lines/).previousSibling).toHaveTextContent(String(evidence.production_lines));
    expect(within(strip).getByText(/fail on 5\.3\.0 without the fix/).previousSibling).toHaveTextContent(String(evidence.failing_places));
    expect(screen.getByText("routes/orders.js")).toBeInTheDocument();
  });

  it("lists every finding with its three signals and never the negative control", async () => {
    renderAt(`/upgrades/${runId}`);
    await screen.findByRole("tab", { name: "Affected code" });
    expect(screen.queryByText("lib/notify.js")).not.toBeInTheDocument();
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getAllByText("✓ Failing test").length).toBeGreaterThanOrEqual(5);
    expect(within(panel).getAllByText(/Code search/).length).toBeGreaterThan(0);
    expect(within(panel).getByText("req.param")).toBeInTheDocument(); // the highlighted API in the snippet
    expect(within(panel).getAllByRole("button", { name: "Not relevant" }).length).toBeGreaterThan(0);
  });

  it("switches tabs with the mouse and the arrow keys", async () => {
    const user = userEvent.setup();
    renderAt(`/upgrades/${runId}`);
    const first = await screen.findByRole("tab", { name: "Affected code" });
    expect(first).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "What changed" }));
    expect(screen.getAllByRole("blockquote").length).toBeGreaterThanOrEqual(5);
    expect(screen.getAllByRole("link", { name: "Open the release notes" })[0]).toHaveAttribute("href", expect.stringContaining("github.com/expressjs/express"));

    screen.getByRole("tab", { name: "What changed" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Blast radius" })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowRight}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Plan" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText(/Why critical:/)).toBeInTheDocument();
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("list", { name: "Activity" })).toBeInTheDocument();
    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "Affected code" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows production evidence, the diff and the three test runs", async () => {
    const user = userEvent.setup();
    renderAt(`/upgrades/${runId}`);
    await user.click(await screen.findByRole("tab", { name: "Production evidence" }));
    expect(screen.getByText(/log lines matched the affected APIs/)).toBeInTheDocument();
    expect(screen.getAllByText(/express deprecated req\.param/).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("tab", { name: "Fix and tests" }));
    const diff = screen.getByLabelText("Proposed changes");
    const added = [...diff.querySelectorAll(".add")].map((el) => el.textContent);
    expect(added).toContain("+  const id = Number(req.params.id);");
    expect(diff.querySelectorAll(".add").length).toBeGreaterThan(5);
    expect(diff.querySelectorAll(".del").length).toBeGreaterThan(5);
    expect(screen.getByText("On 4.21.2")).toBeInTheDocument();
    expect(screen.getByText("On 5.3.0 without the fix").parentElement).toHaveTextContent("Some tests fail");
    expect(screen.getByText("On 5.3.0 with the fix").parentElement).toHaveTextContent("9 of 9 pass");
  });

  it("renders the blast radius and sends a clicked file back to its lines", async () => {
    const user = userEvent.setup();
    renderAt(`/upgrades/${runId}`);
    await user.click(await screen.findByRole("tab", { name: "Blast radius" }));
    expect(await screen.findByTestId("blast-radius")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toContain("lib/notify.js: blast radius");
    expect(items.find((t) => t?.startsWith("routes/orders.js"))).toMatch(/affected, \d+ affected lines/);
  });
});

describe("Decisions", () => {
  it("needs a name, sends the decision, and tells the user", async () => {
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ status: "queued" });
    const user = userEvent.setup();
    renderAt(`/upgrades/${runId}`);
    await user.click(await screen.findByRole("button", { name: "Approve and open PR" }));
    const dialog = await screen.findByRole("dialog", { name: "Approve and open a pull request?" });
    const confirm = within(dialog).getByRole("button", { name: "Approve and open PR" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Your name"), "alice");
    await user.type(within(dialog).getByLabelText(/Note for reviewers/), "ship it");
    await user.click(confirm);
    expect(decide).toHaveBeenCalledWith(runId, { decision: "approved", decided_by: "alice", comment: "ship it" });
    expect(await screen.findByText(/Approved\. Pilot is opening the pull request\./)).toBeInTheDocument();
  });

  it("rejects without a comment", async () => {
    const decide = vi.spyOn(api, "decide").mockResolvedValue({ status: "queued" });
    const user = userEvent.setup();
    renderAt(`/upgrades/${runId}`);
    await user.click(await screen.findByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog", { name: "Reject this upgrade?" });
    await user.clear(within(dialog).getByLabelText("Your name"));
    await user.type(within(dialog).getByLabelText("Your name"), "bob");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(decide).toHaveBeenCalledWith(runId, { decision: "rejected", decided_by: "bob", comment: null });
  });

  it("disables the buttons once the run is no longer waiting", async () => {
    vi.spyOn(api, "upgrade").mockResolvedValue({ ...fixtures.upgrade, can_decide: false });
    renderAt(`/upgrades/${runId}`);
    expect(await screen.findByRole("button", { name: "Approve and open PR" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });
});

describe("Pending decision", () => {
  it("shows that the decision was sent and keeps both buttons disabled", async () => {
    vi.spyOn(api, "upgrade").mockResolvedValue({ ...fixtures.upgrade, decision_pending: true, can_decide: false });
    renderAt(`/upgrades/${runId}`);
    expect(await screen.findByRole("button", { name: "Decision sent" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByText(/Waiting for the worker/)).toBeInTheDocument();
  });
});

describe("States", () => {
  it("shows a loading state, then data", async () => {
    renderAt("/approvals");
    expect(screen.getByRole("status", { name: "Loading approvals" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Review" })).toHaveAttribute("href", `/upgrades/${runId}`);
  });

  it("shows an empty state", async () => {
    vi.spyOn(api, "upgrades").mockResolvedValue([]);
    renderAt("/approvals");
    expect(await screen.findByText("Nothing is waiting")).toBeInTheDocument();
  });

  it("shows an error state with the reason", async () => {
    vi.spyOn(api, "overview").mockRejectedValue(new Error("the API is down"));
    renderAt("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("the API is down");
  });

  it("covers every state in the Async helper", () => {
    const { rerender } = render(<Async query={{ isLoading: true, isError: false, error: null, data: undefined }}>{() => "x"}</Async>);
    expect(screen.getByRole("status")).toBeInTheDocument();
    rerender(<Async query={{ isLoading: false, isError: false, error: null, data: undefined }}>{() => "x"}</Async>);
    expect(screen.getByText("Nothing here yet")).toBeInTheDocument();
    rerender(<Async query={{ isLoading: false, isError: false, error: null, data: 1 }}>{(n) => `value ${n}`}</Async>);
    expect(screen.getByText("value 1")).toBeInTheDocument();
  });

  it("falls back to the overview for unknown routes", async () => {
    renderAt("/nowhere");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(/upgrade/);
  });
});

describe("Blast radius layout", () => {
  it("places the package left of the files and colours nodes by status", () => {
    const { nodes, edges } = layout(fixtures.graph);
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    expect(byId["dep:express"].className).toBe("rf-target");
    expect(byId["file:routes/orders.js"].className).toBe("rf-affected");
    expect(byId["file:lib/notify.js"].className).toBe("rf-blast_radius");
    expect(byId["dep:express"].position.x).toBeLessThan(byId["file:routes/orders.js"].position.x);
    expect(byId["file:routes/orders.js"].position.x).toBeLessThan(byId["file:lib/notify.js"].position.x);
    expect(edges.length).toBe(fixtures.graph.edges.length);
  });
});

describe("Scan a repository", () => {
  it("sends the URL typed in the header, and rescans the last repository when it is empty", async () => {
    const start = vi.spyOn(api, "startScan").mockResolvedValue({ job_id: "j1" });
    const user = userEvent.setup();
    renderAt("/");
    const field = await screen.findByLabelText("Repository to scan");
    await user.type(field, "https://github.com/acme/shop");
    await user.click(screen.getByRole("button", { name: "Scan this repository" }));
    expect(start).toHaveBeenCalledWith("https://github.com/acme/shop");
    expect(await screen.findByRole("button", { name: "Scan now" })).toBeEnabled();
  });
});
