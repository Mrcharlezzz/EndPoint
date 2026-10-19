import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Slice 2: LiveChip becomes a clickable trigger that opens a debug popover
// listing every active WebSocket channel. The chip's visual state matches
// the /ws/metrics connection (connected / reconnecting / disconnected).

test.describe('Overview · LiveChip', () => {
  test('click opens popover listing active channels', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    const trigger = page.locator('header.page-header .live-trigger').first();
    await expect(trigger).toBeVisible();
    await expect(trigger).toHaveAttribute('aria-haspopup', 'dialog');
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await trigger.click();
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');

    const popover = page.locator('.live-popover');
    await expect(popover).toBeVisible();

    // Overview subscribes to /ws/metrics (LiveChip) + /ws/syncs (ActivityFeed)
    // + /ws/errors (ErrorsFeed). All should appear.
    await expect(popover.getByText('/ws/metrics')).toBeVisible();
    await expect(popover.getByText('/ws/syncs')).toBeVisible();
    await expect(popover.getByText('/ws/errors')).toBeVisible();

    // Each row renders exactly one status dot. Assert per-row to avoid the
    // race of calling .count() twice at different instants.
    const rows = popover.locator('.live-popover-row');
    const n = await rows.count();
    expect(n).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < n; i++) {
      await expect(rows.nth(i).locator('.live-dot')).toHaveCount(1);
    }
  });

  test('popover closes on Escape', async ({ page }) => {
    await login(page);
    const trigger = page.locator('header.page-header .live-trigger').first();
    await trigger.click();
    await expect(page.locator('.live-popover')).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(page.locator('.live-popover')).toBeHidden();
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  test('popover closes on outside click', async ({ page }) => {
    await login(page);
    const trigger = page.locator('header.page-header .live-trigger').first();
    await trigger.click();
    await expect(page.locator('.live-popover')).toBeVisible();

    await page.getByRole('heading', { name: 'Overview' }).click();
    await expect(page.locator('.live-popover')).toBeHidden();
  });
});
