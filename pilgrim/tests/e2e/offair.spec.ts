import { expect, test } from "@playwright/test";

/* SPLIT_PLAN §4 / RADIO.md §9.1, §11: when the edge cannot reach the origin
   it answers /api/health with on_air:false and other /api/* with 503
   {off_air:true}. The page must show OFF AIR, disable PLAY, and close the
   request line cleanly (no uncaught error, no hang). */
test("edge off-air: OFF AIR shown, PLAY disabled, requests closed", async ({ page }) => {
  await page.route("**/api/health", route =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ on_air: false, off_air: true }),
    }));

  await page.route("**/api/requests", async route => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ off_air: true, detail: "station is off the air" }),
      });
    } else {
      // GET keeps the last board; a fresh page has nothing yet.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ queue: [], recent: [], cap: 10 }),
      });
    }
  });

  await page.goto("/");

  await expect(page.locator("#offair")).toBeVisible();
  await expect(page.locator("#btn-play")).toBeDisabled();

  await page.fill("#req-input", "play something");
  await page.click("#req-send");
  await expect(page.locator("#req-err")).toHaveText(/off the air/i);
});
