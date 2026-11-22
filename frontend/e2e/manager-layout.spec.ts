import { expect, test, type Page } from '@playwright/test'

async function fixture(page: Page) {
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
  let tasks = Array.from({ length: 50 }, (_, index) => {
    const name = `ours_lamp5_scholarly_title_${variants[index % variants.length]}_2026-11-15_17-${String(index).padStart(2, '0')}-08`
    return {
      name, dir: `/shared/experiments/tasks/${name}`, task_identity: `task-${index}`,
      task_kind: 'shell', config_file: 'config.sh', config_text: 'python train.py',
      preview_text: `export CUDA_VISIBLE_DEVICES=${index % 4}\npython train.py --seed ${index} --dataset LaMP-5`,
      status: ['running', 'running', 'queued', 'pending', 'cancelled', 'running', 'queued'][index] ?? (index % 8 === 0 ? 'failed' : 'completed'),
      created_at: `2026-11-15_17-${String(index).padStart(2, '0')}-09`,
      run_index: 1, start_times: [], finish_times: [], pids: [], records: [], tracks: [],
      config: {}, env: {}, notes: '', progress: 0, pinned: false, ...origins[index],
    }
  })
  const requests: string[] = []
  const mutations: string[] = []
  await page.route('**/api/tasks?*', route => {
    const params = new URL(route.request().url()).searchParams
    const status = params.get('status') || 'All'
    requests.push(status)
    const counts = Object.fromEntries(['pending', 'queued', 'running', 'completed', 'failed', 'cancelled'].map(status => [status, tasks.filter(task => task.status === status).length]))
    const filtered = tasks.filter(task => (status === 'All' || (status === 'active' ? ['running', 'queued'].includes(task.status) : task.status === status))
      && task.name.includes(params.get('query') || ''))
    return route.fulfill({ json: { items: filtered, total: filtered.length, offset: 0, limit: 50, has_more: false, status_counts: counts } })
  })
  await page.route('**/api/tasks/reorder?*', route => {
    const items = route.request().postDataJSON().items
    mutations.push('reorder')
    tasks = items.map((item: { name: string; pinned: boolean }) => ({ ...tasks.find(task => task.name === item.name)!, pinned: item.pinned }))
    return route.fulfill({ json: { count: tasks.length, items: tasks } })
  })
  await page.route('**/api/tasks/*/pin?*', route => {
    mutations.push('pin')
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/')[3])
    tasks = tasks.map(task => task.name === name ? { ...task, pinned: !task.pinned } : task)
    return route.fulfill({ json: { ok: true, task: tasks.find(task => task.name === name) } })
  })
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  return { requests, mutations, first: tasks[0].name, reported: tasks[1].name, remoteRunning: tasks[5].name, remoteQueued: tasks[6].name }
}

test('manager combines quick filters with persistent views and keeps every task action', async ({ page, isMobile }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  if (!isMobile) await page.setViewportSize({ width: 1600, height: 1000 })
  else await page.setViewportSize({ width: 375, height: 812 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const state = await fixture(page)
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const cards = page.locator('[data-task-card]')
  await expect(cards).toHaveCount(50)
  const cardAppearance = (list = false) => cards.evaluateAll((elements, list) => Object.fromEntries(elements.map(element => {
    const card = getComputedStyle(element)
    const label = getComputedStyle(element.querySelector('.task-card-status-label')!)
    const icon = element.querySelector('.task-card-status-icon')!
    return [element.getAttribute('data-task-tone')!, {
      surface: card.backgroundColor,
      stripe: list ? card.borderInlineStartColor : card.borderBlockStartColor,
      label: label.color,
      glyph: [...icon.classList].find(name => name.startsWith('lucide-')),
    }] as const
  })), list)
  const lightColors = await cardAppearance()
  expect(new Set(Object.values(lightColors).map(({ surface }) => surface))).toEqual(new Set(['rgb(255, 255, 255)']))
  expect(new Set(Object.values(lightColors).map(({ stripe }) => stripe)).size).toBe(7)
  expect(Object.fromEntries(Object.entries(lightColors).map(([tone, { glyph }]) => [tone, glyph]))).toEqual({
    running: 'lucide-activity', reported: 'lucide-circle-dashed', queued: 'lucide-clock3',
    completed: 'lucide-check', failed: 'lucide-x', pending: 'lucide-circle', cancelled: 'lucide-ban',
  })
  const runningCard = page.locator(`[data-task-card="${state.first}"]`)
  const reportedCard = page.locator(`[data-task-card="${state.reported}"]`)
  const remoteRunningCard = page.locator(`[data-task-card="${state.remoteRunning}"]`)
  const remoteQueuedCard = page.locator(`[data-task-card="${state.remoteQueued}"]`)
  await expect(runningCard.getByText('Running', { exact: true })).toBeVisible()
  await expect(reportedCard).toHaveAttribute('data-task-status', 'running')
  await expect(reportedCard.getByRole('button', { name: `Open details for ${state.reported}`, exact: true }))
    .toHaveAccessibleDescription('Last reported running')
  await expect(reportedCard.getByText('Runner status awaiting confirmation')).toHaveCSS('color', lightColors.reported.label)
  await expect(remoteRunningCard.getByText('Remote reports running', { exact: true })).toBeVisible()
  await expect(remoteRunningCard).toHaveAttribute('data-task-tone', 'reported')
  await expect(remoteQueuedCard.getByText('Last reported queued', { exact: true })).toBeVisible()
  await expect(remoteQueuedCard).toHaveAttribute('data-task-tone', 'reported')
  await expect(runningCard).toHaveCSS('box-shadow', /0px 1px 1px/)
  await expect(runningCard).toHaveCSS('border-inline-start-width', '1px')
  await expect(runningCard).toHaveCSS('border-block-start-width', '5px')
  await expect(runningCard).toHaveCSS('border-radius', '0px')
  const preview = runningCard.locator('.task-card-preview')
  await expect(preview).toHaveAttribute('title', 'export CUDA_VISIBLE_DEVICES=0\npython train.py --seed 0 --dataset LaMP-5')
  expect((await preview.boundingBox())!.height).toBe(16)
  expect((await reportedCard.boundingBox())!.height).toBeLessThanOrEqual(isMobile ? 232 : 208)
  for (const card of [runningCard, reportedCard]) {
    const label = (await card.locator('.task-card-status-label').boundingBox())!
    const name = (await card.locator('.task-card-name').boundingBox())!
    expect(label.height).toBe(18)
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
  await expect(page.getByRole('button', { name: 'Filter tasks: All tasks' })).toHaveAttribute('aria-pressed', 'true')
  await page.screenshot({ path: testInfo.outputPath('manager-cards.png') })
  await page.locator('[data-tour="manager-controls"]').screenshot({ path: testInfo.outputPath('manager-toolbar.png') })
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
  await page.getByRole('button', { name: 'Filter tasks: Active' }).click()
  await expect(cards).toHaveCount(5)
  const options = page.getByRole('button', { name: 'View options', exact: true })
  await options.click()
  await expect(page.getByRole('combobox', { name: 'Filter tasks by status' })).toHaveValue('active')
  const sort = page.getByRole('combobox', { name: 'Sort task cards' })
  await expect(sort).toBeFocused()
  await expect(sort).toHaveAccessibleDescription(/Pinned tasks always appear first.*Running and queued/)
  await page.getByRole('combobox', { name: 'Maximum cards per row' }).selectOption('4')
  await page.screenshot({ path: testInfo.outputPath('manager-options.png') })
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'View options' })).toBeHidden()
  await expect(options).toBeFocused()
  await options.click()
  await page.getByRole('combobox', { name: 'Filter tasks by status' }).selectOption('queued')
  await expect(cards).toHaveCount(2)
  await page.keyboard.press('Escape')
  await expect(options).toContainText('Queued')
  await page.getByRole('button', { name: 'Filter tasks: Active' }).click()
  await expect(cards).toHaveCount(5)
  expect(state.requests.at(-1)).toBe('active')
  const all = page.getByRole('button', { name: 'Filter tasks: All tasks' })
  await expect(all).toContainText('50')
  await expect(all).toHaveAccessibleDescription('50')
  await page.getByRole('button', { name: 'List view', exact: true }).click()
  await expect(page.getByRole('button', { name: 'List view', exact: true })).toHaveAttribute('aria-pressed', 'true')
  const row = page.locator('[data-task-card]').filter({ has: page.getByRole('button', { name: `Open details for ${state.first}` }) })
  for (const label of [`Stop ${state.first}`, `View logs for ${state.first}`]) {
    await expect(row.getByRole('button', { name: label, exact: true })).toBeVisible()
  }
  const more = row.getByRole('button', { name: `More actions for ${state.first}`, exact: true })
  const menu = page.getByRole('group', { name: `Task actions for ${state.first}`, exact: true })
  await expect(menu).toBeHidden()
  await more.press('Enter')
  await expect(menu.getByRole('button', { name: 'Move earlier', exact: true })).toBeFocused()
  await expect(menu.getByRole('button', { name: 'Delete task', exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(menu).toBeHidden()
  await expect(more).toBeFocused()
  await more.click()
  await menu.getByRole('button', { name: 'Move later', exact: true }).click()
  await expect.poll(() => state.mutations).toContain('reorder')
  await pin.click()
  await expect(row).toHaveAttribute('data-task-card-pinned', 'true')
  await expect(unpin).toBeVisible()
  await more.click()
  await expect(menu).toBeVisible()
  await page.getByRole('heading', { name: 'Task Manager', exact: true }).click()
  await expect(menu).toBeHidden()
  await more.click()
  await menu.getByRole('button', { name: 'Delete task', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Stop and Delete Task', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Close dialog', exact: true }).click()
  await all.click()
  await expect(page.locator('[data-task-card]')).toHaveCount(50)
  await page.mouse.move(0, 0)
  await expect.poll(() => cardAppearance(true)).toEqual(lightColors)
  await expect(row).toHaveCSS('border-inline-start-width', '4px')
  await expect(row.locator('.task-card-preview .truncate-2')).toHaveCSS('-webkit-line-clamp', '2')
  await expect(row).not.toHaveCSS('box-shadow', /0px 1px 1px/)
  await page.screenshot({ path: testInfo.outputPath('manager-list.png'), animations: 'disabled' })
  if (!isMobile) {
    expect((await row.boundingBox())!.height).toBeLessThan(180)
    const name = (await row.locator('.task-card-name').boundingBox())!
    const state = (await row.locator('.task-card-status-label').boundingBox())!
    expect(state.x).toBeGreaterThan(name.x + name.width)
    expect((await remoteQueuedCard.locator('.task-card-status-label').boundingBox())!.x).toBe(state.x)
  }
  const select = page.getByRole('button', { name: 'Select tasks', exact: true })
  const position = await select.boundingBox()
  await select.click()
  const cancel = page.locator('.manager-heading').getByRole('button', { name: 'Cancel', exact: true })
  expect(await cancel.boundingBox()).toEqual(position)
  await page.getByRole('button', { name: `Select ${state.first}`, exact: true }).click()
  await page.getByRole('button', { name: 'Card view', exact: true }).click()
  await expect(page.getByRole('button', { name: `Deselect ${state.first}`, exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(runningCard).toHaveCSS('background-color', lightColors.running.surface)
  await expect(runningCard.locator('.task-card-status-icon')).toHaveCSS('color', lightColors.running.label)
  await expect(runningCard).toHaveClass(/\bring-accent\b/)
  await expect(runningCard).toHaveCSS('box-shadow', /0px 0px 0px 1px.*0px 1px 1px/)
  await expect(runningCard).toHaveCSS('border-block-start-color', lightColors.running.stripe)
  await page.screenshot({ path: testInfo.outputPath('manager-selection.png') })
  await cancel.click()
  await page.getByRole('button', { name: 'List view', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('button', { name: 'List view', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: 'Dark Mode', exact: true }).click()
  await expect(runningCard).toHaveCSS('background-color', 'rgb(28, 31, 40)')
  const darkColors = await cardAppearance(true)
  expect(new Set(Object.values(darkColors).map(({ surface }) => surface))).toEqual(new Set(['rgb(28, 31, 40)']))
  expect(new Set(Object.values(darkColors).map(({ stripe }) => stripe)).size).toBe(7)
  for (const [tone, colors] of Object.entries(lightColors)) {
    expect(darkColors[tone].label).not.toBe(colors.label)
    expect(darkColors[tone].glyph).toBe(colors.glyph)
  }
  await expect(reportedCard.getByText('Runner status awaiting confirmation')).toHaveCSS('color', darkColors.reported.label)
  await page.screenshot({ path: testInfo.outputPath('manager-dark.png') })
  await page.getByRole('button', { name: 'Card view', exact: true }).click()
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
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(errors).toEqual([])
})
