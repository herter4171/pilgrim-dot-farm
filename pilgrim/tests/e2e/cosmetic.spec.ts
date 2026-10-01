/** Cosmetic page tests (COSMETIC_PATCHING). Backend-free; the e2e server seeds
 *  a temp library with fake FLAC + song titles. Verifies pane order/labels,
 *  the song label under the meter, the three-row recent history, the model
 *  credits + hit counter panes, and that a failing visitor counter never
 *  disturbs PLAY or the request line. */
import { test, expect } from "@playwright/test";

test("page layout: pane order, ON AIR title, credits + hit counter", async ({ page }) => {
  await page.goto("/");
  const order = await page.$$eval(".window", els => els.map(e => e.getAttribute("data-win")));
  expect(order).toEqual(["radio", "requests", "credits", "hit"]);
  expect(await page.textContent('[data-win="radio"] .win-title')).toBe("ON AIR");
  expect(await page.$('[data-win="credits"]')).toBeTruthy();
  expect(await page.$('[data-win="hit"]')).toBeTruthy();
  // five documented models + Kokoro = six credit rows
  expect(await page.$$eval(".credits-list li", els => els.length)).toBe(6);
  expect(await page.textContent(".visitors")).toMatch(/^Unique visitors:/);
});

test("song label shows the audible track, not a buffered next one; STOP clears it", async ({ page }) => {
  await page.goto("/");
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  // wait for an actual song to go on air (label driven by the audio clock)
  await page.waitForFunction(
    () => (document.getElementById("song-label") || {}).textContent?.length > 0,
    null, { timeout: 20_000 });
  const label = await page.textContent("#song-label");
  expect(label).toMatch(/^Song \d+ - The Fakes$/);
  // label is set via textContent (safe rendering) and never shows 'undefined'/'null'
  expect(label).not.toMatch(/undefined|null/);
  // STOP clears the label (also covers cross-tab STOP path)
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/idle/);
  expect(await page.textContent("#song-label")).toBe("");
});

test("recently-played shows at most three even when the server returns five", async ({ page }) => {
  await page.goto("/");
  await page.fill("#req-input", "play the synthesizer, Liam");
  await page.click("#req-send");
  // POST response carries five recents; the client defensive-slices to three
  await expect(page.locator(".req-recent-item")).toHaveCount(3, { timeout: 10_000 });
});

test("a failed visitor counter never breaks PLAY or request submission", async ({ page }) => {
  await page.route("**/api/visitors", route => route.fulfill({
    status: 503, contentType: "application/json",
    body: JSON.stringify({ detail: "unavailable" }) }));
  await page.goto("/");
  // counter shows '—', not a fabricated zero
  await expect(page.locator("#visitors")).toHaveText("Unique visitors: —");
  // PLAY still works
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  await expect(page.locator("#btn-play")).toContainText("STOP");
  // request submission still works
  await page.fill("#req-input", "another tune, please");
  await page.click("#req-send");
  await expect(page.locator(".req-recent-item")).toHaveCount(3, { timeout: 10_000 });
});

test("all panes stay reachable by scrolling on a small screen", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 420 });
  await page.goto("/");
  const desktop = page.locator("#desktop");
  await desktop.evaluate(el => el.scrollTo(0, el.scrollHeight));
  await page.waitForTimeout(300);
  const hit = page.locator('[data-win="hit"]');
  await hit.scrollIntoViewIfNeeded();
  await expect(hit).toBeVisible();
  await expect(page.locator('[data-win="credits"]')).toBeVisible();
});
