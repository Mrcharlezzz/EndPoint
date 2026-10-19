import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Slice 4: PlatformPanel rows become buttons that open a side-slide drawer
// with per-platform detail (summary KPIs, credentials list for teams/moodle,
// recent syncs). Each row also renders a 24h throughput sparkline.

test.describe('Overview · PlatformPanel drill-in', () => {
  test('each row renders a button with a sparkline', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    // 3 known platforms: teams, moodle, telegram.
    const rowBtns = page.locator('.pp-meta-btn');
    await expect(rowBtns).toHaveCount(3);

    for (let i = 0; i < 3; i++) {
      await expect(rowBtns.nth(i)).toHaveAttribute('aria-label', /Open .+ detail/);
    }
    // Sparklines present on each row.
    const sparks = page.locator('.pp-spark svg');
    await expect(sparks).toHaveCount(3);
  });

  test('clicking a row opens the drill drawer with platform detail', async ({ page }) => {
    await login(page);
    const teamsBtn = page.locator('.pp-meta-btn').filter({ hasText: /Teams/ }).first();
    await teamsBtn.click();

    const drawer = page.locator('.pp-drill');
    await expect(drawer).toBeVisible();
    // Four summary KPIs: total syncs, failed, credentials, last success.
    await expect(drawer.locator('.pp-drill-kpi')).toHaveCount(4);
    // Teams is a per-user platform, so the Credentials section renders.
    await expect(drawer.getByText(/Credentials \(\d+\)/)).toBeVisible();
  });

  test('Escape and backdrop both close the drawer', async ({ page }) => {
    await login(page);
    const firstRow = page.locator('.pp-meta-btn').first();
    await firstRow.click();
    await expect(page.locator('.pp-drill')).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(page.locator('.pp-drill')).toBeHidden();

    await firstRow.click();
    await expect(page.locator('.pp-drill')).toBeVisible();
    await page.locator('.pp-drill-backdrop').click();
    await expect(page.locator('.pp-drill')).toBeHidden();
  });

  test('telegram drawer omits the credentials section (singleton bot)', async ({ page }) => {
    await login(page);
    const telegramBtn = page
      .locator('.pp-meta-btn')
      .filter({ hasText: /Telegram/ })
      .first();
    await telegramBtn.click();
    const drawer = page.locator('.pp-drill');
    await expect(drawer).toBeVisible();
    // Per Telegram-sentinel pattern, no per-user cred list here.
    await expect(drawer.getByText(/Credentials \(/)).toHaveCount(0);
    // "credentials: bot" shows in the summary KPIs.
    await expect(drawer.locator('.pp-drill-kpi', { hasText: /credentials/i })).toContainText(/bot/);
  });

  test('row button is keyboard-reachable and opens on Enter', async ({ page }) => {
    await login(page);
    const firstRow = page.locator('.pp-meta-btn').first();
    await firstRow.focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('.pp-drill')).toBeVisible();
  });
});
