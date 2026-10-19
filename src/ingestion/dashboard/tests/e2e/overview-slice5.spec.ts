import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

// Slice 5: NextSyncPanel groups rows under collapsible per-platform sections
// and renders a "sync now" icon-button on every runnable row.

test.describe('Overview · NextSyncPanel', () => {
  test('rows are grouped by platform into collapsible sections', async ({ page }) => {
    await login(page);
    await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
    // Let the schedule query settle.
    await expect(
      page
        .locator('.feed-card')
        .filter({ hasText: /Next sync/ })
        .locator('.card-h .sub'),
    ).toContainText(/\d+ credentials/);

    const sections = page.locator('.nsp-section-toggle');
    const n = await sections.count();
    expect(n).toBeGreaterThanOrEqual(1);

    // First section starts expanded.
    await expect(sections.first()).toHaveAttribute('aria-expanded', 'true');

    // Clicking collapses.
    await sections.first().click();
    await expect(sections.first()).toHaveAttribute('aria-expanded', 'false');

    // Click again expands.
    await sections.first().click();
    await expect(sections.first()).toHaveAttribute('aria-expanded', 'true');
  });

  test('runnable rows expose a sync-now button with descriptive aria-label', async ({ page }) => {
    await login(page);
    await expect(
      page
        .locator('.feed-card')
        .filter({ hasText: /Next sync/ })
        .locator('.card-h .sub'),
    ).toContainText(/\d+ credentials/);

    const force = page.locator('button.nsp-force');
    const n = await force.count();
    // Seed has non-expired creds, so at least one runnable row should exist.
    expect(n).toBeGreaterThan(0);

    const label = await force.first().getAttribute('aria-label');
    expect(label ?? '').toMatch(/Sync .+ (teams|moodle|telegram) now/);
  });

  test('sync-now button click queues a sync and surfaces a toast', async ({ page }) => {
    await login(page);
    await expect(
      page
        .locator('.feed-card')
        .filter({ hasText: /Next sync/ })
        .locator('.card-h .sub'),
    ).toContainText(/\d+ credentials/);

    const firstForce = page.locator('button.nsp-force').first();
    await expect(firstForce).toBeVisible();
    // Pull user/platform from aria-label so the toast assertion can match
    // the *clicked* row, not a race-case coincidence from a previous test.
    const label = (await firstForce.getAttribute('aria-label')) ?? '';
    const m = /Sync (\S+) (teams|moodle|telegram) now/.exec(label);
    expect(m, `aria-label didn't match: ${label}`).not.toBeNull();
    const [, user, platform] = m!;

    await firstForce.click();

    await expect(page.getByText(/Sync queued/i)).toBeVisible({ timeout: 5000 });
    // Toast body includes the clicked user + capitalised platform.
    const capPlat = platform.charAt(0).toUpperCase() + platform.slice(1);
    await expect(page.getByText(new RegExp(`${user}.*${capPlat}`))).toBeVisible();
  });
});
