import { expect, test, type Page } from '@playwright/test'

async function fixture(page: Page) {
  await page.route('**/api/workspace', route => route.fulfill({ json: {
    run_root: '/shared/experiments/_pyruns_/_shell_', working_root: '/shared/experiments',
    tasks_dir: '/shared/experiments/tasks', workspace_kind: 'shell', workspace_ready: true,
    script_path: '', script_name: 'Shell', native_file_picker: false, settings: {}, templates: [],
  } }))
  const variants = ['library16', 'diverse', 'bounded', 'support2', 'feedback6']
  let tasks = Array.from({ length: 50 }, (_, index) => {
    const name = `ours_lamp5_scholarly_title_${variants[index % variants.length]}_2026-11-15_17-${String(index).padStart(2, '0')}-08`
    return {
      name, dir: `/shared/experiments/tasks/${name}`, task_identity: `task-${index}`,
      task_kind: 'shell', config_file: 'config.sh', config_text: 'python train.py',
      preview_text: `export CUDA_VISIBLE_DEVICES=${index % 4}\npython train.py --seed ${index} --dataset LaMP-5`,
      status: ['running', 'queued', 'pending', 'cancelled'][index] ?? (index % 8 === 0 ? 'failed' : 'completed'),
      created_at: `2026-11-15_17-${String(index).padStart(2, '0')}-09`,
      run_index: 1, start_times: [], finish_times: [], pids: [], records: [], tracks: [],
      config: {}, env: {}, notes: '', progress: 0, pinned: false, runner_host: index < 2 ? 'gpu-worker-02' : null,
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
  return { requests, mutations, first: tasks[0].name, second: tasks[1].name }
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
  const cardColors = () => cards.evaluateAll(elements => Object.fromEntries(elements.map(element => [
    element.getAttribute('data-task-status')!,
    {
      surface: getComputedStyle(element).backgroundColor,
      status: getComputedStyle(element.querySelector('.task-card-status-label')!).backgroundColor,
      frame: getComputedStyle(element, '::before').borderImageSource,
    },
  ] as const)))
  const lightColors = await cardColors()
  expect(new Set(Object.values(lightColors).map(({ surface }) => surface))).toEqual(new Set(['rgb(255, 255, 255)']))
  expect(new Set(Object.values(lightColors).map(({ status }) => status)).size).toBe(5)
  expect(lightColors.pending.status).toBe(lightColors.cancelled.status)
  const lightFrames = Object.values(lightColors).map(({ frame }) => frame)
  expect(new Set(lightFrames).size).toBe(6)
  expect(lightFrames.every(frame => frame.startsWith('linear-gradient('))).toBe(true)
  const runningCard = page.locator('[data-task-card][data-task-status="running"]')
  await expect(runningCard.getByText('Running', { exact: true })).toBeVisible()
  await expect(runningCard).toHaveCSS('box-shadow', /0px 2px 0px/)
  const frame = await runningCard.evaluate(element => {
    const style = getComputedStyle(element, '::before')
    return {
      widths: [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth],
      edges: [style.top, style.right, style.bottom, style.left],
      radius: style.borderRadius,
      pointerEvents: style.pointerEvents,
    }
  })
  expect(frame.widths).toEqual(['3px', '3px', '3px', '3px'])
  expect(frame.edges).toEqual(['0px', '0px', '0px', '0px'])
  expect(frame.radius).toBe('0px')
  expect(frame.pointerEvents).toBe('none')
  const pin = runningCard.getByRole('button', { name: `Pin ${state.first}`, exact: true })
  const unpin = runningCard.getByRole('button', { name: `Unpin ${state.first}`, exact: true })
  await expect(pin).toBeVisible()
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
    await page.setViewportSize({ width: 2560, height: 1400 })
    await page.screenshot({ path: testInfo.outputPath('manager-wide.png') })
    await page.setViewportSize({ width: 1600, height: 1000 })
  }
  await pin.click()
  await expect(runningCard).toHaveAttribute('data-task-card-pinned', 'true')
  await unpin.click()
  await expect(runningCard).toHaveAttribute('data-task-card-pinned', 'false')
  await page.getByRole('button', { name: 'Filter tasks: Active' }).click()
  await expect(page.locator('[data-task-card]')).toHaveCount(2)
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
  await expect(page.locator('[data-task-card]')).toHaveCount(1)
  await page.keyboard.press('Escape')
  await expect(options).toContainText('Queued')
  await page.getByRole('button', { name: 'Filter tasks: Active' }).click()
  await expect(page.locator('[data-task-card]')).toHaveCount(2)
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
  await expect.poll(cardColors).toEqual(lightColors)
  expect(await row.evaluate(element => getComputedStyle(element, '::before').borderTopWidth)).toBe('1px')
  await expect(row).not.toHaveCSS('box-shadow', /0px 2px 0px/)
  await page.screenshot({ path: testInfo.outputPath('manager-list.png'), animations: 'disabled' })
  if (!isMobile) {
    expect((await row.boundingBox())!.height).toBeLessThan(180)
    const name = (await row.locator('.task-card-name').boundingBox())!
    const state = (await row.locator('.task-card-status-label').boundingBox())!
    expect(state.x).toBeGreaterThan(name.x + name.width)
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
  await expect(runningCard.locator('.task-card-status-label')).toHaveCSS('background-color', lightColors.running.status)
  await expect(runningCard).toHaveClass(/\bring-accent\b/)
  await expect(runningCard).toHaveCSS('box-shadow', /0px 0px 0px 1px.*0px 2px 0px/)
  expect((await cardColors()).running.frame).toBe(lightColors.running.frame)
  await page.screenshot({ path: testInfo.outputPath('manager-selection.png') })
  await cancel.click()
  await page.getByRole('button', { name: 'List view', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('button', { name: 'List view', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: 'Dark Mode', exact: true }).click()
  await expect(runningCard).toHaveCSS('background-color', 'rgb(28, 31, 40)')
  const darkColors = await cardColors()
  expect(new Set(Object.values(darkColors).map(({ surface }) => surface))).toEqual(new Set(['rgb(28, 31, 40)']))
  expect(new Set(Object.values(darkColors).map(({ status }) => status)).size).toBe(5)
  expect(darkColors.pending.status).toBe(darkColors.cancelled.status)
  expect(darkColors.running.status).not.toBe(lightColors.running.status)
  expect(new Set(Object.values(darkColors).map(({ frame }) => frame)).size).toBe(6)
  for (const [status, colors] of Object.entries(lightColors)) {
    expect(darkColors[status].frame).not.toBe(colors.frame)
  }
  await page.screenshot({ path: testInfo.outputPath('manager-dark.png') })
  await page.getByRole('button', { name: 'Card view', exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath('manager-cards-dark.png') })
  await page.getByRole('button', { name: 'Switch to Chinese', exact: true }).click()
  await expect(page.getByRole('heading', { name: '任务管理', exact: true })).toBeVisible()
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
