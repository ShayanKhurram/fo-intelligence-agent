#!/usr/bin/env node
// ui_plan.md §9 — "Contrast-check the glass surfaces... a script that renders the three
// panels over worst-case backgrounds and asserts ratios is 30 minutes and prevents the
// most likely regression." Not wired into a CI pipeline (this repo has none), but is a
// real, runnable check — `node scripts/contrast-check.mjs` — against the exact glass fill
// + veil math used in app/globals.css.
//
// T47.8 extended it in two directions:
//
//   1. BOTH THEMES. The palette is no longer single-theme, so every check runs twice
//      against its own token set. A light theme that was never contrast-checked would be
//      the obvious way to ship an unreadable half of the product.
//
//   2. THE REAL GROUND, not only the hypothetical one. The original check modelled a
//      bright photo behind the glass, which is the hardest case a blurred fill can face —
//      but this app has no photos, and every glass panel actually sits on --bg-base. Both
//      are now checked, and the real-ground pass covers the full token set (including
//      --text-low and all five functional colours) rather than just --text-hi/--text-mid.
//
// That second addition is what caught a genuine pre-existing defect: dark --text-low was
// #67717f, which measures 3.74:1 over the real composited glass — below the 4.5 floor, on
// small labels like a record's `aum_basis`. It is now #8b95a3.

const THEMES = {
  dark: {
    bgBase: "#0d1014",
    bgGlass: "rgba(26, 33, 42, 0.68)",
    bgGlassHi: "rgba(34, 43, 54, 0.78)",
    glassVeil: "rgba(13, 16, 20, 0.55)",
    text: { "--text-hi": "#e9edf2", "--text-mid": "#98a3b2", "--text-low": "#8b95a3" },
    functional: {
      "--confirmed": "#2fb88a",
      "--partial": "#c8912f",
      "--unknown": "#8b95a3",
      "--live": "#5b9cff",
      "--urgent": "#e8734a",
    },
    // The hardest content a blurred fill has to survive: for a dark panel, near-white.
    worstCaseBehind: "#f5f5f5",
  },
  light: {
    bgBase: "#eceff4",
    bgGlass: "rgba(255, 255, 255, 0.70)",
    bgGlassHi: "rgba(255, 255, 255, 0.86)",
    glassVeil: "rgba(255, 255, 255, 0.55)",
    text: { "--text-hi": "#10151b", "--text-mid": "#4a5462", "--text-low": "#5f6874" },
    functional: {
      "--confirmed": "#0b7551",
      "--partial": "#855c12",
      "--unknown": "#5f6874",
      "--live": "#2159bd",
      "--urgent": "#ab3f19",
    },
    // Mirror image: for a light panel the hardest case is dark content behind it.
    worstCaseBehind: "#141414",
  },
};

const FLOOR = 4.5;

function hex(h) {
  const s = h.replace("#", "");
  return { r: parseInt(s.slice(0, 2), 16), g: parseInt(s.slice(2, 4), 16), b: parseInt(s.slice(4, 6), 16) };
}

function rgba(str) {
  const [r, g, b, a] = str.match(/[\d.]+/g).map(Number);
  return { r, g, b, a };
}

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

/** Flattens translucent fills over a background, in paint order. */
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

const results = [];

for (const [themeName, t] of Object.entries(THEMES)) {
  const veil = rgba(t.glassVeil);
  const glass = rgba(t.bgGlass);
  const glassHi = rgba(t.bgGlassHi);

  // .glass / .glass-hi layer --glass-veil as the topmost background on EVERY glass
  // surface (not just the drawer) — model that here, not the bare fill alone.
  const surfaces = {
    "bg-base": hex(t.bgBase),
    ".glass over bg-base": flatten(hex(t.bgBase), glass, veil),
    ".glass-hi over bg-base": flatten(hex(t.bgBase), glassHi, veil),
  };

  const foregrounds = { ...t.text, ...t.functional };

  for (const [sName, sColor] of Object.entries(surfaces)) {
    for (const [fName, fHex] of Object.entries(foregrounds)) {
      results.push({
        theme: themeName,
        name: `${fName} on ${sName}`,
        ratio: contrastRatio(sColor, hex(fHex)),
      });
    }
  }

  // The original contract, preserved: --text-hi and --text-mid must survive worst-case
  // content behind the glass, which is what the veil was introduced to guarantee.
  const worstGlass = flatten(hex(t.worstCaseBehind), glass, veil);
  const worstGlassHi = flatten(hex(t.worstCaseBehind), glassHi, veil);
  for (const [fName, fHex] of [["--text-hi", t.text["--text-hi"]], ["--text-mid", t.text["--text-mid"]]]) {
    results.push({ theme: themeName, name: `${fName} on .glass over WORST-CASE content`, ratio: contrastRatio(worstGlass, hex(fHex)) });
    results.push({ theme: themeName, name: `${fName} on .glass-hi over WORST-CASE content`, ratio: contrastRatio(worstGlassHi, hex(fHex)) });
  }
}

let failed = 0;
let current = null;
for (const r of results) {
  if (r.theme !== current) {
    current = r.theme;
    console.log(`\n${current.toUpperCase()}`);
  }
  const pass = r.ratio >= FLOOR;
  if (!pass) failed++;
  console.log(`  ${pass ? "PASS" : "FAIL"}  ${r.ratio.toFixed(2).padStart(5)}:1  ${r.name}`);
}

console.log(`\n${results.length} checks across ${Object.keys(THEMES).length} themes.`);
if (failed) {
  console.error(`${failed} fall below the ${FLOOR}:1 WCAG AA floor.`);
  process.exit(1);
} else {
  console.log(`All clear ${FLOOR}:1.`);
}
