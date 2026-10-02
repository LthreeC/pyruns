import { expect, test } from '@playwright/test'

const emptyCounts = { pending: 0, queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }

for (const stalledEndpoint of ['dashboard', 'metrics']) {
  test(`dashboard loads once and recovers when ${stalledEndpoint} times out`, async ({ page }) => {
    const now = new Date()
    await page.clock.install({ time: now })
    await page.clock.pauseAt(new Date(now.getTime() + 100))
    await page.addInitScript(() => {
      const original = window.fetch.bind(window)
      const state = { stall: '', dashboard: 0, metrics: 0, aborts: 0 }
      Object.assign(window, { dashboardRefreshTest: state })
      window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const endpoint = url.startsWith('/api/dashboard?') ? 'dashboard'
          : url.startsWith('/api/system/metrics?') ? 'metrics' : ''
        if (endpoint) {
          state[endpoint]++
          if (state.stall === endpoint) {
            return new Promise<Response>((_resolve, reject) => {
              const abort = () => { state.aborts++; reject(init?.signal?.reason) }
              if (init?.signal?.aborted) abort()
              else init?.signal?.addEventListener('abort', abort, { once: true })
            })
          }
        }
        return original(input, init)
      }) as typeof window.fetch
    })
    await page.route('**/api/dashboard?*', route => route.fulfill({ json: {
      summary: { total: 0, ...emptyCounts }, recent_tasks: [], active_task: null, template_count: 0,
    } }))
    await page.route('**/api/system/metrics?*', route => route.fulfill({ json: { cpu_percent: 5, mem_percent: 10, gpus: [] } }))
    await page.route('**/api/tasks?*', route => route.fulfill({ json: {
      items: [], total: 0, offset: 0, limit: 50, has_more: false, status_counts: emptyCounts,
    } }))
    const snapshot = () => page.evaluate(() => (window as typeof window & {
      dashboardRefreshTest: { stall: string; dashboard: number; metrics: number; aborts: number }
    }).dashboardRefreshTest)
    const stall = (endpoint: string) => page.evaluate(value => {
      (window as typeof window & { dashboardRefreshTest: { stall: string } }).dashboardRefreshTest.stall = value
    }, endpoint)
    await page.goto('/?token=pyruns-e2e-access-token')
    const refresh = page.getByRole('button', { name: 'Refresh dashboard', exact: true })
    const refreshError = page.getByText(stalledEndpoint === 'dashboard'
      ? 'Dashboard refresh timed out. Check the connection and retry.'
      : 'Metrics refresh failed. Showing last values.').first()
    await expect(refresh).toBeEnabled()
    await expect.poll(snapshot).toMatchObject({ dashboard: 1, metrics: 1, aborts: 0 })
    await stall(stalledEndpoint)
    await refresh.click()
    await expect.poll(snapshot).toMatchObject({ dashboard: 2, metrics: 2 })
    await page.clock.runFor(10_001)
    await expect(refresh).toBeEnabled()
    await expect.poll(snapshot).toMatchObject({ aborts: 1 })
    await expect(refreshError).toBeVisible()
    await stall('')
    await refresh.click()
    await expect(refresh).toBeEnabled()
    await expect.poll(snapshot).toMatchObject({ dashboard: 3, metrics: 3 })
    await expect(refreshError).toBeHidden()

    await stall(stalledEndpoint)
    await refresh.click()
    await expect.poll(snapshot).toMatchObject({ dashboard: 4, metrics: 4 })
    await page.getByRole('link', { name: 'Manager', exact: true }).click()
    await expect.poll(snapshot).toMatchObject({ aborts: 2 })
    await page.clock.runFor(30_001)
    expect(await snapshot()).toMatchObject({ dashboard: 4, metrics: 4 })
  })
}

test('monitor pauses background streams and resumes logs with one reconciled snapshot', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { reads: 0, refreshes: [] as string[], logReads: 0 }
    Object.assign(window, { monitorRefreshTest: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith('/api/tasks?')) {
        state.reads++
        state.refreshes.push(new URL(String(input), location.origin).searchParams.get('refresh') || '')
      }
      if (String(input).includes('/logs?')) state.logReads++
      return original(input, init)
    }) as typeof window.fetch
  })
  const task = { name: 'live-task', status: 'running', run_index: 1, task_kind: 'shell' }
  let finalTailReads = 0
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 200, has_more: false,
    status_counts: { ...emptyCounts, running: 1 },
  } }))
  await page.route('**/api/tasks/live-task?*', route => route.fulfill({ json: task }))
  await page.route('**/api/tasks/live-task/logs?*', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset'))
    const finalTail = !offset && task.status === 'completed'
    if (finalTail) finalTailReads++
    return route.fulfill({ json: {
      selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'same-log',
      content: finalTail ? 'finished while hidden\n' : offset ? '' : 'initial output\n',
      offset: finalTail ? 100000 : offset || 100,
    } })
  })
  let sendEvent!: (value: string) => void
  let eventConnections = 0
  let eventCloses = 0
  await page.routeWebSocket('**/api/tasks/events', socket => {
    eventConnections++
    socket.onClose(() => { eventCloses++ })
    sendEvent = value => socket.send(value)
    socket.send(JSON.stringify({ type: 'ready' }))
  })
  let sendLog!: (value: string) => void
  let logCloses = 0
  const logConnections: string[] = []
  await page.routeWebSocket('**/api/tasks/live-task/logs/stream?*', socket => {
    logConnections.push(socket.url())
    socket.onClose(() => { logCloses++ })
    sendLog = value => socket.send(value)
  })
  const reads = () => page.evaluate(() => (window as typeof window & {
    monitorRefreshTest: { reads: number; refreshes: string[]; logReads: number }
  }).monitorRefreshTest.reads)
  const visibility = (value: 'visible' | 'hidden') => page.evaluate(state => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
    document.dispatchEvent(new Event('visibilitychange'))
    // Capture at fetch invocation, atomically with hiding, so a request started
    // while visible cannot arrive late at a route handler and count as hidden.
    return (window as typeof window & { monitorRefreshTest: { reads: number } }).monitorRefreshTest.reads
  }, value)
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const terminal = page.getByRole('region', { name: 'Read-only logs for live-task' })
  await expect.poll(() => Boolean(sendEvent)).toBe(true)
  await expect.poll(() => logConnections.length).toBe(1)
  await expect(page.getByRole('status', { name: 'Task list updates live', exact: true })).toBeVisible()
  await page.clock.runFor(150)
  await expect(terminal).toContainText('initial output')
  await expect.poll(reads).toBe(2)
  for (let index = 0; index < 3; index++) sendEvent(JSON.stringify({ type: 'changed' }))
  await page.clock.runFor(150)
  await expect.poll(reads).toBe(3)
  const refreshes = () => page.evaluate(() => (window as typeof window & {
    monitorRefreshTest: { refreshes: string[] }
  }).monitorRefreshTest.refreshes)
  expect((await refreshes()).slice(1)).toEqual(['false', 'false'])
  sendLog(JSON.stringify({ type: 'chunk', task_name: task.name, log_file_name: 'run1.log',
    content: 'before hiding\n', offset: 114, log_identity: 'same-log' }))
  await page.clock.runFor(100)
  await expect(terminal).toContainText('before hiding')
  const before = await visibility('hidden')
  await expect.poll(() => [eventCloses, logCloses]).toEqual([1, 1])
  const logReads = () => page.evaluate(() => (window as typeof window & {
    monitorRefreshTest: { logReads: number }
  }).monitorRefreshTest.logReads)
  const logReadsBefore = await logReads()
  await page.clock.runFor(60_001)
  expect(await reads()).toBe(before)
  expect(await logReads()).toBe(logReadsBefore)
  expect([eventConnections, logConnections.length]).toEqual([1, 1])
  await visibility('visible')
  await expect.poll(() => [eventConnections, logConnections.length]).toEqual([2, 2])
  const resumed = new URL(logConnections[1])
  expect(resumed.searchParams.get('offset')).toBe('114')
  expect(resumed.searchParams.get('log_identity')).toBe('same-log')
  await page.clock.runFor(200)
  await expect.poll(reads).toBe(before + 1)
  expect((await refreshes()).at(-1)).toBe('true')
  expect(await logReads()).toBe(logReadsBefore)
  sendLog(JSON.stringify({ type: 'chunk', task_name: task.name, log_file_name: 'run1.log',
    content: 'while hidden\n', offset: 127, log_identity: 'same-log' }))
  await page.clock.runFor(500)
  await expect(terminal).toContainText('while hidden')
  await expect(terminal.getByRole('listitem').filter({ hasText: 'before hiding' })).toHaveCount(1)
  expect(await reads()).toBe(before + 1)
  await page.clock.runFor(60_001)
  await expect.poll(reads).toBe(before + 2)
  expect((await refreshes()).at(-1)).toBe('true')
  await visibility('hidden')
  await expect.poll(() => [eventCloses, logCloses]).toEqual([2, 2])
  task.status = 'completed'
  await visibility('visible')
  await expect.poll(() => eventConnections).toBe(3)
  await page.clock.runFor(200)
  await expect.poll(() => finalTailReads).toBe(1)
  await page.clock.runFor(100)
  await expect(terminal).toContainText('finished while hidden')
})

test('manager refreshes on return and coalesces a return during an active request', async ({ page }) => {
  await page.clock.install()
  let reads = 0
  let release: (() => void) | undefined
  await page.route('**/api/tasks?*', async route => {
    reads++
    if (reads === 2) await new Promise<void>(resolve => { release = resolve })
    await route.fulfill({ json: {
      items: [], total: 0, offset: 0, limit: 50, has_more: false, status_counts: emptyCounts,
    } })
  })
  const visibility = (value: 'visible' | 'hidden') => page.evaluate(state => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
    document.dispatchEvent(new Event('visibilitychange'))
  }, value)
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await expect(page.getByRole('textbox', { name: 'Search tasks', exact: true })).toBeVisible()
  await expect.poll(() => reads).toBe(1)
  await visibility('hidden')
  await page.clock.fastForward(60_001)
  expect(reads).toBe(1)
  await visibility('visible')
  await expect.poll(() => reads).toBe(2)
  await visibility('hidden')
  await visibility('visible')
  expect(reads).toBe(2)
  release?.()
  await expect.poll(() => reads).toBe(3)
  await page.clock.runFor(500)
  expect(reads).toBe(3)
})
