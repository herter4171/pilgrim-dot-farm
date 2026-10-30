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

const OFF_AIR_503 = {
  status: 503,
  contentType: "application/json",
  body: JSON.stringify({ off_air: true, detail: "station is off the air" }),
};

test.describe("off air while playing", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      // Record every scheduled host source and the audio clock at STOP
      // (stop() closes the AudioContext), so tests can compare the two.
      const w = window as any;
      w.__audioStarts = [];
      const start = AudioBufferSourceNode.prototype.start;
      AudioBufferSourceNode.prototype.start = function (when = 0, offset = 0, duration?: number) {
        w.__audioStarts.push({ when, sfx: !!(this as any)._sfx,
                               end: when + (duration ?? this.buffer!.duration - offset) });
        if (duration === undefined) start.call(this, when, offset);
        else start.call(this, when, offset, duration);
      };
      const close = AudioContext.prototype.close;
      AudioContext.prototype.close = function () {
        w.__ctxTimeAtClose = this.currentTime;
        return close.call(this);
      };
    });
  });

  /* A short outage (well under the 10 s grace) must not end the session: one
     blip, a 404 or a bad decode is not "off air" (SPLIT_PLAN §4). */
  test("a brief program outage does not show OFF AIR", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto("/");
    await page.click("#btn-play");
    await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });

    // 8 s with no program or media (under the 10 s grace): long enough to
    // drain the decoded buffer, so the dry-buffer path is exercised.
    let aborted = 0;
    const cut = (route: any) => { aborted++; return route.abort(); };
    await page.route("**/api/station/program**", cut);
    await page.route("**/api/media/**", cut);
    await page.waitForTimeout(8_000);
    await page.unroute("**/api/station/program**", cut);
    await page.unroute("**/api/media/**", cut);
    expect(aborted, "the outage actually hit program/media fetches").toBeGreaterThan(0);

    await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
    await page.waitForTimeout(3_000);
    await expect(page.locator("#offair")).toBeHidden();
    await expect(page.locator("#btn-play")).toBeEnabled();
    expect(await page.evaluate(() => (window as any).__ctxTimeAtClose)).toBeUndefined();
    expect(errors).toEqual([]);
  });

  /* Definitive off air (edge/gateway 503 {off_air:true}): buffered audio
     plays to its end, then OFF AIR shows and PLAY is disabled. */
  test("buffered audio plays out, then OFF AIR", async ({ page }) => {
    await page.goto("/");
    await page.click("#btn-play");
    await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });

    await page.route("**/api/station/program**", route => route.fulfill(OFF_AIR_503));
    await page.route("**/api/media/**", route => route.fulfill(OFF_AIR_503));
    await page.route("**/api/health", route => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ on_air: false, off_air: true }),
    }));
    // Everything scheduled before the switch must still be allowed to play.
    const bufferedEnd = await page.evaluate(() => Math.max(
      ...(window as any).__audioStarts.filter((s: any) => !s.sfx).map((s: any) => s.end)));

    await expect(page.locator("#offair")).toBeVisible({ timeout: 45_000 });
    await expect(page.locator("#btn-play")).toBeDisabled();
    const closedAt = await page.evaluate(() => (window as any).__ctxTimeAtClose);
    expect(closedAt, "OFF AIR only after the buffered audio ended")
      .toBeGreaterThanOrEqual(bufferedEnd - 0.1);
  });
});
