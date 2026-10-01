import { test } from "@playwright/test";
test("inspect panes", async ({ page }) => {
  await page.goto("/", { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  const info = await page.evaluate(() => ({
    wins: [...document.querySelectorAll(".window")].map(w => {
      const bar = w.querySelector(".win-bar");
      const body = w.querySelector(".win-body");
      return {
        id: w.getAttribute("data-win"),
        winH: Math.round(w.getBoundingClientRect().height),
        barH: bar ? Math.round(bar.getBoundingClientRect().height) : null,
        bodyH: body ? Math.round(body.getBoundingClientRect().height) : null,
        bodyScrollH: body ? body.scrollHeight : null,
        bodyChildren: body ? body.children.length : null,
      };
    }),
    visitors: (document.getElementById("visitors")||{}).textContent,
  }));
  console.log("INSPECT " + JSON.stringify(info));
  await page.screenshot({ path: "/tmp/radio_page.png", fullPage: true });
});
