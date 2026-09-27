/**
 * PILGRIM DOT FARM — client gap test (RADIO.md §15, M9).
 *
 * Joins the committed program in a headed browser and proves gapless,
 * underrun-free playout. The client reports progress via heartbeats and the
 * server records airplay + underrun events; the report endpoint exposes them.
 *
 * Assertions derivable from heartbeats:
 *   - zero underrun events (client never had to fall back)
 *   - on-air seqs are strictly increasing (no overlap / rewind / skip-back)
 *   - committed coverage stayed at/above the lookahead target
 */
import { test, expect } from "@playwright/test";

test("PLAY joins the program without gaps or underruns", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#btn-play")).toBeVisible();

  await page.click("#btn-play");
  // wait for the on-air indicator, proving the AudioContext started
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });

  // Let several short items air and heartbeats accumulate.
  await page.waitForTimeout(25_000);

  const report = await page.request
    .get("/api/test/report")
    .then((r) => r.json());

  expect(report.underrun, "no underrun events").toBe(0);

  const seqs = report.seqs as number[];
  expect(seqs.length, "heartbeats were recorded").toBeGreaterThan(2);
  for (let i = 1; i < seqs.length; i++) {
    expect(seqs[i], `seq[${i}] strictly increasing`).toBeGreaterThan(seqs[i - 1]);
  }

  expect(report.coverage_s, "committed window stays filled").toBeGreaterThanOrEqual(300);

  // Verify playback actually progressed (on-air seq advanced past the start).
  expect(seqs[seqs.length - 1]).toBeGreaterThan(seqs[0]);
});
