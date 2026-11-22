import { expect, test, type Locator, type Page } from '@playwright/test'

const taskName = 'controlled-memory-comparison-with-a-long-experiment-name_2026-10-16_12-01-36'

async function mockWorkspace(page: Page, taskCount = 1) {
  const workspace = {
    run_root: '/experiments/memory/_pyruns_', working_root: '/experiments/memory',
    tasks_dir: '/experiments/memory/_pyruns_/tasks', workspace_kind: 'shell',
    workspace_ready: true, script_path: '', script_name: 'Shell',
    native_file_picker: false, settings: {}, templates: [],
  }
  const task = {
    name: taskName, dir: `/experiments/memory/${taskName}`, status: 'completed',
    task_kind: 'shell', run_index: 2, created_at: '2026-10-16_12-01-36',
    config: {}, config_text: 'printf "recorded command\\n"', config_file: 'config.sh',
    preview_text: '', pinned: false, notes: '', env: {}, progress: 100,
    start_times: [], finish_times: [], pids: [], records: [], tracks: [],
  }
  const tasks = Array.from({ length: taskCount }, (_, index) => index === 0 ? task : {
    ...task, name: `layout-task-${index}`, dir: `/experiments/memory/layout-task-${index}`,
  })
  const summary = { total: 9, pending: 0, queued: 0, running: 0, completed: 8, failed: 1, cancelled: 0 }
  await page.addInitScript(() => localStorage.setItem('pyruns_language', 'en'))
  await page.route('**/api/workspace', route => route.fulfill({ json: workspace }))
  await page.route('**/api/dashboard?*', route => route.fulfill({ json: {
    workspace, summary, recent_tasks: [task], template_count: 0, active_task: null,
  } }))
  await page.route('**/api/system/metrics**', route => route.fulfill({ json: {
    cpu_percent: 11, mem_percent: 24,
    gpus: Array.from({ length: 6 }, (_, index) => ({
      id: index, index, uuid: `GPU-${index}`, name: 'NVIDIA A100-PCIE-40GB',
      util: index * 18, mem_used: index * 5120, mem_total: 40960, processes: [],
    })),
  } }))
  await page.route('**/api/tasks?*', route => {
    const params = new URL(route.request().url()).searchParams
    const offset = Number(params.get('offset') || 0)
    const limit = Number(params.get('limit') || 200)
    return route.fulfill({ json: {
      items: tasks.slice(offset, offset + limit), total: taskCount, offset, limit,
      has_more: offset + limit < taskCount,
      status_counts: { pending: 0, queued: 0, running: 0, completed: taskCount, failed: 0, cancelled: 0 },
    } })
  })
  await page.route(`**/api/tasks/${taskName}?*`, route => route.fulfill({ json: task }))
  await page.route(`**/api/tasks/${taskName}/logs?*`, route => {
    const selected = new URL(route.request().url()).searchParams.get('log_file_name') || 'run2.log'
    return route.fulfill({ json: {
      task_name: taskName, content: 'recorded command\n', offset: 17,
      selected_log: selected, available_logs: ['run1.log', 'run2.log'], log_identity: selected,
    } })
  })
  await page.routeWebSocket('**/api/tasks/events?*', socket => {
    socket.send(JSON.stringify({ type: 'ready' }))
  })
}

async function expectInsideViewport(page: Page, locator: Locator) {
  await expect(locator).toBeVisible()
  const box = await locator.boundingBox()
  expect(box).not.toBeNull()
  const viewport = page.viewportSize()!
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.y).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width + 1)
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height + 1)
}

async function expectCompactTaskRow(page: Page) {
  const row = page.getByRole('button').filter({ hasText: taskName })
  await row.scrollIntoViewIfNeeded()
  await expectInsideViewport(page, row)
  expect((await row.boundingBox())!.height).toBeLessThanOrEqual(44)
  const layout = await row.evaluate(element => {
    const rowBox = element.getBoundingClientRect()
    const children = Array.from(element.children).filter(child => getComputedStyle(child).display !== 'none')
    return {
      rowRight: rowBox.right,
      children: children.map(child => {
        const box = child.getBoundingClientRect()
        return { centerY: box.y + box.height / 2, right: box.right, width: box.width }
      }),
    }
  })
  const centers = layout.children.map(child => child.centerY)
  expect(Math.max(...centers) - Math.min(...centers)).toBeLessThanOrEqual(1)
  expect(layout.children[1].width).toBeGreaterThan(0)
  // The chevron stays at the trailing edge when the timestamp is hidden.
  expect(layout.rowRight - layout.children.at(-1)!.right).toBeLessThanOrEqual(10)
}

async function expectCompactManagerFooter(page: Page) {
  const footer = page.locator('footer').filter({ hasText: 'matching task(s)' })
  await expectInsideViewport(page, footer)
  const summary = footer.locator(':scope > span')
  const next = footer.getByRole('button', { name: 'Next page', exact: true })
  await expect(next).toBeVisible()
  const summaryBox = (await summary.boundingBox())!
  const nextBox = (await next.boundingBox())!
  expect(Math.abs(summaryBox.y + summaryBox.height / 2 - nextBox.y - nextBox.height / 2)).toBeLessThanOrEqual(1)
  expect((await footer.boundingBox())!.height).toBeLessThanOrEqual(61)
}

test('desktop keeps six GPUs and recent work compact without crowding log actions', async ({ page, isMobile }, testInfo) => {
  test.skip(isMobile, 'Desktop density is checked with a fine pointer.')
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockWorkspace(page, 201)
  await page.goto('/?token=pyruns-e2e-access-token')

  const gpus = page.getByRole('button', { name: /^View details for GPU \d / })
  await expect(gpus).toHaveCount(6)
  const gpuSurface = await gpus.first().evaluate(element => getComputedStyle(element).backgroundColor)
  const gpuChannels = gpuSurface.match(/\d+/g)?.map(Number) ?? []
  expect(gpuChannels).toHaveLength(3)
  // GPU telemetry uses a pale, low-saturation surface in the light theme.
  // Keep this guard broad enough for theme tuning while preventing a return to
  // dark/navy cards that disappear against the white dashboard.
  expect(Math.min(...gpuChannels)).toBeGreaterThanOrEqual(220)
  expect(Math.max(...gpuChannels) - Math.min(...gpuChannels)).toBeLessThanOrEqual(28)
  for (const gpu of await gpus.all()) {
    const box = await gpu.boundingBox()
    expect(box!.height).toBeLessThanOrEqual(100)
  }
  const recentTask = page.getByRole('button').filter({ hasText: taskName })
  await expectCompactTaskRow(page)
  expect((await recentTask.boundingBox())!.y).toBeLessThanOrEqual(500)
  await page.screenshot({ path: testInfo.outputPath('compact-dashboard-desktop.png') })

  await page.setViewportSize({ width: 768, height: 900 })
  await expectCompactTaskRow(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('compact-dashboard-tablet.png') })
  await page.setViewportSize({ width: 1440, height: 900 })

  await recentTask.click()
  const actions = page.getByRole('toolbar', { name: 'Log actions' })
  await expect(actions).toBeVisible()
  // Measure the common task context and action container, without depending on CSS utilities.
  const header = actions.locator('xpath=../..')
  expect((await header.boundingBox())!.height).toBeLessThanOrEqual(48)
  for (const action of [
    page.getByRole('combobox', { name: 'Select task log file' }),
    actions.getByRole('button', { name: 'View Details', exact: true }),
    actions.getByRole('button', { name: 'Copy current log', exact: true }),
    actions.getByRole('button', { name: 'Run', exact: true }),
  ]) await expectInsideViewport(page, action)
  const pagination = page.getByLabel('Monitor task pagination', { exact: true })
  const footer = pagination.locator('xpath=..')
  expect((await pagination.boundingBox())!.height).toBeLessThanOrEqual(32)
  expect((await footer.boundingBox())!.height).toBeLessThanOrEqual(88)
  await expectInsideViewport(page, footer.getByRole('button', { name: 'Export', exact: true }))
  await pagination.getByRole('button', { name: 'Next page', exact: true }).click()
  await expect(pagination).toContainText('201')
  await expect(pagination.getByRole('button', { name: 'Previous page', exact: true })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'View layout-task-200, completed', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('compact-monitor-desktop.png') })

  await page.getByRole('link', { name: 'Manager', exact: true }).click()
  await expectCompactManagerFooter(page)
  await page.screenshot({ path: testInfo.outputPath('compact-manager-desktop.png') })
  await page.setViewportSize({ width: 768, height: 900 })
  await expectCompactManagerFooter(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('compact-manager-tablet.png') })
})

test('375px layout keeps GPU details and log controls reachable without horizontal overflow', async ({ page, isMobile }, testInfo) => {
  test.skip(!isMobile, 'The narrow layout is checked with touch input.')
  await page.setViewportSize({ width: 375, height: 812 })
  await mockWorkspace(page, 201)
  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Refresh dashboard', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Start New Task', exact: true })).toHaveCount(0)
  await expectCompactTaskRow(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('button', { name: 'View details for GPU 0 NVIDIA A100-PCIE-40GB' }).click()
  const gpuDialog = page.getByRole('dialog', { name: 'GPU 0 | NVIDIA A100-PCIE-40GB' })
  await expect(gpuDialog).toBeVisible()
  await gpuDialog.getByRole('button', { name: 'Close GPU details' }).click()
  const gpuRow = page.getByRole('button', { name: 'View details for GPU 0 NVIDIA A100-PCIE-40GB' })
  const usageTracks = gpuRow.locator('.gpu-usage-track')
  await expect(usageTracks).toHaveCount(2)
  for (const track of await usageTracks.all()) {
    const box = await track.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThan(120)
    expect(box!.x + box!.width).toBeLessThanOrEqual(375)
  }
  await page.screenshot({ path: testInfo.outputPath('compact-dashboard-mobile.png') })

  await page.getByRole('link', { name: 'Monitor', exact: true }).click()
  const selectLog = page.getByRole('combobox', { name: 'Select task log file' })
  await expectInsideViewport(page, selectLog)
  await selectLog.selectOption('run1.log')
  await expect(selectLog).toHaveValue('run1.log')
  const details = page.getByRole('button', { name: 'View Details', exact: true })
  await expectInsideViewport(page, details)
  await details.click()
  await expect(page.getByRole('dialog', { name: `Task details for ${taskName}`, exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('compact-monitor-mobile.png') })
  await page.getByRole('dialog', { name: `Task details for ${taskName}`, exact: true }).getByRole('button', { name: 'Close task details' }).click()
  await page.getByRole('link', { name: 'Manager', exact: true }).click()
  await expectCompactManagerFooter(page)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('compact-manager-mobile.png') })
})
