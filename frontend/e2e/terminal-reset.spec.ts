import { expect, test } from '@playwright/test'

test('monitor releases terminal window listeners when leaving the page', async ({ page }) => {
  const task = { name: 'terminal-lifecycle', status: 'completed', run_index: 1, task_kind: 'shell' }
  await page.route('**/api/tasks?*', route => {
    const monitoring = new URL(route.request().url()).searchParams.get('limit') === '200'
    return route.fulfill({ json: {
      items: monitoring ? [task] : [], total: monitoring ? 1 : 0,
      offset: 0, limit: monitoring ? 200 : 50, has_more: false,
    } })
  })
  await page.route('**/api/tasks/terminal-lifecycle?*', route => route.fulfill({ json: task }))
  await page.route('**/api/tasks/terminal-lifecycle/logs?*', route => route.fulfill({ json: {
    selected_log: 'run1.log', available_logs: ['run1.log'],
    content: 'Lifecycle output\n', offset: 17, log_identity: 'lifecycle',
  } }))
  const cdp = await page.context().newCDPSession(page)
  const resizeListeners = async () => (await cdp.send('Runtime.evaluate', {
    expression: '(getEventListeners(window).resize || []).length',
    includeCommandLineAPI: true, returnByValue: true,
  })).result.value as number
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await expect(page.getByRole('link', { name: 'Monitor', exact: true })).toBeVisible()
  const baseline = await resizeListeners()
  for (let visit = 0; visit < 3; visit++) {
    await page.getByRole('link', { name: 'Monitor', exact: true }).click()
    await expect(page.locator('.xterm-rows')).toContainText('Lifecycle output')
    await page.getByRole('link', { name: 'Manager', exact: true }).click()
    await expect(page.locator('.xterm')).toHaveCount(0)
    await expect.poll(resizeListeners).toBe(baseline)
  }
})

test('monitor loads a bounded tail and expands history for the selected log', async ({ page }) => {
  const tasks = ['long-history', 'short-history', 'byte-limited'].map(name => ({
    name, status: 'completed', run_index: 1, task_kind: 'shell',
  }))
  const requested: { bytes: string | null; lines: number }[] = []
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: tasks, total: tasks.length, offset: 0, limit: 200, has_more: false,
  } }))
  for (const task of tasks) {
    await page.route(`**/api/tasks/${task.name}/logs?*`, route => {
      const params = new URL(route.request().url()).searchParams
      const lines = Number(params.get('tail_lines'))
      const long = task.name === 'long-history'
      const byteLimited = task.name === 'byte-limited'
      if (long) requested.push({ bytes: params.get('tail_bytes'), lines })
      return route.fulfill({ json: {
        selected_log: 'run1.log', available_logs: ['run1.log'],
        content: !long ? 'Short log\n' : lines === 10_000 ? 'Older lines\nLatest lines\n' : 'Latest lines\n',
        offset: long ? 400_000 : 10, log_identity: task.name,
        tail_truncated: long || byteLimited, tail_byte_limited: byteLimited,
      } })
    })
  }

  await page.goto('/monitor?task=long-history&token=pyruns-e2e-access-token')
  const rows = page.locator('.xterm-rows')
  const expand = page.getByRole('button', { name: 'Load more history' })
  await expect(rows).toContainText('Latest lines')
  await expect(page.getByText('Showing up to 1,000 recent log lines.')).toBeVisible()
  await expect(rows).not.toContainText('Older lines')
  await expand.click()
  await expect(rows).toContainText('Older lines')
  await expect(expand).toBeHidden()
  expect(requested).toEqual([
    { bytes: null, lines: 1_000 },
    { bytes: null, lines: 10_000 },
  ])

  await page.getByRole('button', { name: 'View short-history, completed', exact: true }).click()
  await expect(rows).toContainText('Short log')
  await expect(rows).not.toContainText('Older lines')
  await expect(expand).toBeHidden()

  await page.getByRole('button', { name: 'View byte-limited, completed', exact: true }).click()
  await expect(page.getByText('Showing the end of a large log.')).toBeVisible()
  await expect(expand).toBeHidden()
})

test('monitor shows the latest progress redraw and keeps the original log available to copy', async ({ page }) => {
  const task = { name: 'progress-task', status: 'completed', run_index: 1, task_kind: 'shell' }
  const frames = Array.from({ length: 80 }, (_, index) => (
    ('Progress stage ' + String(index).padStart(4, '0')).padEnd(159, '█')
  ))
  const content = 'Started\r\n\r' + frames.join('\r') + '\r\nFinished\r\n'
  let copiedLog = ''
  await page.exposeFunction('captureCopiedLog', (value: string) => { copiedLog = value })
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: {
      writeText: (text: string) => (window as Window & { captureCopiedLog: (value: string) => Promise<void> }).captureCopiedLog(text),
    } })
  })
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 200, has_more: false,
  } }))
  await page.route('**/api/tasks/progress-task/logs?*', route => route.fulfill({ json: {
    selected_log: 'run1.log', available_logs: ['run1.log'], content, offset: 34_000, log_identity: 'progress',
  } }))

  await page.goto('/monitor?task=progress-task&token=pyruns-e2e-access-token')
  const rows = page.locator('.xterm-rows')
  await expect(rows).toContainText('Finished')
  await expect(rows).toContainText('Progress stage 0079')
  await expect(rows).not.toContainText('Progress stage 0078')
  await page.getByRole('button', { name: 'Copy current log', exact: true }).click()
  await expect.poll(() => copiedLog).toBe(content)
})

test('revisiting a log restores its terminal before validation and refreshes a rewritten preview', async ({ page }) => {
  const names = ['alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta']
  const tasks = names.map(name => ({ name, status: 'completed', run_index: 1, task_kind: 'shell' }))
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: tasks, total: tasks.length, offset: 0, limit: 200, has_more: false,
  } }))
  let changed = false
  let alphaResponse: Promise<void> | undefined
  const alphaText = ['alpha-first', ...Array.from({ length: 150 }, (_, i) => 'line-' + i), 'output-alpha'].join('\n')
  const validators: (string | null)[] = []
  for (const { name } of tasks) {
    await page.route(`**/api/tasks/${name}/logs?*`, async route => {
      const known = new URL(route.request().url()).searchParams.get('known_preview')
      if (name === 'alpha') {
        validators.push(known)
        await alphaResponse
      }
      const revision = name === 'alpha' && changed ? 'new' : name
      return route.fulfill({ json: {
        selected_log: 'run1.log', available_logs: ['run1.log'], offset: 12, log_identity: name,
        preview_revision: revision, preview_unchanged: known === revision,
        content: known === revision ? '' : revision === 'alpha' ? alphaText : 'output-' + revision,
      } })
    })
  }
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const rows = page.locator('.xterm-rows')
  await expect(rows).toContainText('output-alpha')
  await rows.evaluate(node => node.setAttribute('data-original-terminal', 'yes'))
  await page.getByRole('region', { name: 'Read-only logs for alpha' }).hover()
  await page.mouse.wheel(0, -50_000)
  await expect(rows).not.toContainText('output-alpha')
  const firstRow = rows.locator(':scope > div').first()
  const scrolledRow = await firstRow.innerText()
  for (const fresh of [false, true]) {
    await page.getByRole('button', { name: 'View beta, completed', exact: true }).click()
    await expect(rows).toContainText('output-beta')
    changed = fresh
    let release!: () => void
    alphaResponse = new Promise<void>(resolve => { release = resolve })
    try {
      await page.getByRole('button', { name: 'View alpha, completed', exact: true }).click()
      await expect(page.getByText('Checking for new output…')).toBeVisible()
      await expect(rows).toHaveAttribute('data-original-terminal', 'yes')
      await expect(firstRow).toHaveText(scrolledRow)
      await expect(rows).not.toContainText('output-beta')
    } finally {
      release()
    }
    await expect(page.locator('.monitor-log-output')).toHaveAttribute('aria-busy', 'false')
    if (fresh) await expect(rows).toContainText('output-new')
    else await expect(firstRow).toHaveText(scrolledRow)
  }
  expect(validators).toEqual(['', 'alpha', 'alpha'])

  for (const name of [...names.slice(1, 6), 'alpha']) {
    await page.getByRole('button', { name: `View ${name}, completed`, exact: true }).click()
    await expect(rows).toContainText(name === 'alpha' ? 'output-new' : 'output-' + name)
  }
  await expect(rows).toHaveAttribute('data-original-terminal', 'yes')
  for (const name of [...names.slice(1), 'alpha']) {
    await page.getByRole('button', { name: `View ${name}, completed`, exact: true }).click()
    await expect(rows).toContainText(name === 'alpha' ? 'output-new' : 'output-' + name)
  }
  await expect(rows).not.toHaveAttribute('data-original-terminal', 'yes')
})

for (const emptyLog of [false, true]) {
  test(`monitor discards queued output when switching to ${emptyLog ? 'an empty' : 'another'} log`, async ({ page }) => {
    const now = new Date()
    await page.clock.install({ time: now })
    await page.clock.pauseAt(new Date(now.getTime() + 100))
    const tasks = ['old-task', 'new-task'].map(name => ({
      name, status: 'completed', run_index: 1, task_kind: 'shell',
    }))
    await page.route('**/api/tasks?*', route => route.fulfill({ json: {
      items: tasks, total: 2, offset: 0, limit: 200, has_more: false,
      status_counts: { pending: 0, queued: 0, running: 0, completed: 2, failed: 0, cancelled: 0 },
    } }))
    await page.routeWebSocket('**/api/tasks/events?*', socket => {
      socket.send(JSON.stringify({ type: 'ready' }))
    })
    for (const task of tasks) {
      await page.route(`**/api/tasks/${task.name}?*`, route => route.fulfill({ json: task }))
      await page.route(`**/api/tasks/${task.name}/logs?*`, route => route.fulfill({ json: {
        selected_log: 'run1.log', available_logs: ['run1.log', 'run0.log'],
        // The old log also leaves an unfinished OSC sequence in the parser.
        content: task.name === 'old-task' ? 'OLD_TASK_ONLY\r\n\x1b]0;unfinished' : emptyLog ? '' : 'NEW_TASK_ONLY\r\n',
        offset: 100, log_identity: task.name,
      } }))
    }

    await page.goto('/monitor?token=pyruns-e2e-access-token')
    await expect(page.getByRole('region', { name: 'Read-only logs for old-task' })).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Select task log file' })).toBeEnabled()
    // Keep the parser's timer paused while changing tasks: the previous write
    // must stay queued until after the new log has been selected and loaded.
    await page.getByRole('button', { name: 'View new-task, completed', exact: true }).dispatchEvent('click')
    const terminal = page.getByRole('region', { name: 'Read-only logs for new-task' })
    await expect(terminal).toBeVisible()
    await expect(page.getByRole('combobox', { name: 'Select task log file' })).toBeEnabled()
    await page.clock.runFor(200)

    const rows = terminal.locator('.xterm-rows')
    await expect(rows).not.toContainText('OLD_TASK_ONLY')
    if (emptyLog) await expect(rows).toHaveText('')
    else await expect(rows).toContainText('NEW_TASK_ONLY')
  })
}
