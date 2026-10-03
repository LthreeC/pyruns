import { expect, test } from '@playwright/test'

const emptyCounts = { pending: 0, queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }

test('Manager cancels obsolete lists and preserves results through timeout and retry', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { stall: false, reads: 0, aborts: 0 }
    Object.assign(window, { managerReadTest: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      if (new URL(String(input), location.origin).pathname === '/api/tasks') {
        state.reads++
        if (state.stall) return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            state.aborts++
            reject(init.signal?.reason)
          }, { once: true })
        })
      }
      return original(input, init)
    }) as typeof window.fetch
  })
  const task = { name: 'retained-task', dir: '/tmp/retained-task', status: 'completed', run_index: 1, task_kind: 'shell' }
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 50, has_more: false,
    status_counts: { ...emptyCounts, completed: 1 },
  } }))
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  const snapshot = () => page.evaluate(() => (window as typeof window & {
    managerReadTest: { stall: boolean; reads: number; aborts: number }
  }).managerReadTest)
  const stall = (value: boolean) => page.evaluate(next => {
    (window as typeof window & { managerReadTest: { stall: boolean } }).managerReadTest.stall = next
  }, value)
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const refresh = page.getByRole('button', { name: 'Refresh tasks', exact: true })
  const card = page.locator('[data-task-card]')
  await expect(refresh).toBeEnabled()
  await expect(card).toContainText('retained-task')
  await stall(true)
  await refresh.click()
  await expect.poll(snapshot).toMatchObject({ reads: 2, aborts: 0 })
  await page.getByRole('combobox', { name: 'Filter tasks by status' }).selectOption('completed')
  await expect.poll(snapshot).toMatchObject({ reads: 3, aborts: 1 })
  await page.clock.runFor(10_001)
  await expect.poll(snapshot).toMatchObject({ aborts: 2 })
  await expect(refresh).toBeEnabled()
  const error = page.getByText('Task list loading timed out. Refresh to retry.', { exact: true })
  await expect(error).toBeVisible()
  await expect(card).toContainText('retained-task')
  await stall(false)
  await refresh.click()
  await expect(refresh).toBeEnabled()
  await expect(error).toBeHidden()
  await expect.poll(snapshot).toMatchObject({ reads: 4 })
  await stall(true)
  await refresh.click()
  await expect.poll(snapshot).toMatchObject({ reads: 5 })
  await page.getByRole('link', { name: 'Home', exact: true }).click()
  await expect.poll(snapshot).toMatchObject({ aborts: 3 })
})

test('Monitor times out stalled list reads without losing its page or open log', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { stall: false, offsets: [] as number[], aborts: 0 }
    Object.assign(window, { monitorReadTest: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin)
      if (url.pathname === '/api/tasks') {
        state.offsets.push(Number(url.searchParams.get('offset') || 0))
        if (state.stall) return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            state.aborts++
            reject(init.signal?.reason)
          }, { once: true })
        })
      }
      return original(input, init)
    }) as typeof window.fetch
  })
  const tasks = Array.from({ length: 401 }, (_, index) => ({
    name: `retained-${index}`, status: 'completed', run_index: 2, task_kind: 'shell',
  }))
  await page.route('**/api/tasks?*', route => {
    const offset = Number(new URL(route.request().url()).searchParams.get('offset') || 0)
    return route.fulfill({ json: { items: tasks.slice(offset, offset + 200), total: 401,
      offset, limit: 200, has_more: offset + 200 < 401, status_counts: { ...emptyCounts, completed: 401 } } })
  })
  let logReads = 0
  await page.route('**/api/tasks/retained-**', route => {
    const url = new URL(route.request().url())
    const match = url.pathname.match(/\/tasks\/retained-(\d+)(\/logs)?$/)!
    if (!match[2]) return route.fulfill({ json: tasks[Number(match[1])] })
    logReads++
    const log = url.searchParams.get('log_file_name') || 'run2.log'
    const content = `${log}: retained terminal output\n`
    return route.fulfill({ json: { content, offset: content.length, selected_log: log,
      available_logs: ['run2.log', 'run1.log'], log_identity: log } })
  })
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  const snapshot = () => page.evaluate(() => (window as typeof window & {
    monitorReadTest: { stall: boolean; offsets: number[]; aborts: number }
  }).monitorReadTest)
  const stall = (value: boolean) => page.evaluate(next => {
    (window as typeof window & { monitorReadTest: { stall: boolean } }).monitorReadTest.stall = next
  }, value)
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const sidebar = page.getByRole('complementary', { name: 'Task monitor sidebar' })
  const refresh = sidebar.getByRole('button', { name: 'Refresh tasks', exact: true })
  const next = sidebar.getByRole('button', { name: 'Next page', exact: true })
  const terminal = page.getByRole('region', { name: 'Read-only logs for retained-0' })
  const logs = page.getByRole('combobox', { name: 'Select task log file' })
  await expect(logs).toHaveValue('run2.log')
  await page.clock.runFor(200)
  await expect(terminal).toContainText('run2.log: retained terminal output')
  await logs.selectOption('run1.log')
  await expect(logs).toHaveValue('run1.log')
  await page.clock.runFor(200)
  await expect(terminal).toContainText('run1.log: retained terminal output')
  await page.clock.runFor(200)
  await next.click()
  await expect(sidebar.getByText('2 / 3', { exact: true })).toBeVisible()
  const historicalReads = logReads
  await stall(true)
  await refresh.click()
  await expect(refresh).toBeDisabled()
  await expect(next).toBeDisabled()
  await page.clock.runFor(10_001)
  await expect.poll(snapshot).toMatchObject({ aborts: 1 })
  await expect(refresh).toBeEnabled()
  await expect(next).toBeEnabled()
  await expect(sidebar.getByRole('alert')).toBeVisible()
  await expect(sidebar.getByText('2 / 3', { exact: true })).toBeVisible()
  await expect(sidebar.getByRole('button', { name: 'View retained-200, completed', exact: true })).toBeVisible()
  await expect(logs).toHaveValue('run1.log')
  await expect(terminal).toContainText('run1.log: retained terminal output')
  expect(logReads).toBe(historicalReads)
  await stall(false)
  await refresh.click()
  await expect(refresh).toBeEnabled()
  await expect(sidebar.getByRole('alert')).toBeHidden()
  expect((await snapshot()).offsets.slice(-2)).toEqual([200, 200])
  await expect(sidebar.getByText('2 / 3', { exact: true })).toBeVisible()
  await expect(logs).toHaveValue('run1.log')
  expect(logReads).toBe(historicalReads)
})

test('Launcher cancels replaced and hidden path checks and recovers after timeout', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { stall: true, reads: [] as string[], aborts: 0 }
    Object.assign(window, { launcherReadTest: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin)
      if (url.pathname === '/api/launcher/validate-path') {
        state.reads.push(url.searchParams.get('path') || '')
        if (state.stall) return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            state.aborts++
            reject(init.signal?.reason)
          }, { once: true })
        })
      }
      return original(input, init)
    }) as typeof window.fetch
  })
  await page.route('**/api/launcher/validate-path?*', route => route.fulfill({ json: {
    ok: true, message: 'Ready', normalized_path: '/valid.py',
  } }))
  const snapshot = () => page.evaluate(() => (window as typeof window & {
    launcherReadTest: { stall: boolean; reads: string[]; aborts: number }
  }).launcherReadTest)
  await page.goto('/launcher?token=pyruns-e2e-access-token&script=/first.py&config=/hidden.yaml')
  const dialog = page.getByRole('dialog', { name: 'Launch Workspace' })
  const input = dialog.getByRole('textbox', { name: 'Python script path' })
  await expect(input).toHaveValue('/first.py')
  await page.clock.runFor(301)
  await expect.poll(snapshot).toMatchObject({ reads: ['/first.py'], aborts: 0 })
  await input.fill('/second.py')
  await expect.poll(snapshot).toMatchObject({ aborts: 1 })
  await page.clock.runFor(301)
  await expect.poll(snapshot).toMatchObject({ reads: ['/first.py', '/second.py'] })
  await dialog.getByRole('button', { name: 'Shell', exact: true }).click()
  await expect.poll(snapshot).toMatchObject({ aborts: 2 })
  await page.clock.runFor(301)
  expect((await snapshot()).reads).toHaveLength(2)
  await dialog.getByRole('button', { name: 'Python', exact: true }).click()
  await expect.poll(snapshot).toMatchObject({ reads: ['/first.py', '/second.py', '/second.py'] })
  await page.clock.runFor(10_001)
  await expect(dialog.getByText('Path check timed out. Edit the path to retry.', { exact: true })).toBeVisible()
  await expect.poll(snapshot).toMatchObject({ aborts: 3 })
  await page.evaluate(() => {
    (window as typeof window & { launcherReadTest: { stall: boolean } }).launcherReadTest.stall = false
  })
  await input.fill('/valid.py')
  await page.clock.runFor(301)
  await expect(dialog.getByRole('button', { name: 'Select Script Path' })).toBeEnabled()
  await expect(dialog.getByRole('status')).toHaveText('/valid.py')
  await page.evaluate(() => {
    (window as typeof window & { launcherReadTest: { stall: boolean } }).launcherReadTest.stall = true
  })
  await input.fill('/closing.py')
  await page.clock.runFor(301)
  await expect.poll(async () => (await snapshot()).reads.at(-1)).toBe('/closing.py')
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect.poll(snapshot).toMatchObject({ aborts: 4 })
})

test('historical log timeout can retry the same file', async ({ page }) => {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    const state = { stall: true, aborts: 0, requestedLogs: [] as string[] }
    Object.assign(window, { historicalLogTest: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin)
      if (url.pathname === '/api/tasks/history-task/logs') {
        const logName = url.searchParams.get('log_file_name') || ''
        state.requestedLogs.push(logName)
        if (logName === 'run1.log' && state.stall) {
          return new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              state.aborts++
              reject(init.signal?.reason)
            }, { once: true })
          })
        }
      }
      return original(input, init)
    }) as typeof window.fetch
  })
  const task = { name: 'history-task', status: 'completed', run_index: 2, task_kind: 'shell' }
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 200, has_more: false,
    status_counts: { ...emptyCounts, completed: 1 },
  } }))
  await page.route('**/api/tasks/history-task?*', route => route.fulfill({ json: task }))
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  await page.route('**/api/tasks/history-task/logs?*', route => {
    const logName = new URL(route.request().url()).searchParams.get('log_file_name') || 'run2.log'
    return route.fulfill({ json: {
      selected_log: logName, available_logs: ['run2.log', 'run1.log'], log_identity: logName,
      content: logName === 'run1.log' ? 'Recovered historical output\n' : 'Latest output\n', offset: 100,
    } })
  })
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const logs = page.getByRole('combobox', { name: 'Select task log file' })
  await expect(logs).toHaveValue('run2.log')
  await logs.selectOption('run1.log')
  await page.clock.runFor(10_001)
  const retry = page.getByRole('button', { name: 'Retry', exact: true })
  await expect(retry).toBeVisible()
  await expect(page.getByText('Loading log…', { exact: true })).toBeHidden()
  expect(await page.evaluate(() => (window as typeof window & {
    historicalLogTest: { aborts: number }
  }).historicalLogTest.aborts)).toBe(1)
  await page.evaluate(() => {
    (window as typeof window & { historicalLogTest: { stall: boolean } }).historicalLogTest.stall = false
  })
  await retry.click()
  await expect(logs).toHaveValue('run1.log')
  await expect(retry).toBeHidden()
  await page.clock.runFor(200)
  await expect(page.getByRole('region', { name: 'Read-only logs for history-task' })).toContainText('Recovered historical output')
  expect(await page.evaluate(() => (window as typeof window & {
    historicalLogTest: { requestedLogs: string[] }
  }).historicalLogTest.requestedLogs.slice(-2))).toEqual(['run1.log', 'run1.log'])
})

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
  await page.routeWebSocket('**/api/tasks/events?*', socket => {
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
