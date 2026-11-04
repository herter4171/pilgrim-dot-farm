import { expect, test, type Page } from "@playwright/test";

const VOTER = "0123456789abcdef0123456789abcdef";

/* VOTING.md Step 7: ratings + queue votes through the real client and the real
   e2e board. The e2e server seeds 12 free requests + 1 paid and 5 aired. */
test.describe.configure({ mode: "serial" });

async function state(page: Page) {
  const r = await page.request.get("/api/station/state");
  expect(r.ok()).toBeTruthy();
  return r.json();
}

async function waitForSong(page: Page, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const st = await state(page);
    if (st.current_type === "song" && st.rating && st.rating.open) {
      return { epoch: st.epoch as string, seq: st.current_seq as number };
    }
    await page.waitForTimeout(250);
  }
  throw new Error("no song was on air in time");
}

function announce(page: Page, airing: { epoch: string; seq: number } | null) {
  return page.evaluate((detail) => {
    document.dispatchEvent(new CustomEvent("pd:nowplaying", { detail }));
  }, airing ? { ...airing, media_id: 1, title: "T", artist: "A" } : null);
}

test("rating row is disabled before PLAY, enabled while a song is audible, STOP closes it", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#rate-row")).toHaveAttribute("aria-disabled", "true");
  await page.click("#btn-play");
  await expect(page.locator("#song-label")).not.toHaveText("", { timeout: 15000 });
  await expect(page.locator("#rate-row")).toHaveAttribute("aria-disabled", "false");
  await page.click("#btn-play"); // STOP
  await expect(page.locator("#rate-row")).toHaveAttribute("aria-disabled", "true");
  await expect(page.locator("#song-label")).toHaveText("");
});

test("a turkey click posts, fills the row, and shows the aggregate", async ({ page }) => {
  await page.goto("/");
  const airing = await waitForSong(page);
  await announce(page, airing);
  await expect(page.locator("#rate-row")).toHaveAttribute("aria-disabled", "false");
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes("/api/station/rating")),
    page.click(".turkey[data-stars='4']"),
  ]);
  expect(resp.status()).toBe(200);
  const body = await resp.json();
  expect(body.mine).toBe(4);
  expect(body.count).toBe(1);
  expect(body.avg).toBe(4);
  await expect(page.locator(".turkey[data-stars='4']")).toHaveClass(/filled/);
  await expect(page.locator("#rate-agg")).toHaveText("4.0 · 1", { timeout: 2000 });
  // the response is never cached
  expect(resp.headers()["cache-control"]).toBe("no-store");
});

test("a stored rating is restored for the same airing after a reload", async ({ page }) => {
  // The e2e songs are ~2 s: retry until a click lands inside the window, then
  // the rating is in localStorage and the reload must restore it.
  let airing: { epoch: string; seq: number } | null = null;
  let rated = false;
  for (let attempt = 0; attempt < 6 && !rated; attempt++) {
    await page.goto("/");
    airing = await waitForSong(page);
    await announce(page, airing);
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/station/rating")),
      page.click(".turkey[data-stars='5']"),
    ]);
    rated = resp.status() === 200;
    if (rated) {
      await expect(page.locator(".turkey[data-stars='5']")).toHaveClass(/filled/);
    }
  }
  expect(rated, "no click landed inside a rating window").toBeTruthy();
  await page.reload();
  // The restore happens synchronously inside the event handler, before the
  // state poll can close a stale airing: assert it in the same evaluate.
  const restored = await page.evaluate((detail) => {
    document.dispatchEvent(new CustomEvent("pd:nowplaying", { detail }));
    return document.querySelector(".turkey[data-stars='5']")
      .classList.contains("filled");
  }, { ...airing, media_id: 1, title: "T", artist: "A" });
  expect(restored).toBe(true);
});

test("the server closes the window after the song ends", async ({ page }) => {
  await page.goto("/");
  const airing = await waitForSong(page);
  // Wait for the song to end (the on-air row changes), then probe the old seq:
  // it stays ratable through ratings.grace_s, and must 409 once that closes.
  const deadline = Date.now() + 15000;
  let changed = false;
  while (Date.now() < deadline) {
    const st = await state(page);
    if (st.current_seq !== airing.seq) { changed = true; break; }
    await page.waitForTimeout(150);
  }
  expect(changed).toBeTruthy();
  let status = 0;
  let body: { code?: string } = {};
  while (Date.now() < deadline) {
    const r = await page.request.post("/api/station/rating", {
      data: { voter_id: VOTER, epoch: airing.epoch, seq: airing.seq, stars: 3 },
    });
    status = r.status();
    if (status === 409) { body = await r.json(); break; }
    await page.waitForTimeout(250);
  }
  expect(status).toBe(409);
  expect(body.code).toBe("rating_closed");
  // the control is gone once the player announces the next item / nothing
  await announce(page, null);
  await expect(page.locator("#rate-row")).toHaveAttribute("aria-disabled", "true");
});

test("queue votes: up, retract, down; the paid row has no buttons", async ({ page }) => {
  await page.goto("/");
  const row = page.locator(".req-item").filter({ hasText: "e2e free request 7" });
  await expect(row).toHaveCount(1);
  const paid = page.locator(".req-item").filter({ hasText: "e2e paid request" });
  await expect(paid).toHaveCount(1);
  await expect(paid.locator(".vote button")).toHaveCount(0);

  const [up] = await Promise.all([
    page.waitForResponse((r) => r.url().includes("/vote")),
    row.locator(".vote-up").click(),
  ]);
  expect(up.status()).toBe(200);
  expect((await up.json()).score).toBe(1);
  expect(up.headers()["cache-control"]).toBe("no-store");
  await expect(row.locator(".vote-score")).toHaveText("2");   // base 1 + upvote
  await expect(row.locator(".vote-up")).toHaveAttribute("aria-pressed", "true");

  await row.locator(".vote-up").click();   // second click retracts
  await expect(row.locator(".vote-score")).toHaveText("1");   // back to the base vote
  await expect(row.locator(".vote-up")).toHaveAttribute("aria-pressed", "false");

  await row.locator(".vote-down").click();
  await expect(row.locator(".vote-score")).toHaveText("0");   // base 1 - 1
  await expect(row.locator(".vote-down")).toHaveAttribute("aria-pressed", "true");

  // the pressed state survives a reload (localStorage), and the board score too
  await page.reload();
  const again = page.locator(".req-item").filter({ hasText: "e2e free request 7" });
  await expect(again.locator(".vote-down")).toHaveAttribute("aria-pressed", "true");
  // The board is cached for 2 s at the edge and polled every 6 s: the score
  // catches up on the next poll (the pressed state comes from localStorage).
  await expect(again.locator(".vote-score")).toHaveText("0", { timeout: 12000 });
});

test("an aired request leaves the queue and loses its buttons", async ({ page }) => {
  await page.goto("/");
  const row = page.locator(".req-item").filter({ hasText: "e2e free request 12" });
  await expect(row).toHaveCount(1);
  const id = await row.locator(".vote").getAttribute("data-id");
  expect(id).toBeTruthy();
  const r = await page.request.post(`/api/test/air/${id}`);
  expect(r.ok()).toBeTruthy();
  // the board polls every 6 s: the row is gone from NOW QUEUED...
  await expect(page.locator(".req-item").filter({ hasText: "e2e free request 12" }))
    .toHaveCount(0, { timeout: 10000 });
  // ...and shows up under AIRED REQUESTS
  await expect(page.locator("#req-recent").getByText("e2e free request 12"))
    .toHaveCount(1, { timeout: 10000 });
});

test("the queue box scrolls with a visible cue", async ({ page }) => {
  await page.goto("/");
  const box = page.locator(".req-scroll");
  await expect(box).toHaveClass(/has-more/);
  const scrolls = await box.evaluate((el) => el.scrollHeight > el.clientHeight + 4);
  expect(scrolls).toBeTruthy();
  await box.evaluate((el) => el.scrollTo(0, el.scrollHeight));
  await expect(box).not.toHaveClass(/has-more/);
});

test("mobile: no horizontal scroll, 40px vote targets, status under the title", async ({ page }) => {
  for (const width of [360, 320]) {
    await page.setViewportSize({ width, height: 640 });
    await page.goto("/");
    await expect(page.locator(".req-item").first()).toBeVisible();
    const noOverflow = await page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth);
    expect(noOverflow, `horizontal scroll at ${width}px`).toBeTruthy();
    const up = page.locator(".req-item .vote-up").first();
    const box = await up.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(40);
    expect(box!.height).toBeGreaterThanOrEqual(40);
    // the Status column is hidden and its text sits under the title
    await expect(page.locator(".req-item .req-status").first()).toBeHidden();
    await expect(page.locator(".req-item .req-status-inline").first()).toBeVisible();
    // the rating row never overflows its art width
    const rate = page.locator("#rate-row");
    const rw = (await rate.boundingBox())!.width;
    const art = (await page.locator(".art-frame").boundingBox())!.width;
    expect(rw).toBeLessThanOrEqual(art + 1);
    for (const t of await page.locator(".turkey").all()) {
      const b = await t.boundingBox();
      expect(b!.width).toBeGreaterThanOrEqual(40);
      expect(b!.height).toBeGreaterThanOrEqual(40);
    }
  }
});
