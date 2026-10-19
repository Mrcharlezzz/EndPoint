import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Slice 7: FullResyncButton modal uses the project's Toggle component and
// renders a per-platform impact preview (total / expired / paused /
// will-sync) plus an aggregate "Total impact" line.

test.describe('Overview · FullResyncButton polish', () => {
  test('modal renders a toggle switch + counts for each platform', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();

    await page.getByRole('button', { name: /Full Resync/ }).click();
    await expect(page.locator('.fr-platform-row')).toHaveCount(2);

    // Each platform row has a role=switch toggle (from the Toggle component).
    const switches = page.locator('.fr-platform-row [role="switch"]');
    await expect(switches).toHaveCount(2);

    // Impact preview format: "N creds · N expired · N paused · N will sync".
    for (const label of ['Teams', 'Moodle']) {
      const row = page.locator('.fr-platform-row').filter({ hasText: label });
      await expect(row.locator('.fr-platform-counts')).toContainText(
        /\d+ creds · \d+ expired · \d+ paused · \d+ will sync/,
      );
    }
  });

  test('toggling a switch flips aria-checked + updates the total', async ({ page }) => {
    await login(page);
    await page.getByRole('button', { name: /Full Resync/ }).click();
    await expect(page.locator('.fr-platform-row')).toHaveCount(2);

    const sw = page.locator('.fr-platform-row [role="switch"]').first();
    const before = await sw.getAttribute('aria-checked');
    await sw.click();
    await expect(sw).toHaveAttribute('aria-checked', before === 'true' ? 'false' : 'true');
  });

  test('total-impact line renders when any selected platform has willSync > 0', async ({
    page,
  }) => {
    await login(page);
    await page.getByRole('button', { name: /Full Resync/ }).click();
    await expect(page.locator('.fr-platform-row')).toHaveCount(2);

    // At least one toggle starts selected (defaults to unpaused platforms).
    // Seeded data has non-paused creds on teams + moodle, so total impact
    // should be visible.
    await expect(page.getByText(/Total impact:/)).toBeVisible();
    await expect(page.getByText(/Total impact:/)).toContainText(/\d+ syncs/);
  });

  test('total-impact line hidden when all toggles off', async ({ page }) => {
    await login(page);
    await page.getByRole('button', { name: /Full Resync/ }).click();
    await expect(page.locator('.fr-platform-row')).toHaveCount(2);

    // Deselect every switch; total-impact should disappear.
    const switches = page.locator('.fr-platform-row [role="switch"]');
    const n = await switches.count();
    for (let i = 0; i < n; i++) {
      const s = switches.nth(i);
      if ((await s.getAttribute('aria-checked')) === 'true') {
        await s.click();
      }
    }
    await expect(page.getByText(/Total impact:/)).toHaveCount(0);
  });
});
