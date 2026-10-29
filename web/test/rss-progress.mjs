import { expect } from "@playwright/test";

/** Display estimates without treating rounded RSS totals as completion signals. */
export async function checkRssProgressDisplay(page) {
  const pattern = "**/api/tasks?**";
  const base = {
    original_name: "progress.mp4", show_title: "Progress fixture", season: 1, episode: "01",
    submission: "submitted", speed: 1024, downloader: "aria2", engine_attempts: [],
    rss_size_bytes: 200 * 1024 ** 2,
  };
  const tasks = [
    { ...base, id: 1, file_name: "Estimated progress.mp4", status: "downloading",
      progress: 25, progress_estimated: true, downloaded: 50 * 1024 ** 2, total: 0 },
    { ...base, id: 2, file_name: "Rounded boundary.mp4", status: "downloading",
      progress: 99.9, progress_estimated: true, downloaded: 201 * 1024 ** 2, total: 0 },
    { ...base, id: 3, file_name: "Exact completed.mp4", status: "completed",
      progress: 100, progress_estimated: false, downloaded: 201 * 1024 ** 2, total: 201 * 1024 ** 2 },
  ];
  await page.route(pattern, (route) => route.fulfill({ json: { success: true, data: tasks, total: 3 } }));
  try {
    await page.getByRole("link", { name: /同步记录$/ }).click();
    await page.getByRole("link", { name: /同步$/ }).click();
    const estimated = page.getByRole("row").filter({ hasText: "Estimated progress.mp4" });
    await expect(estimated.getByText("≈25%", { exact: true })).toBeVisible();
    await expect(estimated).toContainText("50.0 MB / 200.0 MB");
    await expect(estimated).not.toContainText("约");
    const rounded = page.getByRole("row").filter({ hasText: "Rounded boundary.mp4" });
    await expect(rounded.getByText("≈99.9%", { exact: true })).toBeVisible();
    await expect(rounded).toContainText("下载中");
    const completed = page.getByRole("row").filter({ hasText: "Exact completed.mp4" });
    await expect(completed).toContainText("已完成");
    await expect(completed).toContainText("201.0 MB / 201.0 MB");
    await expect(completed).not.toContainText("约");
    await expect(completed).not.toContainText("≈");
  } finally {
    await page.unroute(pattern);
  }
}
