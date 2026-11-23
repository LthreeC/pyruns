import { expect, test } from '@playwright/test'

for (const view of ['manager', 'monitor']) {
  test(`${view} replays a task invalidation after the foreground snapshot completes`, async ({ page }) => {
    const now = new Date()
    await page.clock.install({ time: now })
    await page.clock.pauseAt(new Date(now.getTime() + 100))
    let reads = 0, hold = false, release: undefined | (() => void), completed = false
    const pageValue = () => ({ items: [{ name: 'audit-task', dir: '/tmp/audit-task', task_kind: 'shell',
      status: completed ? 'completed' : 'pending', run_index: 0 }], total: 1, offset: 0, limit: 200, has_more: false,
      status_counts: { pending: completed ? 0 : 1, completed: completed ? 1 : 0, failed: 0, running: 0, queued: 0, cancelled: 0 } })
    await page.route('**/api/tasks?*', async route => {
      reads++
      const captured = pageValue()
      if (hold) { hold = false; await new Promise<void>(resolve => { release = resolve }) }
      await route.fulfill({ json: captured })
    })
    await page.route('**/api/tasks/audit-task?*', route => route.fulfill({ json: pageValue().items[0] }))
    await page.route('**/api/tasks/audit-task/logs?*', route => route.fulfill({ json: {
      selected_log: '', available_logs: [], content: '', offset: 0, log_identity: '',
    } }))
    let send!: (value: string) => void
    await page.routeWebSocket('**/api/tasks/events?*', socket => {
      send = value => socket.send(value); socket.send(JSON.stringify({ type: 'ready' }))
    })
    await page.goto(`/${view}?token=pyruns-e2e-access-token`)
    const refresh = page.getByRole('button', { name: 'Refresh tasks', exact: true })
    await expect(refresh).toBeEnabled()
    await expect.poll(() => Boolean(send)).toBe(true)
    await page.clock.runFor(250)
    await expect.poll(() => reads).toBe(2)
    hold = true
    await refresh.click()
    await expect.poll(() => Boolean(release)).toBe(true)
    const before = reads
    completed = true
    send(JSON.stringify({ type: 'changed' }))
    await page.clock.runFor(500)
    const whileHeld = reads
    release!()
    await expect(refresh).toBeEnabled()
    await expect.poll(async () => {
      await page.clock.runFor(150)
      return reads
    }).toBe(before + 1)
    const afterRelease = reads
    expect(whileHeld).toBe(before)
    expect(afterRelease).toBe(before + 1)
    if (view === 'manager') await expect(page.locator('[data-task-card]')).toContainText('Completed')
    else await expect(page.getByRole('button', { name: 'View audit-task, completed', exact: true })).toBeVisible()
    await page.clock.runFor(1000)
    expect(reads).toBe(afterRelease)
  })
}

test('Monitor recovers event updates after a selected task read stalls and cancels it on leaving', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { hold: false, stalled: 0, signals: [] as boolean[], aborted: 0 }
    Object.assign(window, { auditDetached: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin)
      if (state.hold && url.pathname === '/api/tasks/audit-0') {
        state.stalled++; state.signals.push(Boolean(init?.signal))
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => { state.aborted++; reject(init.signal?.reason) }, { once: true })
        })
      }
      return original(input, init)
    }) as typeof window.fetch
  })
  const tasks = Array.from({ length: 401 }, (_, index) => ({ name: `audit-${index}`, dir: `/tmp/audit-${index}`,
    status: 'completed', task_kind: 'shell', run_index: 1 }))
  let reads = 0
  await page.route('**/api/tasks?*', route => {
    reads++
    const offset = Number(new URL(route.request().url()).searchParams.get('offset') || 0)
    return route.fulfill({ json: { items: tasks.slice(offset, offset + 200), total: 401, offset, limit: 200, has_more: offset + 200 < 401 } })
  })
  await page.route('**/api/tasks/audit-**', route => {
    const url = new URL(route.request().url())
    const match = url.pathname.match(/\/tasks\/audit-(\d+)(\/logs)?$/)!
    if (!match[2]) return route.fulfill({ json: tasks[Number(match[1])] })
    return route.fulfill({ json: { content: 'retained\n', selected_log: 'run1.log', available_logs: ['run1.log', 'run2.log'], offset: 9, log_identity: 'same' } })
  })
  let send!: (value: string) => void
  await page.routeWebSocket('**/api/tasks/events?*', socket => { send = value => socket.send(value); socket.send(JSON.stringify({ type: 'ready' })) })
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  await expect(page.getByRole('combobox', { name: 'Select task log file' })).toHaveValue('run1.log')
  await page.clock.runFor(250)
  const sidebar = page.getByRole('complementary', { name: 'Task monitor sidebar' })
  await sidebar.getByRole('button', { name: 'Next page', exact: true }).click()
  await expect(sidebar.getByText('2 / 3', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as any).auditDetached.hold = true)
  send(JSON.stringify({ type: 'changed' }))
  await page.clock.runFor(500)
  await expect.poll(() => page.evaluate(() => (window as any).auditDetached.stalled)).toBe(1)
  const afterFirst = reads
  await page.clock.runFor(10_001)
  const state = await page.evaluate(() => (window as any).auditDetached)
  expect(reads).toBe(afterFirst)
  expect(state).toMatchObject({ stalled: 1, signals: [true], aborted: 1 })
  await page.evaluate(() => (window as any).auditDetached.hold = false)
  send(JSON.stringify({ type: 'changed' }))
  await page.clock.runFor(500)
  await expect.poll(() => reads).toBe(afterFirst + 1)
  await expect(sidebar.getByText('2 / 3', { exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Read-only logs for audit-0' })).toContainText('retained')
  await page.getByRole('button', { name: 'View Details', exact: true }).click()
  const details = page.getByRole('dialog', { name: 'Task details for audit-0' })
  await expect(details.getByText('Completed', { exact: true })).toBeVisible()
  tasks[0].status = 'cancelled'
  send(JSON.stringify({ type: 'changed' }))
  await expect.poll(async () => {
    await page.clock.runFor(250)
    return details.getByText('Cancelled', { exact: true }).count()
  }).toBe(1)
  await details.getByRole('button', { name: 'Close task details' }).click()
  await page.evaluate(() => (window as any).auditDetached.hold = true)
  send(JSON.stringify({ type: 'changed' }))
  await page.clock.runFor(500)
  await expect.poll(() => page.evaluate(() => (window as any).auditDetached.stalled)).toBe(2)
  await page.getByRole('link', { name: 'Manager', exact: true }).click()
  await expect.poll(() => page.evaluate(() => (window as any).auditDetached.aborted)).toBe(2)
})
