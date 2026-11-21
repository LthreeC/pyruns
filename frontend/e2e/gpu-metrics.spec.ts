import { expect, test, type WebSocketRoute } from '@playwright/test'

test('GPU rules use free GiB and idle percent and retain fractional wait hours', async ({ page, isMobile }, testInfo) => {
  if (isMobile) await page.setViewportSize({ width: 320, height: 640 })
  await page.route('**/api/system/metrics**', route => route.fulfill({ json: {
    cpu_percent: 5, mem_percent: 10,
    gpus: [
      { id: 0, index: 0, name: 'GPU rule preview', uuid: 'GPU-0',
        util: 20, mem_used: 8192, mem_total: 24576, processes: [] },
      { id: 1, index: 1, name: 'GPU rule preview', uuid: 'GPU-1',
        util: 10, mem_used: 8192, mem_total: 24576, processes: [] },
    ],
  } }))
  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(await page.request.patch('/api/runtime?refresh_providers=false', { data: {
    gpu_scheduler: { enabled: false, selection_mode: 'auto', device_ids: [], respect_cuda_visible_devices: true },
  } })).toBeOK()
  await page.reload()
  await page.getByRole('button', { name: 'Runtime', exact: true }).click()
  const panel = page.getByRole('dialog', { name: 'Runtime settings' })
  await panel.getByRole('tab', { name: 'GPU', exact: true }).click()
  const panelBounds = (await panel.boundingBox())!
  const closeBounds = (await panel.getByRole('button', { name: 'Close runtime panel' }).boundingBox())!
  expect(closeBounds.x + closeBounds.width).toBeLessThanOrEqual(panelBounds.x + panelBounds.width)
  const enabled = panel.getByRole('switch', { name: 'GPU scheduling', exact: true })
  await expect(enabled).toHaveAttribute('aria-checked', 'false')
  const maximumWait = panel.getByLabel('Maximum wait', { exact: false })
  const freeMemory = panel.getByLabel('Minimum free memory', { exact: false })
  await expect(maximumWait).toBeVisible()
  await expect(maximumWait).toBeDisabled()
  await expect(freeMemory).toBeVisible()
  await expect(panel.getByRole('checkbox', { name: 'Require the same model for multi-GPU tasks' })).toBeVisible()
  await expect(panel.getByText('CUDA_VISIBLE_DEVICES', { exact: true })).toHaveCount(0)
  await expect(panel.getByRole('button', { name: 'Edit Env', exact: true })).toHaveCount(0)
  await expect(panel.getByText('Advanced scheduling rules', { exact: true })).toHaveCount(0)
  await expect(panel.getByRole('tab', { name: 'Env', exact: true })).toBeVisible()
  await enabled.click()
  await panel.getByLabel('GPUs required', { exact: false }).fill('2')
  await freeMemory.fill('8')
  await panel.getByLabel('Minimum GPU idle', { exact: false }).fill('70')
  await panel.getByLabel('Stable for', { exact: false }).fill('0')
  await maximumWait.fill('-1')
  await expect(maximumWait).toHaveValue('-1')
  await expect(maximumWait).toHaveAttribute('aria-invalid', 'true')
  await expect(panel.getByRole('button', { name: 'Save', exact: true })).toBeDisabled()
  await maximumWait.fill('0')
  await expect(maximumWait).toHaveValue('0')
  await expect(panel.getByRole('button', { name: 'Save', exact: true })).toBeEnabled()
  await maximumWait.fill('')
  await expect(maximumWait).toHaveValue('')
  await expect(panel.getByRole('button', { name: 'Save', exact: true })).toBeDisabled()
  await maximumWait.pressSequentially('0.25')
  await expect(maximumWait).toHaveValue('0.25')
  const preview = panel.getByRole('button', { name: /GPU 0 · GPU rule preview/ })
  await enabled.click()
  await expect(maximumWait).toBeVisible()
  await expect(maximumWait).toHaveValue('0.25')
  await expect(maximumWait).toBeDisabled()
  await expect(preview).toBeVisible()
  await enabled.click()
  await expect(maximumWait).toBeEnabled()
  await expect(maximumWait).toHaveValue('0.25')
  await expect(preview).toContainText('Pass')
  await panel.getByLabel('Minimum GPU idle', { exact: false }).fill('90')
  await expect(preview).toContainText('Blocked')
  await preview.click()
  await expect(panel.getByText('• GPU idle 80% is below 90%')).toBeVisible()
  await expect(panel.getByText('Maximum memory use', { exact: true })).toHaveCount(0)
  await expect(panel.getByText('Tasks per GPU', { exact: true })).toHaveCount(0)
  const saved = page.waitForResponse(response => response.url().includes('/api/runtime') && response.request().method() === 'PATCH')
  await panel.getByRole('button', { name: 'Save', exact: true }).click()
  const response = await saved
  expect(response.ok()).toBe(true)
  const runtime = await response.json()
  expect(runtime.gpu_scheduler.gpus_per_task).toBe(2)
  expect(runtime.gpu_scheduler.task_mode).toBe('multi')
  expect(runtime.gpu_scheduler.min_idle_pct).toBe(90)
  expect(runtime.gpu_scheduler.stable_seconds).toBe(0)
  expect(runtime.gpu_scheduler.max_wait_seconds).toBe(900)
  await expect(panel.getByLabel('Maximum wait', { exact: false })).toHaveValue('0.25')
  await page.screenshot({ path: testInfo.outputPath('gpu-fixed-units.png'), fullPage: true })
})

test('GPU rules preserve old policies until migrated to CUDA_VISIBLE_DEVICES', async ({ page }) => {
  await page.route('**/api/system/metrics**', route => route.fulfill({ json: {
    cpu_percent: 5, mem_percent: 10,
    gpus: [
      { id: 0, index: 0, name: 'Model A', uuid: 'GPU-0',
        util: 20, mem_used: 8192, mem_total: 24576, processes: [] },
      { id: 1, index: 1, name: 'Model B', uuid: 'GPU-1',
        util: 10, mem_used: 8192, mem_total: 24576, processes: [] },
    ],
  } }))
  await page.goto('/?token=pyruns-e2e-access-token')
  const runtimePath = '/api/runtime?refresh_providers=false'
  const seeded = await page.request.patch(runtimePath, { data: {
    global_env: { CUDA_VISIBLE_DEVICES: '0,1' },
    gpu_scheduler: {
      enabled: true, selection_mode: 'specified', device_ids: [0], gpus_per_task: 1,
      task_mode: 'single', min_free_memory_gb: 8, min_idle_pct: 70, respect_cuda_visible_devices: false,
    },
  } })
  await expect(seeded).toBeOK()
  await page.reload()
  await page.getByRole('button', { name: 'Runtime', exact: true }).click()
  const panel = page.getByRole('dialog', { name: 'Runtime settings' })
  await panel.getByRole('tab', { name: 'GPU', exact: true }).click()
  const migration = panel.getByRole('button', { name: 'Use CUDA_VISIBLE_DEVICES' })
  const save = panel.getByRole('button', { name: 'Save', exact: true })
  const waitForSave = () => page.waitForResponse(response => response.url().includes('/api/runtime') && response.request().method() === 'PATCH')
  await expect(panel.getByText('Fixed GPU indices 0', { exact: true })).toBeVisible()
  await expect(panel.getByText('Ignores CUDA_VISIBLE_DEVICES', { exact: true })).toBeVisible()
  await expect(panel.getByRole('button', { name: /GPU 0 · Model A/ })).toBeVisible()
  await expect(panel.getByRole('button', { name: /GPU 1 · Model B/ })).toHaveCount(0)
  const idle = panel.getByLabel('Minimum GPU idle', { exact: false })
  await idle.fill('60')
  await expect(idle).toHaveValue('60')
  await expect(idle).toBeFocused()
  const legacySave = waitForSave()
  await save.click()
  const legacy = await (await legacySave).json()
  expect(legacy.gpu_scheduler).toMatchObject({
    selection_mode: 'specified', device_ids: [0], respect_cuda_visible_devices: false, min_idle_pct: 60,
  })
  await expect(panel.getByText('GPU scheduler saved', { exact: true })).toBeVisible()

  const gpuTab = panel.getByRole('tab', { name: /^GPU/ })
  const envTab = panel.getByRole('tab', { name: /^Env/ })
  await gpuTab.press('ArrowLeft')
  await expect(envTab).toBeFocused()
  await expect(envTab).toHaveAttribute('aria-selected', 'true')
  const editor = panel.getByRole('textbox', { name: 'Workspace environment variables editor' })
  await expect(editor).toHaveText('CUDA_VISIBLE_DEVICES=0,1')
  await editor.fill('CUDA_VISIBLE_DEVICES=1,0')
  const envSave = waitForSave()
  await save.click()
  expect((await (await envSave).json()).global_env.CUDA_VISIBLE_DEVICES).toBe('1,0')
  await expect(panel.getByText('Workspace env saved', { exact: true })).toBeVisible()
  await gpuTab.click()
  await expect(panel.getByText('1,0', { exact: true })).toHaveCount(0)
  await migration.click()
  await expect(gpuTab).toBeFocused()
  await expect(migration).toBeHidden()
  await panel.getByLabel('GPUs required', { exact: false }).fill('2')
  await expect(panel.getByRole('button', { name: 'Specific indices', exact: true })).toHaveCount(0)
  await expect(panel.getByRole('checkbox', { name: /Respect.*CUDA_VISIBLE_DEVICES/ })).toHaveCount(0)
  const sameModel = panel.getByRole('checkbox', { name: 'Require the same model for multi-GPU tasks' })
  await sameModel.check()
  await expect(panel.getByText('Need 2 ready GPUs of the same model; the largest group has 1.', { exact: true })).toBeVisible()
  await expect(panel.getByText('Thresholds unmet', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: /GPU 0 · Model A/ }).click()
  await expect(panel.getByText('16 GiB free / 24 GiB total', { exact: true })).toBeVisible()
  await sameModel.uncheck()
  await expect(panel.getByText('Thresholds met', { exact: true })).toBeVisible()
  const saved = waitForSave()
  await save.click()
  const response = await saved
  expect(response.ok()).toBe(true)
  const runtime = await response.json()
  expect(runtime.gpu_scheduler).toMatchObject({
    selection_mode: 'auto', gpus_per_task: 2, device_ids: [], respect_cuda_visible_devices: true,
  })
  expect(runtime.global_env.CUDA_VISIBLE_DEVICES).toBe('1,0')
  await expect(await page.request.patch(runtimePath, { data: { global_env: {} } })).toBeOK()
})

test('GPU workspace fallback preserves legacy utilization limits when runtime loading fails', async ({ page }) => {
  await page.route('**/api/workspace', async route => {
    const response = await route.fetch()
    const workspace = await response.json()
    delete workspace.settings.gpu_scheduler_min_idle_pct
    Object.assign(workspace.settings, {
      gpu_scheduler_enabled: true,
      gpu_scheduler_task_mode: 'MULTI',
      gpu_scheduler_gpus_per_task: 2,
      gpu_scheduler_selection_mode: 'MANUAL',
      gpu_scheduler_device_ids: [0, 1],
      gpu_scheduler_compute_used_pct: 10,
      gpu_scheduler_max_wait_seconds: 900,
    })
    await route.fulfill({ json: workspace })
  })
  await page.route('**/api/runtime', route => route.fulfill({ status: 503, json: { detail: 'Runtime unavailable' } }))
  await page.goto('/?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'Runtime', exact: true }).click()
  const panel = page.getByRole('dialog', { name: 'Runtime settings' })
  await panel.getByRole('tab', { name: 'GPU', exact: true }).click()
  await expect(panel.getByLabel('GPUs required', { exact: false })).toHaveValue('2')
  await expect(panel.getByLabel('Minimum GPU idle', { exact: false })).toHaveValue('90')
  await expect(panel.getByLabel('Maximum wait', { exact: false })).toHaveValue('0.25')
  await expect(panel.getByText('Fixed GPU indices 0, 1', { exact: true })).toBeVisible()
  await expect(panel.getByRole('button', { name: 'Use CUDA_VISIBLE_DEVICES' })).toBeVisible()
  await panel.getByLabel('Minimum GPU idle', { exact: false }).fill('80')
  await panel.getByRole('button', { name: 'Reset', exact: true }).click()
  await expect(panel.getByLabel('Minimum GPU idle', { exact: false })).toHaveValue('90')
})

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

test('Monitor shows actual GPU wait details and refreshes them from task events', async ({ page }) => {
  const waitSnapshot = {
    state: 'waiting', run_index: 1, waited_seconds: 30, max_wait_seconds: 900,
    remaining_seconds: 870, requested_gpu_count: 2, eligible_gpu_count: 1, total_gpu_count: 2,
    reason: 'Need 2 eligible GPUs, only 1 available in CUDA_VISIBLE_DEVICES=2,3',
    devices: [
      { index: 2, name: 'Ready GPU', eligible: true, free_memory_gb: 16, compute_util_pct: 20 },
      { index: 3, name: 'Unknown GPU', eligible: false, reason: 'GPU 3 metrics unavailable or invalid',
        free_memory_gb: null, memory_used_pct: null, compute_util_pct: null },
    ],
  }
  let gpuWait: Record<string, unknown> | null = waitSnapshot
  let status = 'queued'
  const task = () => ({
    name: 'gpu-wait-test', status, task_kind: 'shell', run_index: 1,
    dir: '/experiments/gpu-wait-test', config: {}, config_text: 'python train.py',
    config_file: 'config.sh', preview_text: 'python train.py', pinned: false, notes: '', env: {},
    start_times: [], finish_times: [], pids: [], records: [], tracks: [], gpu_wait: gpuWait,
  })
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task()], total: 1, offset: 0, limit: 200, has_more: false,
    status_counts: { pending: 0, queued: status === 'queued' ? 1 : 0,
      running: status === 'running' ? 1 : 0, completed: 0, failed: 0, cancelled: 0 },
  } }))
  await page.route('**/api/tasks/gpu-wait-test?*', route => route.fulfill({ json: task() }))
  await page.route('**/api/tasks/gpu-wait-test/logs?*', route => route.fulfill({ json: {
    content: '', offset: 0, selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'run1.log',
  } }))
  let events: WebSocketRoute | undefined
  await page.routeWebSocket('**/api/tasks/events?*', socket => {
    events = socket
    socket.send(JSON.stringify({ type: 'ready' }))
  })
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const heading = page.getByRole('heading', { name: 'Waiting for GPU capacity', exact: true })
  await expect(heading).toBeVisible()
  const panel = page.getByRole('region', { name: 'Waiting for GPU capacity', exact: true })
  await expect(panel).toContainText('1/2 eligible')
  await expect(panel).toContainText('CUDA_VISIBLE_DEVICES=2,3')
  await expect(panel).toContainText('14m 30s')
  await expect(panel.locator('dl > div').filter({ hasText: 'Visible GPUs' })).toContainText('2')
  await panel.getByText('Why each GPU is waiting', { exact: true }).click()
  const unavailable = panel.getByRole('listitem').filter({ hasText: 'GPU 3' })
  await expect(unavailable).toContainText('metrics unavailable or invalid')
  await expect(unavailable).toContainText('Metrics unavailable')
  await expect(unavailable).not.toContainText('0%')
  expect(events).toBeDefined()
  gpuWait = null
  events!.send(JSON.stringify({ type: 'changed' }))
  await expect(heading).toHaveCount(0)
  gpuWait = { ...waitSnapshot, max_wait_seconds: 0, remaining_seconds: null }
  events!.send(JSON.stringify({ type: 'changed' }))
  await expect(heading).toBeVisible()
  await expect(panel).toContainText('No limit')
  status = 'running'
  gpuWait = null
  events!.send(JSON.stringify({ type: 'changed' }))
  await expect(heading).toHaveCount(0)
})
