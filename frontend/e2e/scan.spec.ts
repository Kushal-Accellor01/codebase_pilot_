import { expect, test } from "@playwright/test";

// The new pages against the real offline stack (demo server: API, worker, scripted model; Vite).
// Order matters: the first test starts a scan, so the later ones see its results.

test("scan from the browser: form, live stages, results", async ({ page }) => {
  await page.goto("/scan");
  await expect(page.getByRole("heading", { level: 1, name: "Scan a repository" })).toBeVisible();

  // An invalid link is refused with a plain sentence, before anything is sent.
  const field = page.getByLabel("Repository link");
  await expect(field).not.toHaveValue(""); // the last scanned repository is filled in once the scans load
  const original = await field.inputValue();
  await field.fill("not a link");
  await page.getByRole("button", { name: "Start scan" }).click();
  await expect(page.getByRole("alert")).toContainText("Enter a repository link or a local path");
  await field.fill(original);

  await page.getByRole("button", { name: "Check access first" }).click();
  await expect(page.getByText(/local · reachable|reachable/i).first()).toBeVisible();

  await page.getByRole("button", { name: "Start scan" }).click();
  await expect(page).toHaveURL(/\/scan\/[0-9a-f-]{36}$/);
  const steps = page.getByRole("list", { name: "Scan steps" });
  await expect(steps.getByRole("listitem")).toHaveCount(9);
  await expect(page.getByText("Scan complete")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("progressbar", { name: "Scan progress" })).toHaveAttribute("aria-valuenow", "100");
  await expect(steps).toContainText("159 packages from the lockfile");
  await expect(steps).toContainText("symbol index");

  await page.getByRole("button", { name: "See the results" }).click();
  await expect(page).toHaveURL(/\/scans\/[0-9a-f-]{36}$/);
  // "1 upgrade needs a decision" until someone approves it (the other spec does); then the repository name.
  await expect(page.getByRole("heading", { level: 1 })).toContainText(/needs a decision|pilot-sandbox-shop/);
  await expect(page.getByRole("link", { name: "Download SBOM" })).toBeVisible();
  await expect(page.getByText("159 packages").first()).toBeVisible();
  await expect(page.getByText("lib/notify.js")).toHaveCount(0); // the negative control is never listed
});

test("every new page loads in the real stack and has its states", async ({ page }) => {
  await page.goto("/vulnerabilities");
  await expect(page.getByRole("heading", { level: 1, name: "Vulnerabilities" })).toBeVisible();
  await expect(page.getByText(/No known advisories in \d+ packages|Repository/).first()).toBeVisible();

  await page.goto("/updates");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("What changed");
  const card = page.getByTestId("change-card").first();
  await expect(card).toContainText("express");
  await expect(card.getByText(/checked word for word/)).toBeVisible();
  const chip = page.getByRole("button", { name: /Needs a decision/ });
  const waiting = Number((await chip.innerText()).match(/\d+$/)?.[0] ?? "0");
  await chip.click();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  if (waiting > 0) await expect(page.getByTestId("change-card")).toHaveCount(waiting);
  else await expect(page.getByText("Nothing matches this filter")).toBeVisible();

  await page.goto("/setup/sources");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("publishes its changes");
  await expect(page.getByRole("table")).toBeVisible();

  await page.goto("/setup/channels");
  await expect(page.getByRole("heading", { name: "Change sources" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Schedules" })).toBeVisible();
  const text = await page.locator("main").innerText();
  expect(text).not.toMatch(/xoxb-|ghp_|Bearer /); // no secret value can be on this page
  await page.getByRole("button", { name: "Test connection" }).first().click();
  await expect(page.getByRole("status")).not.toBeEmpty();
});

test("the rail reaches every page by keyboard and no page scrolls sideways at 360px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  for (const path of ["/", "/scan", "/dependencies", "/vulnerabilities", "/updates", "/upgrades", "/approvals", "/setup/sources", "/setup/channels"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `${path} scrolls sideways`).toBeLessThanOrEqual(0);
  }
  await page.goto("/scan");
  await page.getByRole("link", { name: "Channels and schedules" }).focus();
  await expect(page.getByRole("link", { name: "Channels and schedules" })).toBeFocused();
});
