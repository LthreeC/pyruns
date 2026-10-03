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
      if (request.url().endsWith(`/api/tasks/${taskName}/notes`)) {
        writes.push(request.headers()['x-pyruns-workspace'])
      }
    })
    const rejected = page.waitForResponse(response => response.url().endsWith(`/api/tasks/${taskName}/notes`))
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
