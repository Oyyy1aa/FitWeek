import { expect, test } from "@playwright/test";

test("仪表盘通过浏览器代理读取真实 FastAPI 数据", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading")).toContainText("本周训练计划");
  await expect(page.getByText("尚未创建")).toBeVisible();
  await expect(page.getByText("来自 API 的真实计划数据")).toBeVisible();
  await expect(page.getByRole("alert")).not.toBeVisible();
});

test("API 不可用时仪表盘明确显示降级状态", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => route.abort());
  await page.goto("/");

  await expect(page.getByRole("alert")).toContainText("训练数据暂时不可用");
  await expect(page.getByRole("alert")).toContainText("不会把失败伪装成成功");
});
