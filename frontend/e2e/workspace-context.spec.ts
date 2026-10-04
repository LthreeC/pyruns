import { expect, test, type Page } from '@playwright/test'
import { mkdir, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

async function withWorkspaces(page: Page, check: (value: {
  first: string; second: string; taskName: string; other: Page
}) => Promise<void>) {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-workspace-context-'))
  await page.goto('/?token=pyruns-e2e-access-token')
  const request = page.request
  const original = await (await request.get('/api/workspace')).json()
  let other: Page | undefined
  try {
    const workspaces: string[] = []
    const names: string[] = []
    for (const name of ['实验 100%', 'second']) {
      const path = join(root, name)
      await mkdir(path)
      const opened = await request.post('/api/launcher/open-shell-root', { data: { path } })
      expect(opened.ok(), await opened.text()).toBe(true)
      const workspace = await opened.json()
      workspaces.push(workspace.run_root)
      const generated = await request.post('/api/generator/create', { data: {
        name_prefix: 'shared', mode: 'shell', shell_text: `echo ${name}`, append_timestamp: false,
      } })
      expect(generated.ok(), await generated.text()).toBe(true)
      names.push((await generated.json()).items[0].name)
    }
    expect(names[0]).toBe(names[1])
    const restored = await request.post('/api/workspace/run-root', { data: { path: workspaces[0] } })
    expect(restored.ok(), await restored.text()).toBe(true)
    other = await page.context().newPage()
    await other.goto('/manager')
    await expect(other.getByRole('button', { name: `Open details for ${names[0]}` })).toBeVisible()
    await check({ first: workspaces[0], second: workspaces[1], taskName: names[0], other })
  } finally {
    await other?.close()
    await page.close()
    const restored = await request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    expect(restored.ok(), await restored.text()).toBe(true)
    await rm(root, { recursive: true, force: true, maxRetries: 3 })
  }
}

async function switchFromOtherTab(other: Page, first: string, second: string) {
  const result = await other.evaluate(async ({ first, second }) => {
    const response = await fetch('/api/workspace/run-root', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pyruns-Workspace': encodeURIComponent(first) },
      body: JSON.stringify({ path: second }),
    })
    return { status: response.status, body: await response.json() }
  }, { first, second })
  expect(result.status).toBe(200)
  expect(result.body.run_root).toBe(second)
}

test('a delayed reorder read cannot write to a newly selected workspace', async ({ page }, testInfo) => {
  await withWorkspaces(page, async ({ first, second, taskName, other }) => {
    for (const path of [second, first]) {
      expect((await other.request.post('/api/workspace/run-root', { data: { path } })).ok()).toBe(true)
      expect((await other.request.post('/api/generator/create', { data: {
        name_prefix: 'neighbor', mode: 'shell', shell_text: 'echo neighbor', append_timestamp: false,
      } })).ok()).toBe(true)
      if (path === second) {
        expect((await other.request.post(`/api/tasks/${taskName}/pin`, { data: { pinned: true } })).ok()).toBe(true)
      }
    }
    await page.addInitScript(() => {
      const original = window.fetch.bind(window)
      const state = { consumed: false, writes: [] as string[], responses: [] as number[] }
      Object.assign(window, { reorderContextTest: state })
      window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), location.origin)
        if (url.pathname === '/api/tasks/reorder') {
          state.writes.push(new Headers(init?.headers).get('X-Pyruns-Workspace') || '')
        }
        const response = await original(input, init)
        if (url.pathname === '/api/tasks/reorder') state.responses.push(response.status)
        if (url.pathname === '/api/tasks' && url.searchParams.get('limit') === '10000') {
          const json = response.json.bind(response)
          response.json = async () => {
            try { return await json() }
            finally { setTimeout(() => { state.consumed = true }, 0) }
          }
        }
        return response
      }) as typeof window.fetch
    })
    await page.goto('/manager')
    await expect(page.locator('[data-task-card]')).toHaveCount(2)
    const firstName = await page.locator('[data-task-card]').first().getAttribute('data-task-card')
    let releaseRead!: () => void
    let readStarted!: () => void
    const gate = new Promise<void>(resolve => { releaseRead = resolve })
    const started = new Promise<void>(resolve => { readStarted = resolve })
    await page.route('**/api/tasks?*', async route => {
      if (new URL(route.request().url()).searchParams.get('limit') !== '10000') return route.continue()
      const response = await route.fetch()
      readStarted()
      await gate
      await route.fulfill({ response })
    })
    try {
      await page.getByRole('button', { name: `Move ${firstName} later`, exact: true }).click()
      await started
      const selectedSecond = page.waitForResponse(async response => (
        new URL(response.url()).pathname === '/api/workspace'
        && (await response.json()).run_root === second
      ))
      await switchFromOtherTab(other, first, second)
      await selectedSecond
      await expect(page.locator(`[data-task-card="${taskName}"]`)).toHaveAttribute('data-task-card-pinned', 'true')
      releaseRead()
      await page.waitForFunction(() => (window as any).reorderContextTest.consumed)
      const state = await page.evaluate(() => (window as any).reorderContextTest)
      if (state.writes.length) await page.waitForFunction(() => (window as any).reorderContextTest.responses.length > 0)
      const untouched = await (await other.request.get(`/api/tasks/${taskName}`)).json()
      await testInfo.attach('reorder-context', { body: JSON.stringify({ first, second, state, pinned: untouched.pinned }), contentType: 'application/json' })
      expect(state.writes).toEqual([])
      expect(untouched.pinned).toBe(true)
    } finally {
      releaseRead()
    }
  })
})

test('a drag gesture is cancelled when another tab selects a new workspace', async ({ page, isMobile }, testInfo) => {
  await withWorkspaces(page, async ({ first, second, taskName, other }) => {
    expect((await other.request.post('/api/workspace/run-root', { data: { path: second } })).ok()).toBe(true)
    expect((await other.request.post(`/api/tasks/${taskName}/pin`, { data: { pinned: true } })).ok()).toBe(true)
    expect((await other.request.post('/api/workspace/run-root', { data: { path: first } })).ok()).toBe(true)
    await page.addInitScript(() => {
      const original = window.fetch.bind(window)
      const state = { reads: 0, consumed: false, writes: [] as string[], responses: [] as number[] }
      Object.assign(window, { dragContextTest: state })
      window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), location.origin)
        const orderRead = url.pathname === '/api/tasks' && url.searchParams.get('limit') === '10000'
        if (orderRead) state.reads++
        if (url.pathname === '/api/tasks/reorder') state.writes.push(new Headers(init?.headers).get('X-Pyruns-Workspace') || '')
        const response = await original(input, init)
        if (url.pathname === '/api/tasks/reorder') state.responses.push(response.status)
        if (orderRead) {
          const json = response.json.bind(response)
          response.json = async () => {
            try { return await json() }
            finally { setTimeout(() => { state.consumed = true }, 0) }
          }
        }
        return response
      }) as typeof window.fetch
    })
    await page.goto('/manager')
    const card = page.locator(`[data-task-card="${taskName}"]`)
    await expect(card).toHaveAttribute('data-task-card-pinned', 'false')
    const handle = await card.locator('[data-task-drag-handle]').boundingBox()
    expect(handle).not.toBeNull()
    if (!handle) return
    const x = handle.x + handle.width / 2
    const y = handle.y + handle.height / 2
    const touch = isMobile ? await page.context().newCDPSession(page) : null
    let touchActive = false
    try {
      if (touch) {
        await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] })
        touchActive = true
        await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + 20, y: y + 20 }] })
      } else {
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 20, y + 20)
      }
      await expect(card).toHaveClass(/opacity-70/)
      await switchFromOtherTab(other, first, second)
      // Idle Manager polling detects the other tab's change within ten seconds.
      await expect(card).toHaveAttribute('data-task-card-pinned', 'true', { timeout: 15_000 })
      const target = await page.locator('[data-task-drop-target="tasks"]').boundingBox()
      expect(target).not.toBeNull()
      if (!target) return
      const point = { x: target.x + target.width / 2, y: target.y + 12 }
      if (touch) {
        await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point] })
        await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
        touchActive = false
      } else {
        await page.mouse.move(point.x, point.y)
        await page.mouse.up()
      }
      const reads = await page.evaluate(() => (window as any).dragContextTest.reads)
      if (reads) await page.waitForFunction(() => (window as any).dragContextTest.consumed)
      const writes = await page.evaluate(() => (window as any).dragContextTest.writes.length)
      if (writes) await page.waitForFunction(() => (window as any).dragContextTest.responses.length > 0)
      const state = await page.evaluate(() => (window as any).dragContextTest)
      const unchanged = await (await other.request.get(`/api/tasks/${taskName}`)).json()
      await testInfo.attach('drag-context', { body: JSON.stringify({ input: touch ? 'touch' : 'mouse', first, second, state, pinned: unchanged.pinned }), contentType: 'application/json' })
      expect(state.reads).toBe(0)
      expect(state.writes).toEqual([])
      expect(unchanged.pinned).toBe(true)
      await expect(card).not.toHaveClass(/opacity-70/)
    } finally {
      if (touch) {
        if (touchActive) await touch.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] })
        await touch.detach()
      } else {
        await page.mouse.up()
      }
    }
  })
})

test('another tab cannot redirect a notes save to a same-name task', async ({ page }) => {
  await withWorkspaces(page, async ({ first, second, taskName, other }) => {
    await page.clock.install()
    await page.goto('/manager')
    await page.getByRole('button', { name: `Open details for ${taskName}` }).click()
    await page.getByRole('tab', { name: 'Notes' }).click()
    await page.getByRole('textbox', { name: 'Task notes' }).fill('keep the first workspace draft')
    await page.clock.pauseAt(await page.evaluate(() => Date.now() + 100))
    await switchFromOtherTab(other, first, second)
    const writes: string[] = []
    page.on('request', request => {
      if (new URL(request.url()).pathname === `/api/tasks/${taskName}/notes`) {
        writes.push(request.headers()['x-pyruns-workspace'])
      }
    })
    const rejected = page.waitForResponse(response => new URL(response.url()).pathname === `/api/tasks/${taskName}/notes`)
    await page.getByRole('button', { name: 'Save Notes' }).click()
    expect((await rejected).status()).toBe(409)
    const overlay = page.getByRole('alertdialog', { name: 'Workspace changed' })
    await expect(overlay).toBeVisible()
    await expect(page.locator('textarea[aria-label="Task notes"]')).toHaveValue('keep the first workspace draft')
    expect(writes).toEqual([encodeURIComponent(first)])
    const secondTask = await (await other.request.get(`/api/tasks/${taskName}`)).json()
    expect(secondTask.notes).toBe('')

    await overlay.getByRole('button', { name: 'Discard drafts and reconnect' }).click()
    await expect(overlay).toBeHidden()
    await expect(page.getByRole('button', { name: `Open details for ${taskName}` })).toBeVisible()
    expect(writes).toHaveLength(1)
  })
})

for (const dirty of [false, true]) {
  test(`a workspace event rejection ${dirty ? 'preserves the open draft' : 'refreshes a clean tab'}`, async ({ page }) => {
    await withWorkspaces(page, async ({ first, second, taskName, other }) => {
      const sockets: string[] = []
      let completedLists = 0
      page.on('websocket', socket => sockets.push(socket.url()))
      page.on('requestfinished', request => {
        if (new URL(request.url()).pathname === '/api/tasks') completedLists++
      })
      await page.clock.install()
      await page.addInitScript(() => {
        const NativeSocket = window.WebSocket
        const closeCodes: number[] = []
        Object.assign(window, { workspaceCloseCodes: closeCodes, workspaceEventReady: false })
        window.WebSocket = class extends NativeSocket {
          constructor(url: string | URL, protocols?: string | string[]) {
            super(url, protocols)
            this.addEventListener('close', event => closeCodes.push(event.code))
            this.addEventListener('message', event => {
              if (JSON.parse(event.data).type === 'ready') (window as any).workspaceEventReady = true
            })
          }
        }
      })
      await page.goto('/monitor')
      await expect(page.getByRole('button', { name: `View ${taskName}, pending`, exact: true })).toBeVisible()
      await expect.poll(() => sockets.some(url => new URL(url).searchParams.get('expected_workspace') === first)).toBe(true)
      await expect.poll(() => page.evaluate(() => (window as any).workspaceEventReady)).toBe(true)
      await page.clock.pauseAt(await page.evaluate(() => Date.now() + 100))
      await page.clock.runFor(500)
      await expect.poll(() => completedLists).toBeGreaterThanOrEqual(2)
      if (dirty) {
        await page.getByRole('button', { name: 'View Details', exact: true }).click()
        await page.getByRole('tab', { name: 'Notes' }).click()
        await page.getByRole('textbox', { name: 'Task notes' }).fill('draft stays in the first workspace')
      }
      const refreshed = page.waitForResponse(
        response => new URL(response.url()).pathname === '/api/workspace',
        { timeout: 2000 },
      )
      await switchFromOtherTab(other, first, second)
      expect((await (await refreshed).json()).run_root).toBe(second)
      await expect.poll(() => page.evaluate(() => (window as any).workspaceCloseCodes)).toContain(4409)
      if (dirty) {
        await expect(page.getByRole('alertdialog', { name: 'Workspace changed' })).toBeVisible()
        await expect(page.locator('textarea[aria-label="Task notes"]')).toHaveValue('draft stays in the first workspace')
      } else {
        await expect.poll(() => sockets.some(url => new URL(url).searchParams.get('expected_workspace') === second)).toBe(true)
        await expect(page.getByRole('alertdialog', { name: 'Workspace changed' })).toBeHidden()
      }
    })
  })
}
