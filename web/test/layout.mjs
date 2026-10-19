import assert from "node:assert/strict";
import { expect } from "@playwright/test";

/** Mock only browser responses; never inserts tasks into the application database. */
export async function checkSyncLayout(page) {
  const pattern = "**/api/tasks?**";
  const titles = [
    "轉生哥布林有問題嗎？",
    "你好，身為魔女的我，被心上人委託製作迷情藥",
    "一覺醒來就有了最強裝備跟太空船 決定以自家獨棟建築為目標當傭兵自由過活",
  ];
  const tasks = Array.from({ length: 60 }, (_, index) => ({
    id: index + 1,
    triggered_at: "2026-10-18T06:00:00+00:00",
    trigger_type: "scheduled",
    original_name: `布局验证 ${index + 1}.mp4`,
    file_name: `[ANi] ${titles[index % titles.length]} - S01E${String(index + 1).padStart(2, "0")} [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4`,
    show_title: titles[index % titles.length],
    season: 1,
    episode: String(index + 1),
    submission: "submitted",
    status: "downloading",
    progress: 25,
    downloader: "aria2",
    downloaded: 81500569,
    total: 326002278,
    speed: 102400,
    observed_at: "2026-10-20T01:34:32+00:00",
    original_url: `https://example.org/${"%E5%8B%95%E7%95%AB".repeat(60)}.mp4`,
    download_url: `https://cdn.example.org/${"episode".repeat(120)}.mp4`,
    engine_id: "original-task-gid",
    current_engine_id: "original-task-gid",
    engine_attempts: [
      {
        engine_id: "original-task-gid",
        status: "complete",
        first_seen: "2026-10-18T06:00:00+00:00",
        last_seen: "2026-10-18T06:30:00+00:00",
      },
    ],
  }));
  await page.route(pattern, async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const size = Number(params.get("page_size") || 20);
    const offset = (Number(params.get("page") || 1) - 1) * size;
    await route.fulfill({
      json: {
        success: true,
        data: tasks.slice(offset, offset + size),
        total: tasks.length,
      },
    });
  });
  try {
    await page.getByRole("link", { name: /同步记录$/ }).click();
    await page.getByText("最近同步", { exact: true }).waitFor();
    assert.equal(await page.locator(".tasks-table-viewport").count(), 0);
    await page.getByRole("link", { name: /同步$/ }).click();
    const body = page
      .locator(
        ".tasks-table-viewport .ant-table-body, .tasks-table-viewport .ant-table-content",
      )
      .first();
    await page.getByText(tasks[0].file_name, { exact: true }).waitFor();
    assert.equal(await page.getByText("最近同步", { exact: true }).count(), 0);
    for (const title of [
      "已创建记录",
      "进行中 / 待确认",
      "下载完成",
      "发送或下载失败",
    ]) {
      await page.locator(".stats").getByText(title, { exact: true }).waitFor();
    }
    for (const viewport of [
      { width: 1500, height: 1100 },
      { width: 1440, height: 800 },
      { width: 1280, height: 720 },
      { width: 1280, height: 640 },
      { width: 1024, height: 600 },
      { width: 900, height: 600 },
      { width: 1280, height: 900 },
      { width: 1280, height: 901 },
      { width: 1440, height: 480 },
      { width: 390, height: 844 },
      { width: 1500, height: 1100 },
    ]) {
      await page.setViewportSize(viewport);
      await expect
        .poll(() =>
          page.evaluate(() => {
            const table = document.querySelector(".tasks-table-viewport");
            const body = table.querySelector(
              ".ant-table-body, .ant-table-content",
            );
            const pager = table.querySelector(".ant-pagination");
            const stats = document.querySelector(".stats");
            if (innerWidth <= 767 || innerHeight <= 500) {
              return (
                stats.getBoundingClientRect().bottom <=
                  table.getBoundingClientRect().top &&
                body.clientHeight >= 280 &&
                document.scrollingElement.scrollHeight > innerHeight &&
                document.scrollingElement.scrollWidth <= innerWidth + 1
              );
            }
            return (
              stats.getBoundingClientRect().bottom <=
                table.getBoundingClientRect().top &&
              body.clientHeight > 40 &&
              body.scrollHeight > body.clientHeight &&
              document.scrollingElement.scrollHeight <=
                window.innerHeight + 1 &&
              table.scrollHeight <= table.clientHeight + 1 &&
              table.getBoundingClientRect().bottom >= window.innerHeight - 40 &&
              pager.getBoundingClientRect().bottom <= window.innerHeight
            );
          }),
        )
        .toBe(true);
      if (
        viewport.width >= 768 &&
        viewport.height > 500 &&
        viewport.height <= 900
      ) {
        // Allow responsive columns and ResizeObserver to settle after resizing.
        await expect
          .poll(() => body.evaluate((element) => element.clientHeight), {
            message: `Room for tasks at ${viewport.width}×${viewport.height}`,
          })
          .toBeGreaterThanOrEqual(viewport.height - 390);
        await expect
          .poll(() =>
            body.evaluate((element) => {
              const rect = element.getBoundingClientRect();
              return [...element.querySelectorAll(".ant-table-row")].filter(
                (row) => {
                  const bounds = row.getBoundingClientRect();
                  return bounds.top >= rect.top && bounds.bottom <= rect.bottom;
                },
              ).length;
            }),
          )
          .toBeGreaterThanOrEqual(viewport.height >= 700 ? 3 : 2);
        await body.hover();
        await page.mouse.wheel(0, 300);
        await expect
          .poll(() => body.evaluate((element) => element.scrollTop))
          .toBeGreaterThan(0);
        assert.equal(await page.evaluate(() => window.scrollY), 0);
        await body.evaluate((element) => {
          element.scrollTop = 0;
        });
        if (viewport.width === 1280 && viewport.height === 720) {
          const heights = [];
          for (const name of ["宽松", "中等", "紧凑", "中等"]) {
            await page.locator(".anticon-column-height").click();
            await page.getByRole("menuitem", { name, exact: true }).click();
            heights.push(
              await body
                .locator(".ant-table-row")
                .first()
                .evaluate((row) => row.getBoundingClientRect().height),
            );
          }
          assert(heights[0] > heights[1] && heights[1] > heights[2]);
          assert.equal(heights[1], heights[3]);
        }
      }
    }
    await body.hover();
    await page.mouse.wheel(0, 450);
    await expect
      .poll(() => body.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    assert.equal(await page.evaluate(() => window.scrollY), 0);
    await body.evaluate((element) => {
      element.scrollTop = 0;
    });
    await page.locator(".ant-table-row-expand-icon-collapsed").first().click();
    for (const viewport of [
      { width: 1500, height: 1100 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(viewport);
      await expect
        .poll(() =>
          page.evaluate(() => {
            const cell = document.querySelector(".ant-table-expanded-row > td");
            const text = cell.querySelector(".ant-typography");
            const table = cell.querySelector(".ant-table-wrapper");
            const inner = table.querySelector(".ant-table");
            const cellRect = cell.getBoundingClientRect();
            const textRect = text.getBoundingClientRect();
            const tableRect = table.getBoundingClientRect();
            const innerRect = inner.getBoundingClientRect();
            return (
              textRect.left - cellRect.left >= 16 &&
              Math.abs(textRect.left - tableRect.left) <= 1 &&
              Math.abs(innerRect.left - tableRect.left) <= 1 &&
              Math.abs(innerRect.right - tableRect.right) <= 1 &&
              tableRect.right <= cellRect.right - 16 &&
              text.scrollWidth <= text.clientWidth + 1
            );
          }),
        )
        .toBe(true);
    }
    await page.setViewportSize({ width: 1500, height: 1100 });
    assert.equal(
      await page.evaluate(
        () => document.scrollingElement.scrollHeight > innerHeight + 1,
      ),
      false,
    );
    await page.locator(".ant-table-row-expand-icon-expanded").first().click();
    await page.getByRole("link", { name: /同步记录$/ }).click();
    await page.getByText("最近同步", { exact: true }).waitFor();
    await page.getByRole("link", { name: /同步$/ }).click();
    await page.getByText(tasks[0].file_name, { exact: true }).waitFor();
  } finally {
    await page.unroute(pattern);
  }
}
