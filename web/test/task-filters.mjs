import assert from "node:assert/strict";
import { expect } from "@playwright/test";

/** Uses checkSyncLayout's browser fixtures; no application data is written. */
export async function checkTaskFilters(page, fileName) {
  const toggle = page.getByRole("button", { name: "筛选", exact: true });
  const form = page.getByRole("form", { name: "任务筛选" });
  const body = page.locator(".tasks-table-viewport .ant-table-body");
  const refresh = page.locator(".tasks-table-viewport .anticon-reload");
  await page.setViewportSize({ width: 1280, height: 720 });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(form).toHaveCount(0);
  await expect(page.locator(".ant-pro-table-search")).toHaveCount(0);
  const refreshBounds = await refresh.boundingBox();
  const toggleBounds = await toggle.boundingBox();
  assert(toggleBounds.x >= refreshBounds.x + refreshBounds.width);

  await page
    .locator(".tasks-table-viewport .ant-pagination")
    .getByTitle("2", { exact: true })
    .click();
  await expect(page.locator(".ant-pagination-item-active")).toHaveText("2");
  await expect
    .poll(() => body.evaluate((node) => node.clientHeight))
    .toBeGreaterThan(390);
  const closedHeight = await body.evaluate((node) => node.clientHeight);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(form).toBeVisible();
  await expect
    .poll(() => body.evaluate((node) => node.clientHeight))
    .toBeLessThan(closedHeight - 30);
  await expect
    .poll(() =>
      page.evaluate(() => {
        const form = document
          .querySelector("#task-filters")
          .getBoundingClientRect();
        const title = document
          .querySelector(".ant-pro-table-list-toolbar-container")
          .getBoundingClientRect();
        const header = document
          .querySelector(".tasks-table-viewport .ant-table-header")
          .getBoundingClientRect();
        return form.top >= title.bottom && form.bottom <= header.top;
      }),
    )
    .toBe(true);

  const search = form.getByLabel("文件 / 剧名", { exact: true });
  await search.fill(fileName);
  await form.getByRole("combobox").click();
  await page
    .locator(".ant-select-item-option-content")
    .getByText("下载中", { exact: true })
    .click();
  const matches = (search, status) => (response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/tasks" &&
      url.searchParams.get("page") === "1" &&
      url.searchParams.get("search") === search &&
      url.searchParams.get("status") === status
    );
  };
  await Promise.all([
    page.waitForResponse(matches(fileName, "downloading")),
    search.press("Enter"),
  ]);
  await expect(page.locator(".ant-table-body .ant-table-row")).toHaveCount(1);
  await expect(page.locator(".ant-pagination-item-active")).toHaveText("1");
  // Repeated searches and refreshing continue to use the applied filters.
  await Promise.all([
    page.waitForResponse(matches(fileName, "downloading")),
    form.getByRole("button", { name: /搜\s*索/ }).click(),
  ]);
  await search.fill("未提交的草稿");
  await toggle.click();
  await expect(form).toHaveCount(0);
  await expect(toggle).toHaveClass(/ant-btn-primary/);
  await expect
    .poll(() => body.evaluate((node) => node.clientHeight))
    .toBe(closedHeight);
  await Promise.all([
    page.waitForResponse(matches(fileName, "downloading")),
    refresh.click(),
  ]);
  await toggle.click();
  await expect(search).toHaveValue("未提交的草稿");
  await expect(form.getByText("下载中", { exact: true })).toBeVisible();
  await Promise.all([
    page.waitForResponse(matches("", "")),
    form.getByRole("button", { name: /重\s*置/ }).click(),
  ]);
  await expect(search).toHaveValue("");
  await expect(page.locator(".ant-table-body .ant-table-row")).toHaveCount(20);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(form).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.scrollingElement.scrollWidth <= innerWidth + 1,
      ),
    )
    .toBe(true);
  await expect(form.getByRole("button", { name: /搜\s*索/ })).toBeVisible();
  await expect(form.getByRole("button", { name: /重\s*置/ })).toBeVisible();
  await toggle.focus();
  await page.keyboard.press("Space");
  await expect(form).toHaveCount(0);
  await expect(toggle).not.toHaveClass(/ant-btn-primary/);
  await page.setViewportSize({ width: 1500, height: 1100 });
  await page.evaluate(() => window.scrollTo(0, 0));
}
