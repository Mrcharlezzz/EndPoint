import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Seeded data (see tests/e2e/fixtures/daemon.ts): 2 expired credentials,
// 1 paused Telegram. StatusStrip therefore renders "2 expired" + "1 paused"
// as attention links, and severity escalation should fire.

test.describe('Overview · signals & severity', () => {
  test('header err-rate link carries ?filter=failed_sync into Errors', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    const errLink = page
      .locator('header.page-header a[href*="/errors"][href*="filter=failed_sync"]')
      .first();
    await expect(errLink).toBeVisible();
    await errLink.click();

    await expect(page).toHaveURL(/\/errors\?filter=failed_sync/);
    await expect(page.getByRole('heading', { name: 'Errors' })).toBeVisible();
    // "Failed sync" pill should render active (class 'active' from FilterPill).
    await expect(page.locator('.pill.active', { hasText: /Failed sync/i })).toBeVisible();
  });

  test('header items-today link navigates to Syncs', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    const itemsLink = page.locator('header.page-header a[href="/syncs"]').first();
    await expect(itemsLink).toBeVisible();
    await itemsLink.click();
    await expect(page).toHaveURL(/\/syncs/);
    await expect(page.getByRole('heading', { name: 'Syncs' })).toBeVisible();
  });

  test('StatusStrip attention badges link to filtered pages + severity escalates', async ({
    page,
  }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    // Seed has two expired credentials; the expired badge should render as a link.
    const expiredLink = page.locator(
      '.status-strip a[href*="/errors"][href*="filter=expired_credential"]',
    );
    await expect(expiredLink).toBeVisible();
    await expect(expiredLink).toContainText(/expired/i);

    // Expired > 0 triggers severity escalation on the card.
    await expect(page.locator('.status-strip.severe')).toBeVisible();

    // Paused users badge links to /users?filter=paused.
    const pausedLink = page.locator('.status-strip a[href*="/users"][href*="filter=paused"]');
    await expect(pausedLink).toBeVisible();
    await expect(pausedLink).toContainText(/paused/i);

    // Clicking the expired badge lands on Errors with the right filter active.
    await expiredLink.click();
    await expect(page).toHaveURL(/\/errors\?filter=expired_credential/);
    await expect(page.locator('.pill.active', { hasText: /Expired credential/i })).toBeVisible();
  });

  test('ErrorsFeed rows carry severity stripe classes', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    // Seeded failures exist, so at least one severity row should be present.
    const sevRow = page.locator('.feed-card table.t tbody tr.sev-row').first();
    await expect(sevRow).toBeVisible();
    // Class is one of sev-err / sev-warn / sev-info.
    const cls = await sevRow.getAttribute('class');
    expect(cls ?? '').toMatch(/sev-(err|warn|info)/);
  });
});
