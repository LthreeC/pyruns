import { expect, test } from '@playwright/test'

test('unavailable GPU readings remain visible and cannot pass the scheduling preview', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/system/metrics**', route => route.fulfill({ json: {
    cpu_percent: 5, mem_percent: 10,
    gpus: [{
      id: 0, index: 0, name: 'Unknown readings GPU', uuid: 'GPU-UNKNOWN',
      util: null, mem_used: null, mem_total: 81920, processes: [],
    }],
  } }))

  await page.goto('/?token=pyruns-e2e-access-token')
  const card = page.getByRole('button', { name: 'View details for GPU 0 Unknown readings GPU' })
  await expect(card).toBeVisible()
  await expect(card).toContainText('Unknown')
  await expect(card).not.toContainText('0%')
  await card.click()
  const dialog = page.getByRole('dialog', { name: 'GPU 0 | Unknown readings GPU' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('Unknown', { exact: true })).toHaveCount(2)
  await expect(dialog.getByText('Unknown / 80 GB', { exact: true })).toBeVisible()
  await dialog.getByRole('button', { name: 'Close GPU details' }).click()

  await page.getByRole('button', { name: 'Runtime', exact: true }).click()
  const panel = page.getByRole('dialog', { name: 'Runtime settings' })
  await panel.getByRole('tab', { name: 'GPU', exact: true }).click()
  const enabled = panel.getByRole('switch', { name: 'GPU scheduling', exact: true })
  if (await enabled.getAttribute('aria-checked') !== 'true') await enabled.click()
  const preview = panel.getByRole('button', { name: /GPU 0 · Unknown readings GPU/ })
  await expect(preview).toContainText('Blocked')
  await preview.click()
  await expect(panel.getByText('• Memory metrics unavailable; waiting for a valid reading.')).toBeVisible()
  await expect(panel.getByText('• Compute metrics unavailable; waiting for a valid reading.')).toBeVisible()
  await expect(panel.getByText('All current thresholds pass.', { exact: false })).toHaveCount(0)
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('gpu-unavailable.png'), fullPage: true })
})
