#!/usr/bin/env node
// ui_plan.md §9 — "Contrast-check the glass surfaces... a script that renders the three
// panels over worst-case backgrounds and asserts ratios is 30 minutes and prevents the
// most likely regression." Not wired into a CI pipeline (this repo has none), but is a
// real, runnable check — `node scripts/contrast-check.mjs` — against the exact glass
// fill + scrim math used in app/globals.css.

const BG_GLASS = { r: 26, g: 33, b: 42, a: 0.68 }; // --bg-glass
const BG_GLASS_HI = { r: 34, g: 43, b: 54, a: 0.78 }; // --bg-glass-hi
const SCRIM = { r: 13, g: 16, b: 20, a: 0.55 }; // --scrim
const TEXT_HI = { r: 233, g: 237, b: 242 }; // --text-hi
const TEXT_MID = { r: 152, g: 163, b: 178 }; // --text-mid

// Worst-case content behind a glass panel: a bright, busy photo-like background —
// modeled as near-white, which is the hardest case a blurred fill has to survive.
const WORST_CASE_BEHIND = { r: 245, g: 245, b: 245 };

function srgbToLinear(c) {
  const cs = c / 255;
  return cs <= 0.04045 ? cs / 12.92 : Math.pow((cs + 0.055) / 1.055, 2.4);
}

function relativeLuminance({ r, g, b }) {
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

function contrastRatio(c1, c2) {
  const l1 = relativeLuminance(c1);
  const l2 = relativeLuminance(c2);
  const [lighter, darker] = l1 > l2 ? [l1, l2] : [l2, l1];
  return (lighter + 0.05) / (darker + 0.05);
}

// Flattens a translucent fill (optionally with a scrim on top) over a background.
function flatten(bg, ...layers) {
  let out = bg;
  for (const layer of layers) {
    out = {
      r: layer.r * layer.a + out.r * (1 - layer.a),
      g: layer.g * layer.a + out.g * (1 - layer.a),
      b: layer.b * layer.a + out.b * (1 - layer.a),
    };
  }
  return out;
}

// .glass / .glass-hi in app/globals.css layer --scrim as the topmost background on
// EVERY glass surface (not just the drawer) — model that here, not the bare fill alone.
const checks = [
  { name: "records rail (.glass) + --text-hi over worst-case content", bg: flatten(WORST_CASE_BEHIND, BG_GLASS, SCRIM), fg: TEXT_HI },
  { name: "records rail (.glass) + --text-mid over worst-case content", bg: flatten(WORST_CASE_BEHIND, BG_GLASS, SCRIM), fg: TEXT_MID },
  { name: "evidence drawer (.glass-hi) + --text-hi over worst-case content", bg: flatten(WORST_CASE_BEHIND, BG_GLASS_HI, SCRIM), fg: TEXT_HI },
  { name: "evidence drawer (.glass-hi) + --text-mid over worst-case content", bg: flatten(WORST_CASE_BEHIND, BG_GLASS_HI, SCRIM), fg: TEXT_MID },
  { name: "input bar (.glass) + --text-hi over worst-case content", bg: flatten(WORST_CASE_BEHIND, BG_GLASS, SCRIM), fg: TEXT_HI },
];

let failed = false;
for (const c of checks) {
  const ratio = contrastRatio(c.bg, c.fg);
  const pass = ratio >= 4.5;
  if (!pass) failed = true;
  console.log(`${pass ? "PASS" : "FAIL"}  ${ratio.toFixed(2)}:1  ${c.name}`);
}

if (failed) {
  console.error("\nOne or more glass surfaces fall below the 4.5:1 WCAG AA floor over worst-case content.");
  process.exit(1);
} else {
  console.log("\nAll checked glass surfaces clear 4.5:1 over worst-case content behind them.");
}
