import { test } from "@playwright/test";

// Screenshots of the new pages for review against docs/design/v2: `SHOTS=1 npx playwright test e2e/shots.spec.ts`.
// Output goes to frontend/screenshots (ignored by git).
const PAGES = ["/scan", "/updates", "/vulnerabilities", "/setup/sources", "/setup/channels", "/upgrades"];
const SIZES = [{ name: "1440", width: 1440, height: 900 }, { name: "390", width: 390, height: 844 }];

test.skip(!process.env.SHOTS, "set SHOTS=1 to take screenshots");

for (const scheme of ["light", "dark"] as const) {
  for (const size of SIZES) {
    test(`${scheme} ${size.name}`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: size.width, height: size.height });
      const names: Record<string, string> = {};
      for (const path of PAGES) {
        await page.goto(path);
        await page.waitForLoadState("networkidle");
        names[path] = path.replace(/\//g, "_") || "home";
        await page.screenshot({ path: `screenshots/${scheme}-${size.name}${names[path]}.png`, fullPage: true });
      }
      await page.goto("/");
      await page.waitForLoadState("networkidle");
      const link = page.getByRole("link", { name: "Scan a repository" }).first();
      await link.click();
      await page.getByRole("group", { name: "Recent repositories" }).getByRole("button").first().click();
      await page.getByRole("button", { name: "Start scan" }).click();
      await page.getByText("Scan complete").waitFor({ timeout: 90_000 });
      await page.screenshot({ path: `screenshots/${scheme}-${size.name}_progress.png`, fullPage: true });
      await page.getByRole("button", { name: "See the results" }).click();
      await page.waitForLoadState("networkidle");
      await page.screenshot({ path: `screenshots/${scheme}-${size.name}_results.png`, fullPage: true });
    });
  }
}
