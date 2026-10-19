import { expect, test } from '@playwright/test';
import { login } from './fixtures/daemon';

test.describe('Logs · processor errors', () => {
  test('logs page shows processor errors and daemon logs', async ({ page }) => {
    await login(page);
    await page.goto('/logs');

    await expect(page.getByRole('heading', { name: 'Logs' })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'Processor errors' })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'Daemon logs' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'platform' })).toBeVisible();

    const processorRow = page
      .locator('.processor-errors-table tbody tr')
      .filter({ hasText: 'Classification call failed: timeout' })
      .first();
    await expect(processorRow).toContainText('course-123');
    await expect(processorRow).toContainText('olek');

    await page.getByRole('tab', { name: 'Daemon logs' }).click();
    await expect(page.getByRole('tab', { name: 'Daemon logs' })).toHaveAttribute(
      'data-state',
      'active',
    );
    await expect(page.getByRole('columnheader', { name: 'event' })).toBeVisible();
    await expect(page.locator('.daemon-logs-table tbody tr').first()).toBeVisible();
  });
});
