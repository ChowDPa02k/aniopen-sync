/** Browser contracts against a disposable Compose instance. No downloader is contacted. */
import assert from "node:assert/strict";
import { chromium } from "@playwright/test";
import { checkSyncLayout } from "./layout.mjs";
import { parse } from "yaml";

const url = process.env.ANIOPEN_TEST_URL;
const token = process.env.ANIOPEN_TEST_TOKEN;
assert(url && token, "ANIOPEN_TEST_URL and ANIOPEN_TEST_TOKEN are required");
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
  headless: true,
});
const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
page.setDefaultTimeout(15000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.goto(url);
  await page
    .getByRole("button", { name: "进入控制台", exact: true })
    .waitFor({ state: "visible" });
  await page.getByLabel("访问令牌", { exact: true }).fill("invalid-test-token");
  await page.getByRole("button", { name: "进入控制台", exact: true }).click();
  await page.getByText("请填写访问令牌").waitFor();
  assert.equal(
    await page.evaluate(() => sessionStorage.getItem("aniopen-token")),
    null,
  );
  await page.getByLabel("访问令牌", { exact: true }).fill(token);
  await page.getByRole("button", { name: "进入控制台", exact: true }).click();
  await checkSyncLayout(page);
  await page.getByRole("link", { name: "配置中心" }).click();
  await page.getByRole("tab", { name: "长期运行", exact: true }).click();
  const retryCard = page.locator(".ant-card").filter({ has: page.getByText("aria2 HTTP 自动重试", { exact: true }) }).last();
  const retrySwitch = retryCard.getByRole("switch");
  assert.equal(await retrySwitch.getAttribute("aria-checked"), "false");
  await retrySwitch.click();
  await page.getByRole("tab", { name: "完整 YAML", exact: true }).click();
  assert.equal(parse(await page.locator("textarea.yaml").inputValue()).download_retry.enabled, true);
  await page.getByRole("tab", { name: "可视化配置", exact: true }).click();
  await page.getByRole("tab", { name: "长期运行", exact: true }).click();
  await retrySwitch.click();
  await page.getByRole("tab", { name: "完整 YAML", exact: true }).click();
  const yaml = page.locator("textarea.yaml");
  const initial = await yaml.inputValue();
  assert(
    initial.includes("********") && !initial.includes("smoke-only-secret"),
  );
  await yaml.fill(initial.replace("Asia/Shanghai", "UTC"));
  await page.getByRole("tab", { name: "可视化配置", exact: true }).click();
  await page.getByRole("tab", { name: "同步计划", exact: true }).click();
  await page.getByRole("button", { name: "使用每小时 06:30 / 36:30" }).click();
  await page.getByRole("tab", { name: "完整 YAML", exact: true }).click();
  assert(
    (await yaml.inputValue()).includes("timezone: UTC"),
    "Preset must preserve timezone",
  );
  const unsaved = (await yaml.inputValue()).replace(
    "history_retention_days: 180",
    "history_retention_days: 90",
  );
  await yaml.fill(unsaved);
  // A server-side expired token must open reauthentication without unmounting Settings.
  await page.route(
    "**/api/status",
    async (route) => {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: '{"detail":"访问令牌无效"}',
      });
    },
    { times: 1 },
  );
  await page
    .getByRole("dialog")
    .filter({ hasText: "重新验证访问令牌" })
    .waitFor();
  await page.getByLabel("访问令牌", { exact: true }).fill(token);
  await page.getByRole("button", { name: "进入控制台", exact: true }).click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  assert.equal(
    await yaml.inputValue(),
    unsaved,
    "401 recovery must preserve unsaved edits",
  );
  await page.getByRole("button", { name: /退\s*出/ }).click();
  await page
    .getByRole("dialog")
    .filter({ hasText: "退出控制台？" })
    .getByRole("button", { name: /取\s*消/ })
    .click();
  assert.equal(await yaml.inputValue(), unsaved);
  await page.getByRole("link", { name: "同步记录" }).click();
  await page
    .getByRole("dialog")
    .filter({ hasText: "离开配置中心？" })
    .getByRole("button", { name: /取\s*消/ })
    .click();
  assert.equal(await yaml.inputValue(), unsaved);
  await page.getByRole("button", { name: "保存并应用", exact: true }).click();
  await page.getByText("未保存", { exact: true }).waitFor({ state: "hidden" });
  await page.getByRole("tab", { name: "可视化配置", exact: true }).click();
  await page.getByRole("tab", { name: "下载器连接", exact: true }).click();
  await page.getByRole("button", { name: "添加下载器连接" }).click();
  await page.getByPlaceholder("例如 aria2-home").fill("contract-download");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /确\s*定/ })
    .click();
  await page.getByText("contract-download", { exact: true }).waitFor();
  await page.getByRole("button", { name: "保存并应用", exact: true }).click();
  await page.getByText("未保存", { exact: true }).waitFor({ state: "hidden" });
  assert.deepEqual(errors, [], "Browser must not raise JavaScript errors");
  console.log(
    JSON.stringify({
      browser_login: "passed",
      retry_switch_and_yaml: "passed",
      schema_forms: "passed",
      timezone_preset: "passed",
      masked_yaml: "passed",
      dirty_guards: "passed",
      sync_navigation_and_table_scroll: "passed",
      reauthentication_preserves_edits: "passed",
    }),
  );
} finally {
  // A dirty form intentionally prevents navigation; teardown closes the entire isolated context.
  await browser.close();
}
