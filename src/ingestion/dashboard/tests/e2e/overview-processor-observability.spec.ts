import { expect, test, type Page } from '@playwright/test';
import { login } from './fixtures/daemon';

test.describe('Overview · processor observability', () => {
  test('overview shows processor metrics and reset button', async ({ page }) => {
    await login(page);

    const panel = page.locator('.processor-panel');
    await expect(page.getByRole('heading', { name: 'Processor' })).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Reset window' })).toBeVisible();
    await expect(kpiValue(page, 'processed')).toHaveText('2');
    await expect(kpiValue(page, 'failed')).toHaveText('1');
    await expect(kpiValue(page, 'pending')).toHaveText('1');
    await expect(kpiValue(page, 'llm-calls')).toHaveText('2');
    await expect(kpiValue(page, 'input-tokens')).toHaveText('140');
    await expect(kpiValue(page, 'output-tokens')).toHaveText('29');
    await expect(kpiValue(page, 'llm-failed')).toHaveText('1');
    await expect(kpiValue(page, 'unclassified')).toHaveText('1');
    await expect(panel.locator('.processor-meta')).toContainText(/baseline/i);
    await expect(panel.locator('.processor-meta')).toContainText(/last processed/i);
  });
});

function kpiValue(page: Page, name: string) {
  return page.getByTestId(`processor-kpi-${name}`).locator('.value');
}
