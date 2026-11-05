/** Cosmetic page tests (COSMETIC_PATCHING). Backend-free; the e2e server seeds
 *  a temp library with fake FLAC + song titles. Verifies pane order/labels,
 *  the song label under the meter, the three-row recent history, the model
 *  credits, hit counter, and infra/data-flow panes, and that a failing visitor counter never
 *  disturbs PLAY or the request line. */
import { test, expect } from "@playwright/test";

test("page layout: pane order, RADIO BROADCAST title, credits + hit counter", async ({ page }) => {
  await page.goto("/");
  const order = await page.$$eval(".window", els => els.map(e => e.getAttribute("data-win")));
  expect(order).toEqual(["radio", "requests", "hit", "motivation", "infra", "credits"]);
  expect(await page.textContent('[data-win="radio"] .win-title')).toBe("RADIO BROADCAST");
  expect(await page.$('[data-win="credits"]')).toBeTruthy();
  expect(await page.$('[data-win="hit"]')).toBeTruthy();
  expect(await page.textContent('[data-win="motivation"] .win-title')).toBe("MOTIVATION");
  expect(await page.textContent('[data-win="infra"] .win-title')).toBe("INFRA AND DATA FLOW");
  // six documented models + Kokoro-82M + the DeepSeek dev model = seven credit
  // rows, plus the asset credit
  expect(await page.$$eval(".credits-list li", els => els.length)).toBe(8);
  expect(await page.textContent('[data-win="credits"] .win-title')).toBe("CREDITS");
  const attr = await page.$eval(".cred-attr a", a => [a.textContent, a.getAttribute("href")]);
  expect(attr).toEqual(["Cornucopia LQ", "https://commons.wikimedia.org/wiki/File:Cornucopia_LQ.svg"]);
  // every model links to its HF model card in a new tab
  const links = await page.$$eval(".credits-list a.cred-model",
    els => els.map(a => [a.textContent, a.getAttribute("href"), a.getAttribute("target")]));
  expect(links).toHaveLength(7);
  expect(links[0]).toEqual(["Qwen 3.8 27B", "https://huggingface.co/Qwen/Qwen3.8-27B", "_blank"]);
  expect(links[5]).toEqual(["Kokoro-82M", "https://huggingface.co/hexgrad/Kokoro-82M", "_blank"]);
  for (const [, href] of links) expect(href).toMatch(/^https:\/\/huggingface\.co\//);
  expect(await page.textContent(".visitors")).toMatch(/^Unique visitors:/);
  // HIT COUNTER above it: DB-backed request counts (RADIO §9.1). The e2e
  // server seeds 5 aired + 12 free + 1 paid = 18 rows, one paid.
  await expect(page.locator("#paid-requests")).toHaveText("Paid requests: 1");
  await expect(page.locator("#total-requests")).toHaveText("Total request: 18");
  // the cornucopia marks a paid request: one icon at either end of that line
  const paidIcons = page.locator(".paid-counter .paid-counter-icon");
  await expect(paidIcons).toHaveCount(2);
  for (const icon of await paidIcons.all()) {
    await icon.scrollIntoViewIfNeeded();
    await expect.poll(
      () => icon.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0),
      { timeout: 10_000 }).toBe(true);
    expect(await icon.getAttribute("src")).toBe("img/cornucopia.png");
    // decorative: the text already says "Paid requests"
    expect(await icon.getAttribute("alt")).toBe("");
  }
  // icons and text share one line height: each icon's vertical centre sits
  // within the text's box (same height at either end)
  const textBox = await page.locator("#paid-requests").boundingBox();
  for (const icon of await paidIcons.all()) {
    const box = await icon.boundingBox();
    const centre = box!.y + box!.height / 2;
    expect(centre).toBeGreaterThan(textBox!.y);
    expect(centre).toBeLessThan(textBox!.y + textBox!.height);
  }
  await expect(page.locator("#listeners")).toHaveText("Listening now: 68.10");
  // the hit-counter pane carries the social links as icons (cosmetic patch):
  // Instagram / Reddit / GitHub, each an SVG glyph in a new tab
  const socials = await page.$$eval(".social-row a", els => els.map(a => {
    const img = a.querySelector("img") as HTMLImageElement | null;
    return [a.getAttribute("href"), a.getAttribute("target"), a.getAttribute("rel"),
            img ? img.getAttribute("src") : null, img ? img.getAttribute("alt") : null];
  }));
  expect(socials).toEqual([
    ["https://www.instagram.com/pilgrim.farm/", "_blank", "noopener noreferrer",
     "img/instagram.svg", "Instagram"],
    ["https://www.reddit.com/r/pilgrimdotfarm", "_blank", "noopener noreferrer",
     "img/reddit.svg", "Reddit"],
    ["https://github.com/pilgrim-dot-farm/pilgrim-dot-farm", "_blank", "noopener noreferrer",
     "img/github.svg", "GitHub"],
  ]);
  // the old text links are gone
  expect(await page.$$(".hit-link")).toHaveLength(0);
  // every icon is an SVG that actually loads, rendered 40-48 px wide
  for (const img of await page.locator(".social-row img").all()) {
    await img.scrollIntoViewIfNeeded();
    await expect.poll(
      () => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0),
      { timeout: 10_000 }).toBe(true);
    const box = await img.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(40);
    expect(box!.width).toBeLessThanOrEqual(48);
    const src = await img.getAttribute("src");
    const r = await page.request.get("/" + src);
    expect(r.ok()).toBeTruthy();
    expect(r.headers()["content-type"]).toContain("image/svg+xml");
  }
  // the infra pane shows infra.png as content, linked to the full-size image
  const infra = await page.$eval('[data-win="infra"] .infra-link img.infra-img',
    img => [img.getAttribute('src'), img.getAttribute('alt')]);
  expect(infra[0]).toBe("img/infra.png");
  expect(infra[1]).toMatch(/infrastructure and data flow/i);
  const infraLink = await page.$eval('[data-win="infra"] .infra-link',
    a => [a.getAttribute("href"), a.getAttribute("target"), a.getAttribute("rel")]);
  expect(infraLink).toEqual(["img/infra.png", "_blank", "noopener noreferrer"]);
  // and the asset actually loads (not a 404 placeholder)
  // (loading="lazy" and below the fold since MOTIVATION: scroll to it first)
  await page.locator('[data-win="infra"] .infra-img').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => {
    const img = document.querySelector('[data-win="infra"] .infra-img') as HTMLImageElement | null;
    return !!img && img.complete && img.naturalWidth > 0;
  }, null, { timeout: 10_000 });
  const loaded = await page.$eval('[data-win="infra"] .infra-img', img => img.complete && img.naturalWidth > 0);
  expect(loaded).toBe(true);
  // COSMETIC_PATCHING §7: the diagram is scaled to the pane's INTERIOR width
  // (pane width minus borders and .win-body padding), never its intrinsic
  // 1024px (which .window{overflow:hidden} would clip).
  const box = await page.$eval('[data-win="infra"] .infra-img', img => {
    const r = img.getBoundingClientRect();
    const body = img.closest('.win-body') as HTMLElement;
    const cs = getComputedStyle(body);
    const interior = body.clientWidth
      - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    return { imgW: r.width, imgH: r.height, interior, natW: img.naturalWidth,
             natH: img.naturalHeight, contentW: img.clientWidth, contentH: img.clientHeight };
  });
  // border-box width equals the pane's interior width (border-box sizing is global)
  expect(Math.abs(box.imgW - box.interior)).toBeLessThan(1);
  expect(box.imgW).toBeLessThan(box.natW);
  // height follows the width, preserving the diagram's aspect ratio (content box)
  expect(Math.abs(box.contentH - box.contentW * (box.natH / box.natW))).toBeLessThan(1);
});

test("song label shows the audible track, not a buffered next one; STOP clears it", async ({ page }) => {
  await page.goto("/");
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  // wait for an actual song to go on air (label driven by the audio clock)
  await page.waitForFunction(
    () => (document.getElementById("song-label") || {}).textContent?.length > 0,
    null, { timeout: 20_000 });
  // title on its own line, artist below it (RADIO §9.1)
  expect(await page.textContent("#song-title")).toMatch(/^Song \d+$/);
  expect(await page.textContent("#song-artist")).toBe("The Fakes");
  const label = await page.textContent("#song-label");
  // label is set via textContent (safe rendering) and never shows 'undefined'/'null'
  expect(label).not.toMatch(/undefined|null|-/);
  const [titleFs, artistFs] = await page.evaluate(() => [
    parseFloat(getComputedStyle(document.getElementById("song-title")!).fontSize),
    parseFloat(getComputedStyle(document.getElementById("song-artist")!).fontSize)]);
  expect(titleFs - artistFs).toBe(4);
  // STOP clears the label (also covers cross-tab STOP path)
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/idle/);
  expect(await page.textContent("#song-label")).toBe("");
});

test("aired requests table shows up to five with a prep time", async ({ page }) => {
  await page.goto("/");
  await page.fill("#req-input", "play the synthesizer, Liam");
  await page.click("#req-send");
  await expect(page.locator(".req-recent-item")).toHaveCount(5, { timeout: 10_000 });
  const waits = await page.$$eval(".req-wait", els => els.map(e => e.textContent));
  expect(waits.length).toBeGreaterThan(0);
  expect(waits[0]).toMatch(/^\d+ (min|h|d)/);   // column is headed "Prep Time"
  // an unmeasured row (wait_s null) reads as an em dash, not a blank cell
  expect(waits[4]).toBe("—");
  const airedTable = page.locator(".req-table").filter({ has: page.locator("#req-recent") });
  await expect(airedTable.getByRole("columnheader")).toHaveText(["Prompt", "Title", "Prep Time"]);
  // Prompt and Title are the same width: each half of the space left over
  // after Prep Time's fixed slice (cosmetic parity request)
  const widths = await airedTable.evaluate((table) => {
    const ths = Array.from(table.querySelectorAll("thead th"));
    const px = (th) => th.getBoundingClientRect().width;
    return { prompt: px(ths[0]), title: px(ths[1]), time: px(ths[2]), total: table.getBoundingClientRect().width };
  });
  expect(Math.abs(widths.prompt - widths.title)).toBeLessThanOrEqual(1);
  expect(widths.prompt / widths.total).toBeCloseTo(0.43, 1);
  // the queue's vote column is left-most (cosmetic patch)
  const queueTable = page.locator(".req-table").filter({ has: page.locator("#req-queue") });
  await expect(queueTable.getByRole("columnheader"))
    .toHaveText(["Votes", "Request", "Title", "Status"]);
  expect(await page.$eval("#req-queue tr.req-item td:first-child",
    td => td.classList.contains("req-vote"))).toBe(true);
  // the queue table's header row stays pinned while its box scrolls
  const pos = await page.$eval(".req-scroll thead th",
                               el => getComputedStyle(el).position);
  expect(pos).toBe("sticky");
});

test("a failed visitor counter never breaks PLAY or request submission", async ({ page }) => {
  await page.route("**/api/visitors", route => route.fulfill({
    status: 503, contentType: "application/json",
    body: JSON.stringify({ detail: "unavailable" }) }));
  await page.goto("/");
  // counter falls back to 0, never a hyphen
  await expect(page.locator("#visitors")).toHaveText("Unique visitors: 0");
  await expect(page.locator("#paid-requests")).toHaveText("Paid requests: 0");
  await expect(page.locator("#total-requests")).toHaveText("Total request: 0");
  // PLAY still works
  await page.click("#btn-play");
  await expect(page.locator("#indicator")).toHaveClass(/onair/, { timeout: 15_000 });
  await expect(page.locator("#btn-play")).toContainText("STOP");
  // request submission still works
  await page.fill("#req-input", "another tune, please");
  await page.click("#req-send");
  await expect(page.locator(".req-recent-item")).toHaveCount(5, { timeout: 10_000 });
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
