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
