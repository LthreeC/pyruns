import { expect, test, type Page } from '@playwright/test'

const initial = 'INITIAL\n'
const first = '中文😀\n'
const second = '続き🚀\n'
const byteLength = (value: string) => Buffer.byteLength(value, 'utf8')

async function prepareMonitor(page: Page) {
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
  const task = { name: 'live-task', status: 'running', run_index: 1, task_kind: 'shell' }
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 200, has_more: false,
    status_counts: { pending: 0, queued: 0, running: 1, completed: 0, failed: 0, cancelled: 0 },
  } }))
  await page.route('**/api/tasks/live-task?*', route => route.fulfill({ json: task }))
  await page.route('**/api/tasks/live-task/logs?*', route => route.fulfill({ json: {
    selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'original',
    content: initial, offset: byteLength(initial),
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

function completeRead(page: Page, content: string, offset: number, identity = 'original', reset = false) {
  return page.evaluate(payload => {
    (window as any).monitorTransportTest.reads[0].resolve(new Response(JSON.stringify({
      selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: payload.identity,
      content: payload.content, offset: payload.offset, reset: payload.reset,
    }), { headers: { 'Content-Type': 'application/json' } }))
  }, { content, offset, identity, reset })
}

function stream(page: Page, content: string, offset: number, identity = 'original', reset = false) {
  return page.evaluate(payload => {
    const socket = (window as any).monitorTransportTest.sockets.at(-1)
    socket.onopen?.()
    socket.onmessage?.({ data: JSON.stringify({ type: payload.reset ? 'reset' : 'chunk', task_name: 'live-task',
      log_file_name: 'run1.log', log_identity: payload.identity, content: payload.content, offset: payload.offset }) })
  }, { content, offset, identity, reset })
}

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
