import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Slice 6: ActivityFeed rows are expandable — click reveals an inline
// phase timeline (from /api/syncs/{sync_id}). Items cells render a mini
// bar scaled against the window's max. Running rows get a shimmer.

test.describe('Overview · ActivityFeed deep-drill', () => {
  test('expandable rows expose a chevron button with aria-expanded', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    const feed = page.locator('.feed-card').filter({ hasText: /Activity feed/ });

    // Seeded syncs produce rows; at least one chevron should be present.
    const chevrons = feed.locator('button.group-chevron');
    const n = await chevrons.count();
    expect(n).toBeGreaterThan(0);
    await expect(chevrons.first()).toHaveAttribute('aria-expanded', 'false');
  });

  test('chevron click toggles the detail row', async ({ page }) => {
    await login(page);
    const feed = page.locator('.feed-card').filter({ hasText: /Activity feed/ });
    const chevron = feed.locator('button.group-chevron').first();

    await chevron.click();
    await expect(chevron).toHaveAttribute('aria-expanded', 'true');
    await expect(feed.locator('tr.activity-detail-row')).toHaveCount(1);

    await chevron.click();
    await expect(chevron).toHaveAttribute('aria-expanded', 'false');
    await expect(feed.locator('tr.activity-detail-row')).toHaveCount(0);
  });

  test('clicking anywhere on an expandable row toggles expansion', async ({ page }) => {
    await login(page);
    const feed = page.locator('.feed-card').filter({ hasText: /Activity feed/ });
    const firstRow = feed.locator('tbody tr.activity-row-expandable').first();
    const firstChevron = firstRow.locator('button.group-chevron');

    // Click on a non-chevron cell (state column) should toggle the row.
    await firstRow.locator('td').nth(1).click();
    await expect(firstChevron).toHaveAttribute('aria-expanded', 'true');
    await expect(feed.locator('tr.activity-detail-row')).toHaveCount(1);

    // Chevron click stops propagation so it doesn't double-toggle.
    await firstChevron.click();
    await expect(firstChevron).toHaveAttribute('aria-expanded', 'false');
    await expect(feed.locator('tr.activity-detail-row')).toHaveCount(0);
  });

  test('rows with an items stat render a mini bar + numeric cell', async ({ page }) => {
    await login(page);
    const feed = page.locator('.feed-card').filter({ hasText: /Activity feed/ });
    const bars = feed.locator('.items-bar-fill');
    // Seeded data has enough syncs with stats; at least one bar should render.
    expect(await bars.count()).toBeGreaterThan(0);
  });
});
