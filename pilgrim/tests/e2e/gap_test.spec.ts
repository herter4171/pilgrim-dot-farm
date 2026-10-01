/** Backend-free playback regressions, RADIO.md §§9.2 and 15. */
import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const starts: { when: number; end: number; sampleRate: number; context: AudioContext;
                    sfx: boolean; stop?: number }[] = [];
    (window as any).__audioStarts = starts;
    const start = AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start = function (when = 0, offset = 0, duration?: number) {
      starts.push({ when, end: when + (duration ?? this.buffer!.duration - offset),
                    sampleRate: this.context.sampleRate, context: this.context as AudioContext,
                    sfx: !!(this as any)._sfx });
      (this as any).__rec = starts[starts.length - 1];
      if (duration === undefined) start.call(this, when, offset);
      else start.call(this, when, offset, duration);
    };
    const stop = AudioBufferSourceNode.prototype.stop;
    AudioBufferSourceNode.prototype.stop = function (when = 0) {
      if ((this as any).__rec) (this as any).__rec.stop = when;
      stop.call(this, when);
    };
  });
});

test("PLAY continues past three clips with sample-contiguous joins", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto("/");
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  await page.waitForTimeout(25_000);

  const all = await page.evaluate(() => (window as any).__audioStarts.map((s: any) => ({
    when: s.when, end: s.end, sampleRate: s.sampleRate, sfx: s.sfx, stop: s.stop,
  })));
  // SFX overlays ride on top of a host; the joins are between hosts only
  const starts = all.filter((s: any) => !s.sfx);
  expect(starts.length, "schedules beyond the initial three decoded items").toBeGreaterThan(8);
  for (let i = 1; i < starts.length; i++) {
    expect(Math.abs(starts[i].when - starts[i - 1].end), "join within one sample")
      .toBeLessThanOrEqual(1 / starts[i].sampleRate);
  }
  // every overlay starts inside a host and is stopped by that host's end (SFX.md §7.4)
  const overlays = all.filter((s: any) => s.sfx);
  expect(overlays.length, "talk clips carried SFX overlays").toBeGreaterThan(0);
  for (const o of overlays) {
    const host = starts.find((h: any) => h.when <= o.when && o.when < h.end);
    expect(host, "overlay starts inside a host clip").toBeTruthy();
    expect(o.stop, "overlay is stopped at its host's end").toBeLessThanOrEqual(host.end + 1e-6);
  }
  const report = await page.request.get("/api/test/report").then(r => r.json());
  expect(report.underrun).toBe(0);
  expect(report.seqs.length).toBeGreaterThan(8);
  for (let i = 1; i < report.seqs.length; i++) {
    expect(report.seqs[i]).toBe(report.seqs[i - 1] + 1);
  }
  expect(report.coverage_s).toBeGreaterThanOrEqual(300);
  expect(errors).toEqual([]);
});

test("health failures do not stop committed audio", async ({ page }) => {
  await page.goto("/");
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
  await page.route("**/api/health", route => route.fulfill({
    json: { on_air: false, backends: {}, inventory: {}, committed_coverage_s: 600 },
  }));
  await page.waitForTimeout(9_000);
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
  await expect(page.locator("#btn-play")).toBeEnabled();
  await expect(page.locator("#btn-play")).toContainText("STOP");
  expect(await page.evaluate(() => (window as any).__audioStarts.length)).toBeGreaterThan(3);
  await page.route("**/api/health", route => route.abort());
  await page.waitForTimeout(9_000);
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
});

test("STOP during a pending decode cannot schedule into the next session", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  let release!: () => void;
  let requested!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const pending = new Promise<void>(resolve => { requested = resolve; });
  let first = true;
  await page.route("**/api/media/*", async route => {
    if (first) {
      first = false;
      requested();
      await held;
    }
    await route.continue();
  });
  await page.goto("/");
  await page.click("#btn-play");
  await pending;
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/idle/);
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
  release();
  await page.waitForTimeout(8_000);
  await expect(page.locator("#indicator")).toHaveClass(/onair/);
  const contexts = await page.evaluate(() => new Set(
    (window as any).__audioStarts.map((s: any) => s.context),
  ).size);
  expect(contexts).toBe(1);
  expect(errors).toEqual([]);
});
