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

test('hidden monitor coalesces task changes into one visible refresh', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { reads: 0 }
    Object.assign(window, { monitorRefreshTest: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith('/api/tasks?')) state.reads++
      return original(input, init)
    }) as typeof window.fetch
  })
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [], total: 0, offset: 0, limit: 200, has_more: false, status_counts: emptyCounts,
  } }))
  let sendEvent!: (value: string) => void
  await page.routeWebSocket('**/api/tasks/events', socket => {
    sendEvent = value => socket.send(value)
  })
  const reads = () => page.evaluate(() => (window as typeof window & {
    monitorRefreshTest: { reads: number }
  }).monitorRefreshTest.reads)
  const visibility = (value: 'visible' | 'hidden') => page.evaluate(state => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
    document.dispatchEvent(new Event('visibilitychange'))
    // Capture at fetch invocation, atomically with hiding, so a request started
    // while visible cannot arrive late at a route handler and count as hidden.
    return (window as typeof window & { monitorRefreshTest: { reads: number } }).monitorRefreshTest.reads
  }, value)
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  await expect(page.getByText('No tasks', { exact: true })).toBeVisible()
  await expect.poll(() => Boolean(sendEvent)).toBe(true)
  sendEvent(JSON.stringify({ type: 'ready' }))
  await expect(page.getByRole('status', { name: 'Task list updates live', exact: true })).toBeVisible()
  // Reproduce the CI ordering: the 120 ms startup debounce fires during the
  // final 50 ms of visibility, after the old test recorded its baseline.
  await page.clock.runFor(100)
  sendEvent(JSON.stringify({ type: 'changed' }))
  await page.clock.runFor(50)
  const before = await visibility('hidden')
  expect(before).toBe(2)
  for (let index = 0; index < 3; index++) {
    sendEvent(JSON.stringify({ type: 'changed' }))
    await page.clock.runFor(300)
  }
  await page.clock.runFor(60_001)
  expect(await reads()).toBe(before)
  await visibility('visible')
  await page.clock.runFor(200)
  await expect.poll(reads).toBe(before + 1)
  await page.clock.runFor(500)
  expect(await reads()).toBe(before + 1)
})
