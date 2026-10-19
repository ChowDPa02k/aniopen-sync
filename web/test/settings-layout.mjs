import { expect } from "@playwright/test";

export async function checkSettingsLayout(page) {
  const yamlMode = page.getByRole("button", { name: "YAML模式", exact: true });
  const formMode = page.getByRole("button", { name: "表单模式", exact: true });
  const save = page.getByRole("button", { name: "保存并应用", exact: true });
  await expect(yamlMode).toBeVisible();
  await expect(page.getByRole("tablist")).toHaveCount(1);
  const modeBox = await yamlMode.boundingBox();
  const reloadBox = await page
    .getByRole("button", { name: "重新载入", exact: true })
    .boundingBox();
  expect(modeBox.x).toBeLessThan(reloadBox.x);
  expect(Math.abs(modeBox.y - reloadBox.y)).toBeLessThan(2);

  for (const name of ["ANi 订阅与解析", "同步计划", "下载器连接", "长期运行"]) {
    await page.getByRole("tab", { name, exact: true }).click();
    const active = page.locator(".ant-tabs-tabpane-active");
    await expect
      .poll(() => active.locator(".ant-form-item").count())
      .toBeGreaterThan(0);
    expect(
      await active
        .locator(".ant-form-item")
        .evaluateAll((items) =>
          items.every((item) => item.closest(".settings-group")),
        ),
    ).toBe(true);
    await expect(
      active
        .locator(".settings-group")
        .first()
        .locator(".ant-collapse-header")
        .first(),
    ).toHaveAttribute("aria-expanded", "true");
  }

  await page.getByRole("tab", { name: "ANi 订阅与解析", exact: true }).click();
  const group = page.locator(".settings-group").filter({
    has: page.getByText("标题与季集解析", { exact: true }),
  });
  const field = page.getByLabel("自定义季映射", { exact: true });
  const original = await field.inputValue();
  await field.fill("[");
  await expect(save).toBeDisabled();
  await group.locator(".ant-collapse-header").click();
  await expect(field).toBeHidden();
  await expect(save).toBeDisabled();
  await yamlMode.click();
  await expect(
    page.getByText("请先修复 YAML 字段", { exact: true }),
  ).toBeVisible();
  await expect(formMode).toHaveCount(0);
  await group.locator(".ant-collapse-header").click();
  await expect(field).toHaveValue("[");
  await field.fill(original);
  await expect(save).toBeEnabled();

  await yamlMode.click();
  const yaml = page.locator("textarea.yaml");
  const raw = await yaml.inputValue();
  await expect(page.getByRole("tablist")).toHaveCount(0);
  await yaml.fill(raw + "\ninvalid: [");
  const response = page.waitForResponse((r) =>
    r.url().endsWith("/api/config/validate"),
  );
  await formMode.click();
  expect((await response).ok()).toBe(false);
  await expect(formMode).toHaveAttribute("aria-busy", "false");
  await expect(yaml).toHaveValue(raw + "\ninvalid: [");
  await expect(formMode).toBeVisible();
  await yaml.fill(raw);
  await formMode.click();
  await expect(page.getByRole("tablist")).toHaveCount(1);

  await page.setViewportSize({ width: 360, height: 780 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect
    .poll(() =>
      page.evaluate(
        () => document.scrollingElement.scrollWidth <= innerWidth + 1,
      ),
    )
    .toBe(true);
  expect(
    await page.locator(".settings-toolbar button").evaluateAll((buttons) =>
      buttons.every((button) => {
        const box = button.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth + 1;
      }),
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1500, height: 1100 });
}
