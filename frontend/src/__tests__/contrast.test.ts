import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/** S8: every text and background pair the pages use must reach 4.5:1 in both themes (WCAG AA for body text). */
const css = readFileSync(resolve(__dirname, "../styles/tokens.css"), "utf-8");

function tokens(block: string): Record<string, string> {
  return Object.fromEntries([...block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})/g)].map((m) => [m[1], m[2]]));
}
const light = tokens(css.slice(0, css.indexOf("@media")));
const dark = { ...light, ...tokens(css.slice(css.indexOf("@media"))) };

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
export function ratio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const WHITE = "#ffffff";
// [text token or colour, background token or colour, where it is used]
const PAIRS: [string, string, string][] = [
  ["ink", "bg", "body text"], ["ink-2", "bg", "secondary text"], ["ink-3", "bg", "muted text"],
  ["ink", "white", "text on cards"], ["ink-2", "white", "secondary text on cards"], ["ink-3", "white", "muted text on cards"],
  ["ink-3", "panel", "muted text on panels"], ["ink-2", "panel-2", "text on the rail"], ["ink-3", "panel-2", "muted text on the rail"],
  ["medium", "bg", "links"], ["medium", "white", "links on cards"],
  ["critical", "critical-bg", "failure pills"], ["high", "high-bg", "waiting pills"], ["medium", "medium-bg", "running pills"],
  ["low", "low-bg", "done pills"], ["critical", "white", "critical risk text"], ["high", "white", "high risk text"],
  ["low", "white", "low risk text"], ["bg", "ink", "primary buttons"], ["ink", "high-bg", "table rows that ask for a person"],
  ["ink-2", "high-bg", "secondary text on those rows"],
];

for (const [name, theme] of [["light", light], ["dark", dark]] as const) {
  describe(`contrast, ${name} theme`, () => {
    const rows = PAIRS.map(([fg, bg, use]) => ({ fg, bg, use, ratio: ratio(theme[fg] ?? fg, theme[bg] ?? bg) }));
    it("lists every pair with its ratio", () => {
      console.info(`\n${name} theme\n` + rows.map((r) => `${r.ratio.toFixed(2).padStart(6)}  ${r.fg} on ${r.bg}  (${r.use})`).join("\n"));
      expect(rows.length).toBeGreaterThan(15);
    });
    it.each(rows)("$fg on $bg ($use) is at least 4.5:1", (row) => {
      expect(row.ratio).toBeGreaterThanOrEqual(4.5);
    });
  });
}

describe("white text on solid badges", () => {
  it.each([["critical"], ["low"]])("white on %s", (token) => {
    expect(ratio(WHITE, light[token])).toBeGreaterThanOrEqual(4.5);
  });
});
