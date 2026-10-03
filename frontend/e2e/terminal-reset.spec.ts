import { expect, test } from '@playwright/test'

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
