import { expect, test } from '@playwright/test';
import { apiCall, login, seededUser } from './fixtures/daemon';

test('unauthed visit redirects to /login', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/\/login/);
});

test('login renders the shell + sidebar against the real daemon', async ({ page }) => {
  await login(page);
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Syncs' })).toBeVisible();

  await page.getByRole('link', { name: 'Syncs' }).click();
  await expect(page).toHaveURL(/\/syncs/);
  await expect(page.getByRole('heading', { name: 'Syncs' })).toBeVisible();
});

test('authed /api/credentials returns the 7 seeded rows', async ({ page }) => {
  await login(page);
  const response = await apiCall(page, '/api/credentials');
  expect(response.status()).toBe(200);
  const rows = (await response.json()) as { user_email: string; platform: string }[];
  expect(rows).toHaveLength(7);
  // Spot-check one known seed row.
  const sample = seededUser(0);
  expect(rows).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ user_email: sample.email, platform: sample.platform }),
    ]),
  );
});
