import { expect, test } from "@playwright/test";

// One pass through the golden path in a real browser: read the evidence, approve, see the pull request.
test("overview -> evidence -> approve -> pull request", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("1 upgrade needs a decision");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Express 5 would break 7 lines");

  await page.getByRole("list", { name: "Upgrades by risk" }).getByRole("listitem").first().click();
  await expect(page.getByRole("tab", { name: "Affected code" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("routes/orders.js").first()).toBeVisible();
  await expect(page.getByText("lib/notify.js")).toHaveCount(0); // the negative control is never reported

  await page.getByRole("tab", { name: "What changed" }).click();
  await expect(page.getByRole("link", { name: "Open the release notes" }).first()).toBeVisible();

  await page.getByRole("tab", { name: "Blast radius" }).click();
  await expect(page.getByTestId("blast-radius").locator(".react-flow__node").first()).toBeVisible();
  await expect(page.locator(".react-flow__node.rf-blast_radius")).not.toHaveCount(0);

  await page.getByRole("tab", { name: "Fix and tests" }).click();
  await expect(page.getByLabel("Proposed changes")).toContainText("req.params.id");
  await expect(page.getByText("9 of 9 pass").last()).toBeVisible();

  await page.getByRole("tab", { name: "Activity" }).click();
  await expect(page.getByRole("list", { name: "Activity" })).toContainText("impact");

  await page.getByRole("button", { name: "Approve and open PR" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Your name").fill("alice");
  await dialog.getByRole("button", { name: "Approve and open PR" }).click();

  await expect(page.getByText("Pull request opened").first()).toBeVisible({ timeout: 45_000 });
  await expect(page.getByRole("button", { name: "Approve and open PR" })).toBeDisabled();
  await expect(page.getByText(/A local pull request was written/)).toBeVisible();

  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).not.toContainText("needs a decision");
});

test("keyboard users can reach the tabs and the page has no horizontal scroll at 360px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto("/dependencies");
  await expect(page.getByRole("heading", { name: "Dependencies" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.getByRole("link", { name: "Approvals" }).focus();
  await expect(page.getByRole("link", { name: "Approvals" })).toBeFocused();
});
