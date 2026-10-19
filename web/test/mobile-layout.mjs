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
      for (let swipe = 0; swipe < 4; swipe++) {
        await cdp.send("Input.synthesizeScrollGesture", {
          x: viewport.width / 2,
          y: viewport.height - 100,
          yDistance: -1500,
          gestureSourceType: "touch",
          speed: 2000,
        });
        if (await pager.evaluate((node) => node.getBoundingClientRect().bottom <= innerHeight)) break;
      }
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
      await expect.poll(() => pager.evaluate((node) =>
        node.getBoundingClientRect().bottom <= innerHeight,
      )).toBe(true);
      await body.evaluate((node) => { node.scrollLeft = 0; });
      const box = await body.boundingBox();
      const point = {
        x: Math.min(box.x + 120, viewport.width - 40),
        y: Math.min(Math.max(box.y + 180, 100), viewport.height - 100),
        gestureSourceType: "touch",
      };
      await cdp.send("Input.synthesizeScrollGesture", { ...point, xDistance: -350 });
      await expect.poll(() => body.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);
      // Vertical swipes over the rows must scroll the page in both directions.
      const before = await page.evaluate(() => window.scrollY);
      await cdp.send("Input.synthesizeScrollGesture", { ...point, yDistance: 350 });
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeLessThan(before);
      const up = await page.evaluate(() => window.scrollY);
      await cdp.send("Input.synthesizeScrollGesture", { ...point, yDistance: -350 });
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(up);
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(mutations, []);
  } finally {
    await context.close();
  }
}
