import { expect, test, type Page } from '@playwright/test'

const initial = 'INITIAL\n'
const first = '中文😀\n'
const second = '続き🚀\n'
const byteLength = (value: string) => Buffer.byteLength(value, 'utf8')

async function prepareMonitor(page: Page, extraTasks: Record<string, unknown>[] = []) {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(() => {
    const state = { sockets: [] as ControlledSocket[], reads: [] as {
      url: string; signal?: AbortSignal | null; resolve: (value: Response) => void
    }[] }
    class ControlledSocket {
      addEventListener() { /* This fixture controls only ordinary disconnects. */ }
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      closed = false
      constructor(readonly url: string) {
        if (url.includes('/api/tasks/events')) {
          queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }))
        } else state.sockets.push(this)
      }
      close() { this.closed = true; this.onclose?.() }
    }
    const originalFetch = window.fetch.bind(window)
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin)
      if (url.pathname.endsWith('/live-task/logs') && url.searchParams.has('offset')) {
        // Intentionally ignore abort: a late result still needs a cursor guard.
        return new Promise<Response>(resolve => state.reads.push({ url: url.href, signal: init?.signal, resolve }))
      }
      return originalFetch(input, init)
    }) as typeof window.fetch
    Object.assign(window, { WebSocket: ControlledSocket, monitorTransportTest: state })
  })
  const task = { name: 'live-task', status: 'running', run_index: 1, task_kind: 'shell', runner_lease_expired: false }
  const tasks = [task, ...extraTasks]
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: tasks, total: tasks.length, offset: 0, limit: 200, has_more: false,
    status_counts: { pending: 0, queued: 0, running: tasks.length, completed: 0, failed: 0, cancelled: 0 },
  } }))
  await page.route('**/api/tasks/live-task?*', route => route.fulfill({ json: task }))
  await page.route('**/api/tasks/live-task/logs?*', route => route.fulfill({ json: {
    selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'original',
    log_revision: 'initial-revision', content: initial, offset: byteLength(initial),
  } }))
  await page.goto('/monitor?token=pyruns-e2e-access-token')
  await expect.poll(() => snapshot(page)).toMatchObject({ sockets: [{ closed: false }] })
  await page.clock.runFor(1600)
  await expect.poll(() => snapshot(page)).toMatchObject({ reads: [{ aborted: false }] })
  return page.getByRole('region', { name: 'Read-only logs for live-task' }).locator('.xterm-rows')
}

function snapshot(page: Page) {
  return page.evaluate(() => {
    const state = (window as any).monitorTransportTest
    return {
      sockets: state.sockets.map((socket: any) => ({ url: socket.url, closed: socket.closed })),
      reads: state.reads.map((read: any) => ({ url: read.url, aborted: Boolean(read.signal?.aborted) })),
    }
  })
}

function completeRead(page: Page, content: string, offset: number, identity = 'original', reset = false,
  { index = 0, fileSize = offset, revision }: { index?: number; fileSize?: number | null; revision?: string } = {},
) {
  return page.evaluate(payload => {
    (window as any).monitorTransportTest.reads[payload.index].resolve(new Response(JSON.stringify({
      selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: payload.identity,
      content: payload.content, offset: payload.offset, reset: payload.reset,
      log_revision: payload.revision,
      ...(payload.fileSize === null ? {} : { file_size: payload.fileSize }),
    }), { headers: { 'Content-Type': 'application/json' } }))
  }, { content, offset, identity, reset, index, fileSize, revision })
}

function stream(page: Page, content: string, offset: number, identity = 'original', reset = false, activity = false) {
  return page.evaluate(payload => {
    const socket = (window as any).monitorTransportTest.sockets.at(-1)
    socket.onopen?.()
    socket.onmessage?.({ data: JSON.stringify({ type: payload.reset ? 'reset' : 'chunk', task_name: 'live-task',
      log_file_name: 'run1.log', log_identity: payload.identity, content: payload.content, offset: payload.offset,
      activity: payload.activity }) })
  }, { content, offset, identity, reset, activity })
}

test('Monitor animates a task row while new output keeps arriving', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const rows = await prepareMonitor(page)
  const header = page.locator('.task-status-badge .task-status-dot')
  const row = (name: string) => page.locator(`[data-monitor-page-task="${name}"]`)
  const active = 'data-log-activity'
  await expect(header).not.toHaveAttribute(active)

  await stream(page, first, byteLength(initial + first))
  await page.clock.runFor(150)
  await expect(rows).toContainText(first.trim())
  await expect(header).not.toHaveAttribute(active)

  const end = byteLength(initial + first + second)
  await stream(page, second, end, 'original', false, true)
  await page.clock.runFor(150)
  await expect(header).toHaveAttribute(active, 'true')
  const strip = row('live-task').locator('.monitor-log-activity')
  await expect(strip).toBeVisible()
  expect(await strip.evaluate(el => getComputedStyle(el, '::after').animationName)).toBe('monitor-log-sweep')
  const initialStrip = await strip.elementHandle()
  const reads = (await snapshot(page)).reads.length
  for (let index = 1; index <= 3; index++) {
    await page.clock.runFor(1000)
    await stream(page, second, end + index * byteLength(second), 'original', false, true)
    await page.clock.runFor(100)
  }
  expect(await strip.evaluate((el, initial) => el === initial, initialStrip)).toBe(true)
  expect((await snapshot(page)).reads.length).toBe(reads)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await strip.evaluate(el => getComputedStyle(el, '::after').animationName)).toBe('none')
  await page.clock.runFor(45_000)
  await expect(strip).toBeVisible()
  await page.clock.fastForward(46_000)
  await expect(header).not.toHaveAttribute(active)
  await expect(strip).toHaveCount(0)
  await stream(page, second, end, 'original', false, true)
  await page.clock.runFor(150)
  await expect(header).not.toHaveAttribute(active)
})

test('Monitor observes new log output after switching to a finished task without refreshing the list', async ({ page }) => {
  const shared = { name: 'shared-writer', status: 'running', run_index: 1, task_kind: 'shell', runner_lease_expired: false }
  const copied = { name: 'git-snapshot', status: 'running', run_index: 1, task_kind: 'shell',
    runner_location: 'remote', runner_lease_expired: true }
  const history = { name: 'finished-task', status: 'completed', run_index: 1, task_kind: 'shell' }
  const sharedPosition = { name: shared.name, run_index: 1, identity: 'shared-file', size: 10 }
  const livePosition = { name: 'live-task', run_index: 1, identity: 'original', size: byteLength(initial) }
  const polls: string[][] = []
  let lists = 0
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/tasks') lists++
  })
  await page.route('**/api/monitor/log-positions', route => {
    const names: string[] = route.request().postDataJSON().task_names
    polls.push(names)
    return route.fulfill({ json: { items: [sharedPosition, livePosition].filter(position => names.includes(position.name)) } })
  })
  await page.route('**/api/tasks/finished-task?*', route => route.fulfill({ json: history }))
  await page.route('**/api/tasks/finished-task/logs?*', route => route.fulfill({ json: {
    selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'history',
    content: 'finished output\n', offset: byteLength('finished output\n'),
  } }))
  await prepareMonitor(page, [shared, copied, history])
  const row = (name: string) => page.locator(`[data-monitor-page-task="${name}"]`)
  const active = 'data-log-activity'
  const end = byteLength(initial + first)
  await stream(page, first, end, 'original', false, true)
  await page.clock.runFor(150)
  await expect(row('live-task')).toHaveAttribute(active, 'true')
  const listBaseline = lists
  await page.clock.runFor(2000)
  await expect.poll(() => polls.at(-1)).toEqual(['live-task', 'shared-writer', 'git-snapshot'])
  await expect(row(shared.name)).not.toHaveAttribute(active)

  const historyLoaded = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tasks/finished-task/logs')
  await row(history.name).click()
  await historyLoaded
  await page.clock.runFor(250)
  await expect(page.getByRole('region', { name: 'Read-only logs for finished-task' }).locator('.xterm-rows')).toContainText('finished output')
  sharedPosition.size = 20
  livePosition.size = end + byteLength(second)
  await page.clock.runFor(2000)
  await expect.poll(() => polls.at(-1)).toEqual(['live-task', 'shared-writer', 'git-snapshot'])
  await expect(row('live-task')).toHaveAttribute(active, 'true')
  await expect(row(shared.name)).toHaveAttribute(active, 'true')
  await expect(row(copied.name)).not.toHaveAttribute(active)

  sharedPosition.identity = 'replacement'
  sharedPosition.size = 40
  await page.clock.runFor(30_000)
  await expect(row(shared.name)).toHaveAttribute(active, 'true')
  await expect(row('live-task')).toHaveAttribute(active, 'true')
  expect(lists).toBe(listBaseline)
  livePosition.size += byteLength(second)
  const continued = page.waitForResponse(response => new URL(response.url()).pathname === '/api/monitor/log-positions')
  await page.clock.runFor(2000)
  await continued
  await page.clock.fastForward(59_000)
  await expect(row(shared.name)).not.toHaveAttribute(active)
  await expect(row('live-task')).toHaveAttribute(active, 'true')
  await page.clock.fastForward(32_000)
  await expect(row('live-task')).not.toHaveAttribute(active)

  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  const count = polls.length
  await page.clock.runFor(8000)
  expect(polls).toHaveLength(count)
})

test('shared logs use their last change on the first sample and detect writes of the same size', async ({ page }) => {
  const shared = { name: 'remote-writer', status: 'running', run_index: 1, task_kind: 'shell',
    task_storage: 'shared', runner_location: 'remote', runner_lease_expired: true }
  const stale = { ...shared, name: 'old-copy' }
  let sampledAt = 1_800_000_000_000
  const sharedPosition = { name: shared.name, run_index: 1, identity: 'shared-log', size: 300,
    modified_at_ms: sampledAt - 20_000 }
  const stalePosition = { ...sharedPosition, name: stale.name, identity: 'old-log',
    modified_at_ms: sampledAt - 120_000, revision: 'original-stale-revision' }
  await page.route('**/api/monitor/log-positions', route => {
    const names: string[] = route.request().postDataJSON().task_names
    return route.fulfill({ json: {
      sampled_at_ms: sampledAt, items: [sharedPosition, stalePosition].filter(item => names.includes(item.name)),
    } })
  })
  await prepareMonitor(page, [shared, stale])
  const row = page.locator('[data-monitor-page-task="remote-writer"]')
  const oldRow = page.locator('[data-monitor-page-task="old-copy"]')
  await expect(row).toHaveAttribute('data-log-activity', 'true')
  await expect(oldRow).not.toHaveAttribute('data-log-activity')
  const strip = row.locator('.monitor-log-activity')
  const initialStrip = await strip.elementHandle()

  await page.clock.fastForward(58_000)
  await expect(strip).toBeVisible()
  sampledAt += 58_000
  sharedPosition.modified_at_ms = sampledAt
  const updated = page.waitForResponse(response => new URL(response.url()).pathname === '/api/monitor/log-positions')
  await page.clock.runFor(2100)
  await updated
  await page.clock.fastForward(75_000)
  await expect(strip).toBeVisible()
  expect(await strip.evaluate((el, initial) => el === initial, initialStrip)).toBe(true)
  await page.clock.fastForward(16_000)
  await expect(strip).toHaveCount(0)
  await expect(oldRow).not.toHaveAttribute('data-log-activity')

  // A synchronizer overwrites the same bytes at the same offset and restores
  // the old mtime. Only the precise server revision changes.
  stalePosition.revision = 'rewritten-in-place'
  await page.clock.runFor(2100)
  await expect(oldRow).toHaveAttribute('data-log-activity', 'true')
  await page.clock.runFor(91_000)
  await expect(oldRow).not.toHaveAttribute('data-log-activity')
  stalePosition.size = 0
  await page.clock.runFor(2100)
  await expect(oldRow).toHaveAttribute('data-log-activity', 'true')
  await page.clock.fastForward(91_000)
  await expect(oldRow).not.toHaveAttribute('data-log-activity')
})

test('open terminal and HTTP fallback carry the preview revision across equal-length resets', async ({ page }) => {
  const rows = await prepareMonitor(page)
  const transport = await snapshot(page)
  for (const url of [transport.sockets[0].url, transport.reads[0].url]) {
    const params = new URL(url, 'http://localhost').searchParams
    expect(params.get('log_revision')).toBe('initial-revision')
    expect(params.get('tail_lines')).toBe('1000')
  }
  await page.evaluate(() => {
    const socket = (window as any).monitorTransportTest.sockets.at(-1)
    socket.onmessage?.({ data: JSON.stringify({ type: 'reset', task_name: 'live-task', log_file_name: 'run1.log',
      log_identity: 'original', log_revision: 'replaced-revision', content: 'CHANGED\n', offset: 8, activity: true }) })
  })
  await page.clock.runFor(150)
  await expect(rows).toContainText('CHANGED')
  await expect(rows).not.toContainText('INITIAL')
  await expect(page.locator('[data-monitor-page-task="live-task"]')).toHaveAttribute('data-log-activity', 'true')
  await page.evaluate(() => (window as any).monitorTransportTest.sockets.at(-1).close())
  await page.clock.runFor(800)
  await expect.poll(async () => new URL((await snapshot(page)).sockets.at(-1).url, 'http://localhost')
    .searchParams.get('log_revision')).toBe('replaced-revision')
})

test('HTTP fallback lights the row when it displays a confirmed equal-length edit', async ({ page }) => {
  const rows = await prepareMonitor(page)
  const row = page.locator('[data-monitor-page-task="live-task"]')
  await completeRead(page, '', byteLength(initial), 'original', false, { revision: 'initial-revision' })
  await page.clock.runFor(1600)
  await expect.poll(async () => (await snapshot(page)).reads.length).toBe(2)
  await expect(row).not.toHaveAttribute('data-log-activity')
  await completeRead(page, 'CHANGED\n', byteLength(initial), 'original', true, { index: 1, revision: 'after' })
  await page.clock.runFor(150)
  await expect(rows).toContainText('CHANGED')
  await expect(row).toHaveAttribute('data-log-activity', 'true')
})

test('returning to Monitor restores recent activity before reading logs again', async ({ page }) => {
  const other = { name: 'quiet-task', status: 'running', run_index: 1, task_kind: 'shell', runner_lease_expired: false }
  let polls = 0
  await page.route('**/api/monitor/log-positions', route => {
    polls++
    return route.fulfill({ json: { items: [] } })
  })
  await prepareMonitor(page, [other])
  const row = page.locator('[data-monitor-page-task="live-task"]')
  const active = 'data-log-activity'
  const home = page.getByRole('link', { name: 'Home', exact: true })
  const monitor = page.getByRole('link', { name: 'Monitor', exact: true })

  await stream(page, first, byteLength(initial + first), 'original', false, true)
  await page.clock.runFor(150)
  await expect(row).toHaveAttribute(active, 'true')
  await expect.poll(() => polls).toBeGreaterThan(0)

  await home.click()
  await expect(row).toHaveCount(0)
  const pollCount = polls
  await page.clock.fastForward(10_000)
  expect(polls).toBe(pollCount)
  await monitor.click()
  // The API returns no samples and virtual time is still paused.
  await expect(row).toHaveAttribute(active, 'true')

  await home.click()
  await expect(row).toHaveCount(0)
  await page.clock.fastForward(85_000)
  await monitor.click()
  await expect(row).toBeVisible()
  await expect(row).not.toHaveAttribute(active)
})

test('activity sampling recovers after a stalled request', async ({ page }) => {
  const shared = { name: 'shared-writer', status: 'running', run_index: 1, task_kind: 'shell' }
  let polls = 0
  const now = Date.now()
  await page.route('**/api/monitor/log-positions', route => {
    if (++polls === 1) return
    return route.fulfill({ json: {
      sampled_at_ms: now,
      items: [{ name: shared.name, run_index: 1, identity: 'shared', size: 10, modified_at_ms: now }],
    } })
  })
  await prepareMonitor(page, [shared])
  await expect.poll(() => polls).toBe(1)
  const row = page.locator('[data-monitor-page-task="shared-writer"]')
  await expect(row).not.toHaveAttribute('data-log-activity')
  await page.clock.runFor(8_100)
  await page.clock.runFor(2_100)
  await expect(row).toHaveAttribute('data-log-activity', 'true')
})

for (const reportsSize of [true, false]) test(
  reportsSize ? 'HTTP fallback animates after catching up to the initial log size' : 'HTTP fallback from older servers keeps the activity strip quiet',
  async ({ page }) => {
  const rows = await prepareMonitor(page)
  const header = page.locator('.task-status-badge .task-status-dot')
  const baseline = byteLength(initial + first + second)
  await completeRead(page, first, byteLength(initial + first), 'original', false, { fileSize: reportsSize ? baseline : null })
  await page.clock.runFor(150)
  await expect(rows).toContainText(first.trim())
  await page.clock.runFor(1600)
  await expect.poll(async () => (await snapshot(page)).reads.length).toBe(2)
  await completeRead(page, second, baseline, 'original', false, { index: 1, fileSize: reportsSize ? baseline : null })
  await page.clock.runFor(150)
  await expect(rows).toContainText(second.trim())
  await expect(header).not.toHaveAttribute('data-log-activity')
  await page.clock.runFor(1600)
  await expect.poll(async () => (await snapshot(page)).reads.length).toBe(3)
  const appended = 'new by polling\n'
  const end = baseline + byteLength(appended)
  await completeRead(page, appended, end, 'original', false, { index: 2, fileSize: reportsSize ? end : null })
  await page.clock.runFor(150)
  await expect(rows).toContainText(appended.trim())
  const strip = page.locator('[data-monitor-page-task="live-task"] .monitor-log-activity')
  if (reportsSize) await expect(strip).toBeVisible()
  else await expect(strip).toHaveCount(0)
})

test('an open socket waits for successful read health and exposes a read failure', async ({ page }) => {
  await prepareMonitor(page)
  await page.evaluate(() => (window as any).monitorTransportTest.sockets.at(-1).onopen?.())
  await expect(page.getByText('Connecting', { exact: true })).toBeVisible()
  await page.evaluate(() => (window as any).monitorTransportTest.sockets.at(-1).onmessage?.({ data: JSON.stringify({
    type: 'ready', task_name: 'live-task',
  }) }))
  await expect(page.getByRole('status').filter({ hasText: /^Live$/ }).last()).toBeVisible()
  await page.evaluate(() => {
    const socket = (window as any).monitorTransportTest.sockets.at(-1)
    // Simulate a close handshake which has not finished: HTTP must resume now.
    socket.close = () => { socket.closed = true }
    socket.onmessage?.({ data: JSON.stringify({
      type: 'error', task_name: 'live-task', detail: 'Live log reads failed. Retrying with incremental polling.',
    }) })
  })
  await expect(page.getByText('Live log reads failed. Retrying with incremental polling.')).toBeVisible()
  await expect(page.getByText('Reconnecting', { exact: true })).toBeVisible()
  await expect.poll(() => snapshot(page)).toMatchObject({ sockets: [{ closed: true }] })
  await completeRead(page, '', byteLength(initial))
  await page.clock.runFor(1600)
  expect((await snapshot(page)).reads).toHaveLength(2)
})

test('quiet successful heartbeats keep a healthy socket live', async ({ page }) => {
  await prepareMonitor(page)
  await page.evaluate(() => (window as any).monitorTransportTest.sockets.at(-1).onopen?.())
  for (let index = 0; index < 3; index++) {
    await page.evaluate(() => (window as any).monitorTransportTest.sockets.at(-1).onmessage?.({ data: JSON.stringify({
      type: 'heartbeat', task_name: 'live-task',
    }) }))
    await page.clock.runFor(15_000)
  }
  await expect(page.getByRole('status').filter({ hasText: /^Live$/ }).last()).toBeVisible()
  expect((await snapshot(page)).sockets).toHaveLength(1)
})

test('zero scrollback loads a visible tail and keeps live output on screen', async ({ page }) => {
  await page.route('**/api/workspace', async route => {
    const response = await route.fetch()
    const workspace = await response.json()
    await route.fulfill({ json: { ...workspace, settings: { ...workspace.settings, monitor_scrollback: 0 } } })
  })
  const rows = await prepareMonitor(page)
  await expect(rows).toContainText('INITIAL')
  await stream(page, 'visible after zero history\n', byteLength(initial + 'visible after zero history\n'))
  await page.clock.runFor(200)
  await expect(rows).toContainText('visible after zero history')
})

for (const reset of [false, true]) {
  test(`websocket ${reset ? 'reset' : 'resume'} rejects a late HTTP fallback`, async ({ page }) => {
    const rows = await prepareMonitor(page)
    const content = first + second
    const offset = byteLength((reset ? '' : initial) + content)
    await stream(page, content, offset, reset ? 'replacement' : 'original', reset)
    await page.clock.runFor(200)
    await expect(rows).toContainText(second.trim())
    await completeRead(page, first, byteLength(initial + first))
    await page.clock.runFor(200)
    expect((await rows.textContent())?.split(first.trim()).length).toBe(2)
    if (reset) await expect(rows).not.toContainText(initial.trim())
    expect((await snapshot(page)).reads[0].aborted).toBe(true)

    await page.evaluate(() => (window as any).monitorTransportTest.sockets.at(-1).close())
    await page.clock.runFor(800)
    const resumed = new URL((await snapshot(page)).sockets.at(-1)!.url)
    expect(resumed.searchParams.get('offset')).toBe(String(offset))
    expect(resumed.searchParams.get('log_identity')).toBe(reset ? 'replacement' : 'original')
  })

  test(`HTTP ${reset ? 'reset' : 'fallback'} hands its committed cursor to the next websocket`, async ({ page }) => {
    const rows = await prepareMonitor(page)
    const identity = reset ? 'replacement' : 'original'
    const offset = byteLength((reset ? '' : initial) + first)
    await completeRead(page, first, offset, identity, reset)
    await expect.poll(() => snapshot(page)).toMatchObject({ sockets: [{ closed: true }, { closed: false }] })
    const resumed = new URL((await snapshot(page)).sockets[1].url)
    expect(resumed.searchParams.get('offset')).toBe(String(offset))
    expect(resumed.searchParams.get('log_identity')).toBe(identity)
    await page.evaluate(() => {
      const retired = (window as any).monitorTransportTest.sockets[0]
      retired.onmessage?.({ data: JSON.stringify({ type: 'chunk', task_name: 'live-task',
        log_file_name: 'run1.log', log_identity: 'original', content: 'OBSOLETE\n', offset: 1000 }) })
    })
    await stream(page, second, offset + byteLength(second), identity)
    await page.clock.runFor(200)
    await expect(rows).toContainText(second.trim())
    expect((await rows.textContent())?.split(first.trim()).length).toBe(2)
    await expect(rows).not.toContainText('OBSOLETE')
    if (reset) await expect(rows).not.toContainText(initial.trim())
    await page.clock.runFor(6000)
    expect((await snapshot(page)).sockets).toHaveLength(2)
    expect((await snapshot(page)).reads).toHaveLength(1)
  })
}

test('a stalled fallback aborts and allows another read', async ({ page }) => {
  await prepareMonitor(page)
  // This fetch follows AbortSignal as real browsers do; the other cases above
  // deliberately retain the promise to verify late responses independently.
  await page.evaluate(() => {
    const read = (window as any).monitorTransportTest.reads[0]
    read.signal?.addEventListener('abort', () => read.resolve(new Response('{}')), { once: true })
  })
  await page.clock.runFor(11_000)
  await expect.poll(() => snapshot(page)).toMatchObject({ reads: [{ aborted: true }, { aborted: false }] })
  await page.getByRole('link', { name: 'Manager', exact: true }).click()
  expect((await snapshot(page)).reads[1].aborted).toBe(true)
})

test('a full repeated log window writes only new ANSI and Unicode fragments', async ({ page }) => {
  const rows = await prepareMonitor(page)
  const line = '\x1b[32mrepeated 中文🚀\x1b[0m\n'
  const content = line.repeat(250_000).slice(-4 * 1024 * 1024)
  let offset = byteLength(content)
  await stream(page, content, offset, 'full-window', true)
  await page.clock.runFor(300)
  await expect(rows).toContainText('repeated 中文🚀')
  // Observe the public write method on the live terminal held by Monitor's ref.
  // This leaves its real parser, scrollback and renderer in place.
  expect(await page.evaluate(() => {
    for (let el = document.querySelector('.xterm'); el; el = el.parentElement) {
      const key = Object.keys(el).find(key => key.startsWith('__reactFiber$'))
      for (let fiber = key && (el as any)[key]; fiber; fiber = fiber.return) {
        for (let hook = fiber.memoizedState; hook && typeof hook === 'object'; hook = hook.next) {
          const term = hook.memoizedState?.current
          if (!term || typeof term.write !== 'function' || !term.buffer?.active) continue
          const writes: string[] = []
          const original = term.write.bind(term)
          term.write = (text: string, callback?: () => void) => { writes.push(text); original(text, callback) }
          Object.assign(window, { monitorWrites: writes })
          return true
        }
      }
    }
    return false
  })).toBe(true)
  const chunks = [line.repeat(20), '\x1b[', '31mNEW 中文', '🚀\x1b[0m\n']
  for (const chunk of chunks) {
    offset += byteLength(chunk)
    await stream(page, chunk, offset, 'full-window')
  }
  await page.clock.runFor(200)
  await expect(rows).toContainText('NEW 中文🚀')
  expect(await page.evaluate(() => (window as any).monitorWrites.reduce((sum: number, text: string) => sum + text.length, 0))).toBe(chunks.join('').length)
  expect(await page.evaluate(() => (window as any).monitorWrites.join(''))).toBe(chunks.join(''))
  await stream(page, chunks.at(-1)!, offset, 'full-window')
  await page.clock.runFor(200)
  expect(await page.evaluate(() => (window as any).monitorWrites.join(''))).toBe(chunks.join(''))
  await stream(page, 'ROTATED\n', offset, 'replacement', true)
  await page.clock.runFor(200)
  await expect(rows).toContainText('ROTATED')
  await expect(rows).not.toContainText('repeated')
})
