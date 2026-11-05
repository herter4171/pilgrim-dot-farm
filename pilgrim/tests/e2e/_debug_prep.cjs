/* Throwaway: inspect the aired table Prep Time column at 360 px. */
const { chromium } = require("@playwright/test");
const { spawn } = require("node:child_process");
const { existsSync } = require("node:fs");
const { resolve } = require("node:path");
const root = process.cwd().replace(/\/pilgrim\/tests\/e2e$/, "");
const CHROME_PATHS = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/opt/google/chrome/chrome",
];
const channel = CHROME_PATHS.some((p) => existsSync(p)) ? "chrome" : "chromium";

(async () => {
  // free port
  const net = require("node:net");
  const port = await new Promise((res) => {
    const s = net.createServer();
    s.listen(0, () => { const p = s.address().port; s.close(() => res(p)); });
  });
  const srv = spawn(`${root}/.venv/bin/python`,
    ["-m", "uvicorn", "pilgrim.tests.e2e.e2e_server:app",
     "--host", "127.0.0.1", "--port", String(port)], { cwd: root });
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 60; i++) {
    try { const r = await fetch(base + "/api/health"); if (r.ok) break; } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  const browser = await chromium.launch({ headless: true, channel });
  const page = await browser.newPage({ viewport: { width: 360, height: 420 } });
  await page.goto(base + "/");
  await page.waitForSelector(".req-recent-item");
  await page.waitForTimeout(500);
  const info = await page.evaluate(() => {
    const tables = Array.from(document.querySelectorAll(".req-table"));
    const table = tables.find(t => t.querySelector("#req-recent"));
    const ths = Array.from(table.querySelectorAll("thead th"));
    const time = ths[2];
    const s = getComputedStyle(time);
    const probe = document.createElement("span");
    probe.style.cssText = `position:absolute;visibility:hidden;font:${s.font};`;
    probe.textContent = "0";
    document.body.appendChild(probe);
    const ch = probe.getBoundingClientRect().width;
    probe.remove();
    const bodyLh = getComputedStyle(document.body).lineHeight;
    return {
      thFont: s.font, thLineHeight: s.lineHeight, thWidth: time.getBoundingClientRect().width,
      ch, bodyLh,
      cells: Array.from(table.querySelectorAll("td.req-wait")).map((c) => {
        const cs = getComputedStyle(c);
        const txt = c.textContent || "";
        const i = txt.indexOf("min");
        let minRect = null, firstRect = null;
        if (i >= 0 && c.firstChild) {
          const r = document.createRange();
          r.setStart(c.firstChild, i); r.setEnd(c.firstChild, i + 3);
          minRect = r.getBoundingClientRect();
        }
        firstRect = c.getBoundingClientRect();
        return {
          text: txt, clientWidth: c.clientWidth, scrollWidth: c.scrollWidth,
          lineHeight: cs.lineHeight,
          minRect: minRect && { w: minRect.width, h: minRect.height },
          cellRect: { w: firstRect.width, h: firstRect.height },
        };
      }),
    };
  });
  console.log(JSON.stringify(info, null, 2));
  await browser.close();
  srv.kill();
  process.exit(0);
})();
