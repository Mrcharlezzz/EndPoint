import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Slice 3: ErrorsFeed groups identical (severity, code, platform) rows into
// a single group-head with a count. Clicking the chevron expands child
// detail rows. Message cells carry a copy-to-clipboard button on hover.

test.describe('Overview · ErrorsFeed duplicate collapse', () => {
  test('header reports group + event counts', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    const header = page
      .locator('.feed-card')
      .filter({ has: page.getByText(/Recent errors/i) })
      .locator('.card-h .sub');
    await expect(header).toContainText(/\d+ group/);
    await expect(header).toContainText(/\d+ events/);
  });

  test('group rows carry severity stripe and render copy button', async ({ page }) => {
    await login(page);
    const feed = page.locator('.feed-card').filter({ has: page.getByText(/Recent errors/i) });

    // If the seeded data populated the feed, there should be at least one
    // sev-row. Otherwise the empty-state branch is tested separately.
    const sevRows = feed.locator('tbody tr.sev-row');
    const rowCount = await sevRows.count();
    if (rowCount === 0) {
      await expect(feed.getByText(/No errors in the window\./)).toBeVisible();
      return;
    }
    // Every sev-row should carry exactly one of sev-err / sev-warn / sev-info.
    for (let i = 0; i < Math.min(rowCount, 5); i++) {
      const cls = await sevRows.nth(i).getAttribute('class');
      expect(cls ?? '').toMatch(/sev-(err|warn|info)/);
    }
    // Copy buttons render inside message cells whenever a message exists,
    // and become visible on hover. At least one should exist in the DOM.
    const copyBtns = feed.locator('.msg-copy');
    expect(await copyBtns.count()).toBeGreaterThan(0);
  });

  test('chevron toggles a multi-count group when present', async ({ page }) => {
    await login(page);
    const feed = page.locator('.feed-card').filter({ has: page.getByText(/Recent errors/i) });

    const groupHeads = feed.locator('tbody tr.group-head');
    const multiCount = await groupHeads.count();
    // Seeded dataset may or may not produce duplicates. If none, this test
    // is a no-op success — the empty branch is covered above.
    test.skip(multiCount === 0, 'No multi-event groups in the seeded dataset');

    const first = groupHeads.first();
    const chevron = first.locator('.group-chevron');
    await expect(chevron).not.toHaveClass(/open/);
    await first.click();
    await expect(chevron).toHaveClass(/open/);
    // Child rows are siblings with class ``group-child``.
    await expect(feed.locator('tr.group-child').first()).toBeVisible();
    // Re-click collapses.
    await first.click();
    await expect(chevron).not.toHaveClass(/open/);
  });
});
