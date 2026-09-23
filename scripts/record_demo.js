/**
 * Records screen footage of the GraphSleuth analyst UI and the GitHub repo for the demo video.
 *
 *   node scripts/record_demo.js ui      # records the local analyst UI (needs uvicorn on :8000)
 *   node scripts/record_demo.js github  # records the public repo page
 *
 * Writes .webm into /tmp/gs_recordings/, one file per run. Convert to mp4 before using in
 * a HyperFrames composition.
 */
const { chromium } = require("playwright");

const OUT = "/tmp/gs_recordings";
const VIEWPORT = { width: 1920, height: 1080 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function smoothScroll(page, totalPx, steps = 60, pause = 40) {
  const per = totalPx / steps;
  for (let i = 0; i < steps; i++) {
    await page.mouse.wheel(0, per);
    await sleep(pause);
  }
}

async function recordUI() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: OUT, size: VIEWPORT },
    deviceScaleFactor: 1,
  });
  const page = await ctx.newPage();

  await page.goto("http://localhost:8000", { waitUntil: "networkidle" });
  // The console ships dark; the demo video is light-themed, so flip it before recording.
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await sleep(2500);

  // Select HHG-014 — the undocumented 28-card device ring, the strongest case.
  const target = page.locator("#c-HHG-014");
  if (await target.count()) {
    await target.scrollIntoViewIfNeeded();
    await sleep(600);
    await target.click();
  }
  await sleep(3000);

  // Run it live so the tool calls stream in one by one.
  const runBtn = page.locator("#run");
  if (await runBtn.count()) {
    await runBtn.click();
  }

  // Let the investigation stream. TigerGraph + LLM can take a while; poll for the
  // verdict hero to settle rather than guessing a fixed wait.
  const deadline = Date.now() + 150000;
  while (Date.now() < deadline) {
    const done = await page.locator("#stepcard .steps li").count();
    const btn = await page.locator("#run").getAttribute("disabled").catch(() => null);
    if (done >= 9 && btn === null) break;
    await sleep(1500);
  }
  await sleep(3500);

  // Walk the result: verdict + gauge, evidence, graph, SAR.
  await smoothScroll(page, 1500, 50, 45);
  await sleep(2000);
  await smoothScroll(page, 1800, 55, 45);
  await sleep(2500);
  await smoothScroll(page, 1800, 55, 45);
  await sleep(3000);

  await ctx.close();
  await browser.close();
  console.log("ui recording written to", OUT);
}

async function recordGithub() {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: OUT, size: VIEWPORT },
    deviceScaleFactor: 1,
  });
  const page = await ctx.newPage();

  await page.goto("https://github.com/neevmodh/graphsleuth", { waitUntil: "domcontentloaded" });
  await sleep(4000);

  // File tree, then down through the README.
  await smoothScroll(page, 1400, 45, 50);
  await sleep(2500);
  await smoothScroll(page, 2200, 60, 45);
  await sleep(2500);
  await smoothScroll(page, 2200, 60, 45);
  await sleep(2000);

  // The 20 graded answer files.
  await page.goto("https://github.com/neevmodh/graphsleuth/tree/main/cases", { waitUntil: "domcontentloaded" });
  await sleep(4000);
  await smoothScroll(page, 900, 35, 55);
  await sleep(3000);

  await ctx.close();
  await browser.close();
  console.log("github recording written to", OUT);
}

const mode = process.argv[2];
(async () => {
  if (mode === "ui") await recordUI();
  else if (mode === "github") await recordGithub();
  else {
    console.error("usage: node scripts/record_demo.js <ui|github>");
    process.exit(1);
  }
})();
