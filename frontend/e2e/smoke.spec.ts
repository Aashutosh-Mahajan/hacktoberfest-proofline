import { expect, test } from "@playwright/test";

/** Real backend smoke: healthy -> fault -> AI finds cause -> approve -> recovery -> money saved. */
test("S1 bad deploy end to end", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto("/");
  await page.request.post("/api/reset");
  await expect(page.getByRole("status").filter({ hasText: "All good" })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Everything looks healthy").first()).toBeVisible();
  await expect(page.getByRole("status", { name: /^Model / })).toBeVisible();

  // start the fault through the real UI
  await page.getByRole("button", { name: "Simulate" }).click();
  await page.getByRole("button", { name: "Start failure" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Problem detected" })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("heading", { name: "What the AI is doing" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Suggested fix" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByText("Undo the recent update")).toBeVisible();
  await expect(page.getByRole("heading", { name: "What went wrong" })).toBeVisible();

  await page.getByRole("button", { name: "Approve fix" }).click();
  await expect(page.getByRole("button", { name: "Fixing…" })).toBeVisible(); // optimistic
  await expect(page.getByRole("status").filter({ hasText: /Fixed in/ })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/saved/).first()).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "All good" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Next steps" })).toHaveCount(0);
  await page.getByRole("button", { name: "Deploy to server" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Deployed to the server" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Deploy to server" })).toHaveCount(0);
  await page.screenshot({ path: "test-results/dashboard-desktop.png", fullPage: true });

  // secondary views load lazily
  await expect(page.getByRole("tab", { name: "Results" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Messages" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Banks" }).click();
  await expect(page.getByText("Why payments failed")).toBeVisible();

  // Shift+D demo controls and projector theme
  await page.keyboard.press("Shift+D");
  await expect(page.getByLabel("Demo controls")).toBeVisible();
  await page.keyboard.press("Shift+D");
  await page.getByRole("button", { name: /projector theme/i }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "projector");

  expect(errors.filter((e) => !/favicon|Failed to load resource/.test(e))).toEqual([]);
});
