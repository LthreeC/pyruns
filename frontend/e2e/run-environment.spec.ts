import { expect, test } from '@playwright/test'

for (const runCount of [1, 3]) {
  test(`run environments show each run once and preserve history (${runCount} runs)`, async ({ page, isMobile }, testInfo) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    if (isMobile) await page.setViewportSize({ width: 390, height: 844 })
    const environment = {
      host: 'gpu-server-02', system: 'Ubuntu 22.04 · x86_64', launcher: '/usr/bin/bash',
      cuda_visible_devices: '2,3', assigned_gpu_ids: [2, 3], gpu_scope: 'assigned', gpu_status: 'ok',
      gpus: [2, 3].map(index => ({ index, uuid: `GPU-${index}`, name: 'NVIDIA A100', memory_total_mb: 81920 })),
    }
    const task = {
      name: 'training', status: 'failed', task_kind: 'shell', dir: '/app/experiments/training',
      config: {}, config_text: 'bash train.sh', config_file: 'config.sh', preview_text: 'bash train.sh',
      created_at: '2026-09-11_01-35-55', run_index: runCount, pinned: false, notes: '', env: {},
      cmd: ['bash', 'train.sh'], workdir: '/app/experiments',
      start_times: ['2026-09-11_01-36-09', '2026-09-11_15-18-15', '2026-09-11_16-20-00'].slice(-runCount),
      finish_times: ['2026-09-11_14-51-11', '2026-09-11_15-18-31', '2026-09-11_16-21-00'].slice(-runCount),
      pids: [111, 222, 333].slice(-runCount), durations: [47702, 16, 60].slice(-runCount), exit_codes: [1, 0, 1].slice(-runCount),
      run_statuses: ['failed', 'completed', 'failed'].slice(-runCount), records: [], tracks: [],
      run_environments: [null, { ...environment, host: 'gpu-server-01' }, environment].slice(-runCount),
    }
    await page.route('**/api/tasks?*', route => route.fulfill({ json: {
      items: [task], total: 1, offset: 0, limit: 50, has_more: false,
      status_counts: { pending: 0, queued: 0, running: 0, completed: 0, failed: 1, cancelled: 0 },
    } }))
    await page.route('**/api/tasks/training?*', route => route.fulfill({ json: task }))
    await page.goto('/manager?token=pyruns-e2e-access-token')
    await page.getByRole('button', { name: 'Open details for training' }).click()
    const panel = page.getByRole('dialog', { name: 'Task details for training' })
    const latest = panel.getByRole('region', { name: 'Run environment' })
    await expect(latest).toContainText(`Run #${runCount}`)
    await expect(latest).toContainText('gpu-server-02')
    await expect(latest).toContainText('Assigned: 2 × NVIDIA A100 · 80 GiB · GPU 2, 3')
    await expect(latest).toContainText('CUDA_VISIBLE_DEVICES=2,3')
    await expect(latest).toContainText('/usr/bin/bash')
    const current = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: `Run #${runCount}` }) })
    await current.locator('summary').click()
    await expect(current).toContainText('333')
    await expect(current.getByRole('heading', { name: 'Environment', exact: true })).toHaveCount(0)
    await expect(panel.getByText('gpu-server-02', { exact: true })).toHaveCount(1)
    await expect(current.getByText('60.000s', { exact: true })).toHaveCount(1)
    await expect(current.getByText('(empty)', { exact: true })).toHaveCount(0)
    if (runCount > 1) {
      const second = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: 'Run #2' }) })
      await second.locator('summary').click()
      await expect(second).toContainText('gpu-server-01')
      await expect(second).not.toContainText('gpu-server-02')
      const first = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: 'Run #1' }) })
      await first.locator('summary').click()
      await expect(first).toContainText('Environment was not recorded for this run.')
      await expect(first).not.toContainText('gpu-server-02')
    }
    await latest.scrollIntoViewIfNeeded()
    await expect(page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).resolves.toBe(true)
    await page.screenshot({ path: testInfo.outputPath('run-environment.png') })
    expect(errors).toEqual([])
  })
}
