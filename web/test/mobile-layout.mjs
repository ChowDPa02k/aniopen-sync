import assert from "node:assert/strict";
import { expect } from "@playwright/test";

/** Exercise native touch scrolling. All task fixtures stay in browser responses. */
export async function checkMobileSyncScroll(browser, url, token) {
  const context = await browser.newContext({
    viewport: { width: 393, height: 700 },
    isMobile: true,
    hasTouch: true,
    userAgent:
      "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
  });
  await context.addInitScript((token) => {
    sessionStorage.setItem("aniopen-token", token);
  }, token);
  const page = await context.newPage();
  const errors = [];
  const mutations = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/") && request.method() !== "GET") {
      mutations.push(request.method());
    }
  });
  await page.route("**/api/tasks?**", (route) => route.fulfill({
    json: {
      success: true,
      total: 60,
      data: Array.from({ length: 20 }, (_, index) => ({
        id: index + 1,
        file_name: `Mobile fixture ${index + 1}.mp4`,
        original_name: "mobile.mp4",
        show_title: "Mobile fixture",
        season: 1,
        episode: "01",
        submission: "submitted",
        status: "completed",
        progress: 100,
        downloaded: 1000,
        total: 1000,
        speed: 0,
        downloader: "aria2",
        engine_attempts: [],
      })),
    },
  }));
  try {
    await page.goto(url);
    await page.getByText("Mobile fixture 1.mp4", { exact: true }).waitFor();
    const body = page.locator(".tasks-table-viewport .ant-table-content").first();
    const pager = page.locator(".tasks-table-viewport .ant-pagination");
    const cdp = await context.newCDPSession(page);
    // Dispatch actual touch points: synthesized scroll gestures vary across host platforms.
    async function swipe(x, y, dx, dy) {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y }] });
      for (let step = 1; step <= 24; step++) {
        const progress = 1 - (1 - step / 24) ** 2;
        await cdp.send("Input.dispatchTouchEvent", {
          type: "touchMove",
          touchPoints: [{ x: x + dx * progress, y: y + dy * progress }],
        });
        await new Promise((resolve) => setTimeout(resolve, 16));
      }
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    }
    for (const viewport of [
      { width: 360, height: 640 },
      { width: 393, height: 700 },
      { width: 430, height: 780 },
      { width: 844, height: 390 },
    ]) {
      await page.setViewportSize(viewport);
      await page.evaluate(() => window.scrollTo(0, 0));
      await expect.poll(() => body.evaluate((node) => node.clientHeight)).toBeGreaterThanOrEqual(280);
      await expect.poll(() => page.evaluate(() =>
        document.scrollingElement.scrollHeight > innerHeight &&
        document.scrollingElement.scrollWidth <= innerWidth + 1,
      )).toBe(true);
      for (let attempt = 0; attempt < 16; attempt++) {
        await swipe(viewport.width / 2, viewport.height - 100, 0, 200 - viewport.height);
        if (await pager.evaluate((node) => node.getBoundingClientRect().bottom <= innerHeight)) break;
      }
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
      await expect.poll(() => pager.evaluate((node) =>
        node.getBoundingClientRect().bottom <= innerHeight,
      )).toBe(true);
      await body.evaluate((node) => { node.scrollLeft = 0; });
      const box = await body.boundingBox();
      const point = {
        x: Math.min(box.x + box.width - 24, viewport.width - 24),
        y: Math.min(Math.max(box.y + 180, 100), viewport.height - 100),
      };
      await swipe(point.x, point.y, -Math.min(300, point.x - 20), 0);
      await expect.poll(() => body.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
      // Vertical swipes over the rows must scroll the page in both directions.
      const before = await page.evaluate(() => window.scrollY);
      await swipe(point.x, point.y, 0, Math.min(250, viewport.height - point.y - 20));
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(before);
      const up = await page.evaluate(() => window.scrollY);
      await swipe(point.x, point.y, 0, -Math.min(250, point.y - 80));
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(up);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(mutations, []);
  } finally {
    await context.close();
  }
}
