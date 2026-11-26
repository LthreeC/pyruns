import { expect, test, type Locator, type Page } from '@playwright/test'

async function fixture(page: Page, taskCount = 50) {
  await page.route('**/api/workspace', route => route.fulfill({ json: {
    run_root: '/shared/experiments/_pyruns_/_shell_', working_root: '/shared/experiments',
    tasks_dir: '/shared/experiments/tasks', workspace_kind: 'shell', workspace_ready: true,
    script_path: '', script_name: 'Shell', native_file_picker: false, settings: {}, templates: [],
  } }))
  const variants = ['library16', 'diverse', 'bounded', 'support2', 'feedback6']
  const origins = [
    { runner_host: 'local-worker', runner_location: 'local', runner_lease_expired: false },
    { runner_host: 'gpu-worker-02', runner_location: 'remote', runner_lease_expired: true },
    { runner_host: 'local-worker', runner_location: 'local', runner_lease_expired: false },
    {},
    {},
    { runner_host: 'gpu-worker-03', runner_location: 'remote', runner_lease_expired: false },
    { task_storage: 'remote', task_source_host: 'gpu-worker-04', runner_location: 'remote' },
  ]
  let tasks = Array.from({ length: taskCount }, (_, index) => {
    const name = `ours_lamp5_scholarly_title_${variants[index % variants.length]}_2026-11-15_17-${String(index).padStart(2, '0')}-08`
    return {
      name, dir: `/shared/experiments/tasks/${name}`, task_identity: `task-${index}`,
      task_kind: 'shell', config_file: 'config.sh', config_text: 'python train.py',
      preview_text: `export CUDA_VISIBLE_DEVICES=${index % 4}\npython train.py --seed ${index} --dataset LaMP-5`,
      status: ['running', 'running', 'queued', 'pending', 'cancelled', 'running', 'queued'][index] ?? (index % 8 === 0 ? 'failed' : 'completed'),
      created_at: `2026-11-15_17-${String(index).padStart(2, '0')}-09`,
      run_index: 1, start_times: [], finish_times: [], pids: [], records: [], tracks: [],
      config: {}, env: {}, notes: '', progress: 0, pinned: false, task_storage: 'shared', ...origins[index],
    }
  })
  const requests: string[] = []
  const mutations: string[] = []
  await page.route('**/api/tasks?*', route => {
    const params = new URL(route.request().url()).searchParams
    const status = params.get('status') || 'All'
    const limit = Number(params.get('limit') ?? 50)
    const offset = Number(params.get('offset') ?? 0)
    requests.push(status)
    const counts = Object.fromEntries(['pending', 'queued', 'running', 'completed', 'failed', 'cancelled'].map(status => [status, tasks.filter(task => task.status === status).length]))
    const filtered = tasks.filter(task => (status === 'All' || (status === 'active' ? ['running', 'queued'].includes(task.status) : task.status === status))
      && task.name.includes(params.get('query') || ''))
    return route.fulfill({ json: { items: filtered.slice(offset, offset + limit), total: filtered.length, offset,
      limit, has_more: offset + limit < filtered.length, status_counts: counts } })
  })
  await page.route('**/api/tasks/reorder?*', route => {
    const items = route.request().postDataJSON().items
    mutations.push('reorder')
    tasks = items.map((item: { name: string; pinned: boolean }) => ({ ...tasks.find(task => task.name === item.name)!, pinned: item.pinned }))
    const includeItems = new URL(route.request().url()).searchParams.get('include_items') !== 'false'
    return route.fulfill({ json: { count: tasks.length, items: includeItems ? tasks : [] } })
  })
  await page.route('**/api/tasks/*/pin?*', route => {
    mutations.push('pin')
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3])
    tasks = tasks.map(task => task.name === name ? { ...task, pinned: !task.pinned } : task)
    return route.fulfill({ json: { ok: true, task: tasks.find(task => task.name === name) } })
  })
  await page.route('**/api/tasks/batch/delete', route => {
    const names = new Set<string>(route.request().postDataJSON().task_names)
    const deleted = tasks.filter(task => names.has(task.name)).map(task => task.name)
    tasks = tasks.filter(task => !names.has(task.name))
    mutations.push('delete')
    return route.fulfill({ json: { count: deleted.length, deleted } })
  })
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  return { requests, mutations, first: tasks[0].name, reported: tasks[1].name, remoteRunning: tasks[5].name, remoteQueued: tasks[6].name }
}

test('single-worker batch follows the visible pinned and unpinned order', async ({ page }) => {
  await fixture(page, 7)
  await page.route('**/api/tasks/batch/run?*', route => route.fulfill({ json: { count: 2, items: [], skipped: [] } }))
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const cards = page.locator('[data-task-card]')
  await expect(cards).toHaveCount(7)
  const unpinned = await cards.nth(3).getAttribute('data-task-card')
  const pinned = await cards.nth(4).getAttribute('data-task-card')
  await page.getByRole('button', { name: `Pin ${pinned}`, exact: true }).click()
  await expect(cards.first()).toHaveAttribute('data-task-card', pinned!)
  await page.getByRole('button', { name: 'Select tasks', exact: true }).click()
  await page.getByRole('button', { name: `Select ${unpinned}`, exact: true }).click()
  await page.getByRole('button', { name: `Select ${pinned}`, exact: true }).click()
  await page.getByRole('spinbutton', { name: 'Maximum parallel workers' }).fill('1')
  const request = page.waitForRequest('**/api/tasks/batch/run?*')
  await page.getByRole('button', { name: 'Run one at a time (2)', exact: true }).click()
  expect((await request).postDataJSON()).toEqual({ task_names: [pinned, unpinned], max_workers: 1 })
})

test('manager drag keeps the grid in place and animates the new order only when motion is enabled', async ({ page, isMobile }) => {
  test.skip(isMobile, 'Uses a mouse on a multi-column grid')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.addInitScript(() => {
    const animate = Element.prototype.animate
    const moved: string[] = []
    Object.assign(window, { movedTaskCards: moved })
    Element.prototype.animate = function (...args) {
      const name = this.getAttribute('data-task-card')
      if (name) moved.push(name)
      return animate.apply(this, args)
    }
  })
  const { first, reported } = await fixture(page)
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const cards = page.locator('[data-task-card]')
  const moved = () => page.evaluate(() => (window as Window & { movedTaskCards?: string[] }).movedTaskCards ?? [])
  const dragAfter = async (source: Locator, target: Locator, overIcon = false) => {
    await source.hover()
    const grip = (await source.locator('[data-task-drag-handle]').boundingBox())!
    const before = (await source.boundingBox())!
    const destination = (await target.boundingBox())!
    const icon = overIcon ? (await target.getByRole('button', { name: /^View logs for / }).boundingBox())! : null
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2)
    await page.mouse.down()
    await page.mouse.move(
      icon ? icon.x + icon.width / 2 : destination.x + destination.width * 0.75,
      icon ? icon.y + icon.height / 2 : destination.y + destination.height / 2,
      { steps: 8 },
    )
    await expect(page.getByText('Drop here to pin', { exact: true })).toBeVisible()
    expect((await source.boundingBox())!.y).toBe(before.y)
    await page.mouse.up()
  }
  await expect(cards.nth(1)).toHaveAttribute('data-task-card', reported)
  const response = page.waitForResponse(res => new URL(res.url()).pathname === '/api/tasks/reorder')
  await dragAfter(cards.nth(0), cards.nth(1), true)
  expect(await (await response).json()).toEqual({ count: 50, items: [] })
  await expect(cards.nth(1)).toHaveAttribute('data-task-card', first)
  await expect.poll(moved).toEqual(expect.arrayContaining([first, reported]))
  expect((await moved()).length).toBeLessThanOrEqual(4)

  await page.emulateMedia({ reducedMotion: 'reduce' })
  const previous = await moved()
  await dragAfter(cards.nth(0), cards.nth(1))
  await expect(cards.nth(0)).toHaveAttribute('data-task-card', first)
  expect(await moved()).toEqual(previous)
})

test('manager groups its toolbar and offers direct delete and keyboard drag', async ({ page, isMobile }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  if (!isMobile) await page.setViewportSize({ width: 1600, height: 1000 })
  else await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const state = await fixture(page)
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const cards = page.locator('[data-task-card]')
  await expect(cards).toHaveCount(50)
  const cardAppearance = () => cards.evaluateAll(elements => Object.fromEntries(elements.map(element => {
    const card = getComputedStyle(element)
    const label = getComputedStyle(element.querySelector('.task-card-status-label')!)
    const dot = getComputedStyle(element.querySelector('.task-status-dot')!)
    return [element.getAttribute('data-task-tone')!, {
      surface: card.backgroundColor,
      stripe: card.borderBlockStartColor,
      label: label.color,
      fill: dot.backgroundColor,
    }] as const
  })))
  const lightColors = await cardAppearance()
  expect(new Set(Object.values(lightColors).map(({ surface }) => surface))).toEqual(new Set(['rgb(255, 255, 255)']))
  expect(new Set(Object.values(lightColors).map(({ stripe }) => stripe)).size).toBe(6)
  for (const { fill, stripe } of Object.values(lightColors)) expect(fill).toBe(stripe)
  const runningCard = page.locator(`[data-task-card="${state.first}"]`)
  const reportedCard = page.locator(`[data-task-card="${state.reported}"]`)
  const remoteRunningCard = page.locator(`[data-task-card="${state.remoteRunning}"]`)
  const remoteQueuedCard = page.locator(`[data-task-card="${state.remoteQueued}"]`)
  await expect(runningCard.getByText('Running', { exact: true })).toBeVisible()
  await expect(reportedCard).toHaveAttribute('data-task-status', 'running')
  await expect(reportedCard.getByRole('button', { name: `Open details for ${state.reported}`, exact: true }))
    .toHaveAccessibleDescription('Last reported running')
  await expect(reportedCard.getByText('Runner status awaiting confirmation')).toHaveCSS('color', lightColors.running.label)
  await expect(remoteRunningCard.getByText('Remote reports running', { exact: true })).toBeVisible()
  await expect(remoteQueuedCard.getByText('Last reported queued', { exact: true })).toBeVisible()
  for (const common of [runningCard, reportedCard, remoteRunningCard]) {
    await expect(common).toHaveAttribute('data-task-tone', 'running')
    await expect(common).toHaveCSS('border-block-start-color', lightColors.running.stripe)
    await expect(common.locator('.task-status-dot')).toHaveCSS('border-top-style', 'solid')
    await expect(common.locator('.task-status-dot')).toHaveCSS('background-color', lightColors.running.fill)
    await expect(common.getByRole('button', { name: /^Pin / })).toBeEnabled()
  }
  await expect(runningCard.getByRole('button', { name: /^Stop / })).toBeEnabled()
  for (const remote of [reportedCard, remoteRunningCard, remoteQueuedCard]) {
    await expect(remote.getByRole('button', { name: /^Stop / })).toHaveCount(0)
    await expect(remote.getByRole('button', { name: /^Delete / })).toHaveCount(0)
  }
  await expect(reportedCard.getByText('Stop on gpu-worker-02', { exact: true })).toBeVisible()
  await expect(remoteRunningCard.getByText('Stop on gpu-worker-03', { exact: true })).toBeVisible()
  const otherDeviceDot = remoteQueuedCard.locator('.task-status-dot')
  await expect(remoteQueuedCard).toHaveCSS('border-block-start-color', lightColors.queued.stripe)
  await expect(otherDeviceDot).toHaveCSS('border-top-style', 'dashed')
  await expect(otherDeviceDot).toHaveCSS('border-top-width', '2px')
  await expect(otherDeviceDot).toHaveCSS('border-top-color', lightColors.queued.label)
  await expect(otherDeviceDot).toHaveCSS('background-color', lightColors.queued.fill)
  await expect(otherDeviceDot).toHaveCSS('background-clip', 'content-box')
  await expect(remoteQueuedCard.getByText('Stop on gpu-worker-04', { exact: true })).toBeVisible()
  await expect(remoteQueuedCard.getByRole('button', { name: /^Pin / })).toBeEnabled()
  await expect(runningCard).toHaveCSS('box-shadow', /0px 1px 1px/)
  await expect(runningCard).toHaveCSS('border-inline-start-width', '1px')
  await expect(runningCard).toHaveCSS('border-block-start-width', '3px')
  await expect(runningCard).toHaveCSS('border-radius', '8px')
  const preview = runningCard.locator('.task-card-preview')
  await expect(preview).toHaveAttribute('title', 'export CUDA_VISIBLE_DEVICES=0\npython train.py --seed 0 --dataset LaMP-5')
  expect((await preview.boundingBox())!.height).toBe(16)
  expect((await reportedCard.boundingBox())!.height).toBeLessThanOrEqual(isMobile ? 232 : 208)
  for (const card of [runningCard, reportedCard]) {
    const label = (await card.locator('.task-card-status-label').boundingBox())!
    const name = (await card.locator('.task-card-name').boundingBox())!
    expect(label.height).toBe(16)
    expect(label.y + label.height).toBeLessThanOrEqual(name.y)
    expect(label.x).toBe(name.x)
  }
  const pin = runningCard.getByRole('button', { name: `Pin ${state.first}`, exact: true })
  const unpin = runningCard.getByRole('button', { name: `Unpin ${state.first}`, exact: true })
  await expect(pin).toBeVisible()
  const pinBox = (await pin.boundingBox())!
  const nameBox = (await runningCard.locator('.task-card-name').boundingBox())!
  expect(pinBox.y + pinBox.height).toBeLessThanOrEqual(nameBox.y)
  if (isMobile) {
    const controls = await runningCard.locator('.task-card-actions button').evaluateAll(buttons =>
      buttons.map(button => {
        const { width, height } = button.getBoundingClientRect()
        return { width, height }
      }),
    )
    expect(controls).toHaveLength(4)
    for (const { width, height } of controls) {
      expect(width).toBeGreaterThanOrEqual(44)
      expect(height).toBeGreaterThanOrEqual(44)
    }
  }
  const status = page.getByRole('combobox', { name: 'Filter tasks by status' })
  const sort = page.getByRole('combobox', { name: 'Sort task cards' })
  await expect(status).toHaveValue('All')
  await expect(status.getByRole('option', { name: 'All tasks · 50', exact: true })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'List view', exact: true })).toHaveCount(0)
  const toolbar = page.locator('[data-tour="manager-controls"]')
  const [statusBox, sortBox] = await Promise.all([status.boundingBox(), sort.boundingBox()])
  expect(statusBox!.y).toBe(sortBox!.y)
  expect(sortBox!.x - statusBox!.x - statusBox!.width).toBeLessThanOrEqual(10)
  if (!isMobile) {
    const searchBox = (await page.getByRole('textbox', { name: 'Search tasks', exact: true }).boundingBox())!
    expect(Math.abs(searchBox.y + searchBox.height / 2 - statusBox!.y - statusBox!.height / 2)).toBeLessThanOrEqual(1)
  }
  await page.screenshot({ path: testInfo.outputPath('manager-cards.png') })
  await toolbar.screenshot({ path: testInfo.outputPath('manager-toolbar.png') })
  if (!isMobile) {
    const grid = (await page.locator('[data-task-grid]').boundingBox())!
    const secondRow = (await cards.nth(9).boundingBox())!
    await page.screenshot({
      path: testInfo.outputPath('manager-status-preview.png'),
      clip: { x: grid.x - 2, y: grid.y - 2, width: grid.width + 4, height: secondRow.y + secondRow.height - grid.y + 4 },
    })
    await page.setViewportSize({ width: 2560, height: 1400 })
    await page.screenshot({ path: testInfo.outputPath('manager-wide.png') })
    await page.setViewportSize({ width: 1600, height: 1000 })
  }
  await pin.click()
  await expect(runningCard).toHaveAttribute('data-task-card-pinned', 'true')
  await unpin.click()
  await expect(runningCard).toHaveAttribute('data-task-card-pinned', 'false')
  await status.selectOption('active')
  await expect(cards).toHaveCount(5)
  await status.selectOption('queued')
  await expect(cards).toHaveCount(2)
  await status.selectOption('active')
  await expect(cards).toHaveCount(5)
  expect(state.requests.at(-1)).toBe('active')
  const row = page.locator('[data-task-card]').filter({ has: page.getByRole('button', { name: `Open details for ${state.first}` }) })
  for (const label of [`Stop ${state.first}`, `View logs for ${state.first}`, `Delete ${state.first}`]) {
    await expect(row.getByRole('button', { name: label, exact: true })).toBeVisible()
  }
  await expect(row.getByRole('button', { name: /More actions|Move earlier|Move later/ })).toHaveCount(0)
  await expect(remoteQueuedCard.getByRole('button', { name: /^Delete / })).toHaveCount(0)
  const grip = row.getByRole('button', { name: `Reorder ${state.first}`, exact: true })
  await grip.press('Space')
  await expect(grip).toHaveAttribute('aria-pressed', 'true')
  await grip.press('ArrowRight')
  await expect(page.locator('footer').getByRole('status')).toContainText(`Drop after ${state.reported}`)
  await grip.press('Escape')
  await expect(grip).toHaveAttribute('aria-pressed', 'false')
  await expect(grip).toBeFocused()
  expect(state.mutations).not.toContain('reorder')
  await grip.press('Enter')
  await grip.press('ArrowRight')
  await grip.press('Enter')
  await expect.poll(() => state.mutations).toContain('reorder')
  await expect(sort).toHaveValue('manual')
  await row.getByRole('button', { name: `Delete ${state.first}`, exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Stop and Delete Task', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click()
  expect(state.mutations).not.toContain('delete')
  await status.selectOption('All')
  await expect(cards).toHaveCount(50)
  await page.mouse.move(0, 0)
  await expect.poll(cardAppearance).toEqual(lightColors)
  const select = page.getByRole('button', { name: 'Select tasks', exact: true })
  const position = await select.boundingBox()
  await select.click()
  const cancel = page.locator('.manager-heading').getByRole('button', { name: 'Cancel', exact: true })
  expect(await cancel.boundingBox()).toEqual(position)
  await page.getByRole('button', { name: `Select ${state.first}`, exact: true }).click()
  await expect(page.getByRole('button', { name: `Deselect ${state.first}`, exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.mouse.move(0, 0)
  await expect(runningCard).toHaveCSS('background-color', lightColors.running.surface)
  await expect(runningCard.locator('.task-status-dot')).toHaveCSS('background-color', lightColors.running.fill)
  await expect(runningCard).toHaveClass(/\bring-accent\b/)
  await expect(runningCard).toHaveCSS('box-shadow', /0px 0px 0px 1px.*0px 1px 1px/)
  await expect(runningCard).toHaveCSS('border-block-start-color', lightColors.running.stripe)
  await page.screenshot({ path: testInfo.outputPath('manager-selection.png') })
  await cancel.click()
  await page.reload()
  await page.getByRole('button', { name: 'Dark Mode', exact: true }).click()
  await expect(runningCard).toHaveCSS('background-color', 'rgb(28, 31, 40)')
  const darkColors = await cardAppearance()
  expect(new Set(Object.values(darkColors).map(({ surface }) => surface))).toEqual(new Set(['rgb(28, 31, 40)']))
  expect(new Set(Object.values(darkColors).map(({ stripe }) => stripe)).size).toBe(6)
  for (const [tone, colors] of Object.entries(lightColors)) {
    expect(darkColors[tone].label).not.toBe(colors.label)
    expect(darkColors[tone].fill).toBe(darkColors[tone].stripe)
  }
  await expect(reportedCard.getByText('Runner status awaiting confirmation')).toHaveCSS('color', darkColors.running.label)
  await page.screenshot({ path: testInfo.outputPath('manager-dark.png') })
  await page.screenshot({ path: testInfo.outputPath('manager-cards-dark.png') })
  await page.getByRole('button', { name: 'Switch to Chinese', exact: true }).click()
  await expect(page.getByRole('heading', { name: '任务管理', exact: true })).toBeVisible()
  await expect(reportedCard.getByText('上次报告为运行中', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('manager-zh.png') })
  await page.locator('[data-tour="manager-controls"]').screenshot({ path: testInfo.outputPath('manager-toolbar-dark-zh.png') })
  if (isMobile) {
    await page.setViewportSize({ width: 320, height: 640 })
    const { clientWidth, scrollWidth } = await runningCard.locator('.task-card-footer').evaluate(element => ({
      clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
    }))
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth)
    expect(await toolbar.evaluate(element => element.scrollWidth)).toBeLessThanOrEqual((await toolbar.boundingBox())!.width)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(errors).toEqual([])
})

test('manager fits cards to the viewport and loads a fixed page at a time', async ({ page }) => {
  await fixture(page, 51)
  await page.addInitScript(() => localStorage.setItem('pyruns_manager_cols', '1'))
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const cards = page.locator('[data-task-card]')
  await expect(cards).toHaveCount(50)
  expect((await cards.nth(0).boundingBox())!.y).toBe((await cards.nth(1).boundingBox())!.y)
  const footer = page.locator('footer').filter({ hasText: 'matching task(s)' })
  await expect(footer).toContainText('Showing 1–50 of 51 matching task(s)')

  await page.setViewportSize({ width: 375, height: 812 })
  expect((await cards.nth(1).boundingBox())!.y).toBeGreaterThan((await cards.nth(0).boundingBox())!.y)
  await footer.getByRole('button', { name: 'Next page', exact: true }).click()
  await expect(cards).toHaveCount(1)
  await expect(footer).toContainText('Showing 51–51 of 51 matching task(s)')
  await expect(footer.getByRole('button', { name: 'Next page', exact: true })).toBeDisabled()
  const taskName = await cards.first().getAttribute('data-task-card')
  await cards.first().getByRole('button', { name: `Delete ${taskName}`, exact: true }).click()
  await page.getByRole('dialog', { name: 'Delete Task', exact: true }).getByRole('button', { name: 'Delete', exact: true }).click()
  await expect(cards).toHaveCount(50)
  await expect(footer).toContainText('Showing 1–50 of 50 matching task(s)')
})
