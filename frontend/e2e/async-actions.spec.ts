import { expect, test, type Page } from '@playwright/test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

async function stallRead(page: Page, target: string) {
  await page.addInitScript(target => {
    const original = window.fetch.bind(window)
    const state = { enabled: false, reads: 0, aborts: 0, release: () => {} }
    Object.assign(window, { stalledRead: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      if (state.enabled && new URL(String(input), location.origin).pathname === target) {
        state.reads++
        return new Promise<Response>((resolve, reject) => {
          state.release = () => { state.enabled = false; resolve(original(input, init)) }
          init?.signal?.addEventListener('abort', () => {
            state.aborts++
            reject(init.signal?.reason)
          }, { once: true })
        })
      }
      return original(input, init)
    }) as typeof window.fetch
  }, target)
}

const readState = (page: Page) => page.evaluate(() => (window as any).stalledRead as { reads: number; aborts: number })

test('launcher config discovery can time out, retry and be cancelled', async ({ page }) => {
  await stallRead(page, '/api/launcher/configs')
  const root = await mkdtemp(join(tmpdir(), 'pyruns-launcher-cancel-'))
  const script = join(root, 'train.py')
  await writeFile(script, 'import pyruns\nconfig = pyruns.load()\n')
  let workspaceWrites = 0
  await page.route('**/api/launcher/open', route => { workspaceWrites++; return route.continue() })
  try {
    await page.goto('/launcher?token=pyruns-e2e-access-token')
    const dialog = page.getByRole('dialog', { name: 'Launch Workspace' })
    await dialog.getByRole('textbox', { name: 'Python script path' }).fill(script)
    const select = dialog.getByRole('button', { name: 'Select Script Path', exact: true })
    await expect(select).toBeEnabled()
    await page.clock.install()
    await page.evaluate(() => (window as any).stalledRead.enabled = true)
    await select.click()
    await expect.poll(() => readState(page)).toMatchObject({ reads: 1, aborts: 0 })
    await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeEnabled()
    await page.clock.runFor(10_001)
    await expect(dialog.getByText('Config discovery timed out. Please retry.', { exact: true })).toBeVisible()
    await expect(select).toBeEnabled()
    await select.click()
    await expect.poll(() => readState(page)).toMatchObject({ reads: 2, aborts: 1 })
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    await expect.poll(() => readState(page)).toMatchObject({ aborts: 2 })
    await page.evaluate(() => (window as any).stalledRead.release())
    expect(workspaceWrites).toBe(0)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('launcher keeps a submitted workspace change pending until its response', async ({ page }) => {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-launcher-open-'))
  const script = join(root, 'train.py')
  await writeFile(script, 'print("hello")\n')
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  let submitted = false
  await page.route('**/api/launcher/configs?*', route => route.fulfill({ json: {
    items: [{ path: join(root, 'config.yaml'), kind: 'workspace_default' }],
    requires_config_template: false, config_source: '',
  } }))
  try {
    await page.goto('/launcher?token=pyruns-e2e-access-token')
    const original = await (await page.request.get('/api/workspace')).json()
    await page.route('**/api/launcher/open', async route => {
      submitted = true
      await gate
      await route.fulfill({ json: original })
    })
    const dialog = page.getByRole('dialog', { name: 'Launch Workspace' })
    await dialog.getByRole('textbox', { name: 'Python script path' }).fill(script)
    await dialog.getByRole('button', { name: 'Select Script Path', exact: true }).click()
    await expect.poll(() => submitted).toBe(true)
    await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled()
    await page.keyboard.press('Escape')
    await expect(dialog).toBeVisible()
    release()
    await expect(dialog).toBeHidden()
  } finally {
    release()
    await rm(root, { recursive: true, force: true })
  }
})

test('creation reports saved tasks before the next on-demand template refresh', async ({ page }) => {
  await stallRead(page, '/api/templates')
  const root = await mkdtemp(join(tmpdir(), 'pyruns-create-refresh-'))
  const script = join(root, 'train.py'), config = join(root, 'input.yaml')
  await writeFile(script, 'import pyruns\nconfig = pyruns.load()\n')
  await writeFile(config, 'value: original\n')
  await page.goto('/?token=pyruns-e2e-access-token')
  const original = await (await page.request.get('/api/workspace')).json()
  try {
    const opened = await page.request.post('/api/launcher/open', { data: { script_path: script, config_path: config } })
    expect(opened.ok()).toBe(true)
    const workspace = await opened.json()
    await page.goto('/generator')
    await expect(page.getByRole('textbox', { name: 'value parameter value', exact: true })).toHaveValue('original')
    const picker = page.getByRole('button', { name: workspace.default_template.label, exact: true })
    const catalog = page.waitForResponse(response => new URL(response.url()).pathname === '/api/templates')
    await picker.click()
    expect((await catalog).ok()).toBe(true)
    await expect(page.getByText('Loading templates...', { exact: true })).toBeHidden()
    await page.getByPlaceholder('Search templates').press('Escape')
    await page.clock.install()
    await page.evaluate(() => (window as any).stalledRead.enabled = true)
    const createdResponse = page.waitForResponse(response => response.url().endsWith('/api/generator/create'))
    await page.getByRole('button', { name: 'Generate Tasks', exact: true }).click()
    const response = await createdResponse
    expect(response.ok()).toBe(true)
    const created = await response.json()
    expect(await readFile(join(workspace.run_root, 'tasks', created.items[0].name, 'config.yaml'), 'utf8')).toContain('value: original')
    await expect(page.getByText('Created 1 python task', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Generate Tasks', exact: true })).toBeEnabled()
    expect(await readState(page)).toMatchObject({ reads: 0, aborts: 0 })
    await picker.click()
    await expect.poll(() => readState(page)).toMatchObject({ reads: 1, aborts: 0 })
    await page.clock.runFor(10_001)
    await expect.poll(() => readState(page)).toMatchObject({ aborts: 1 })
    await expect(page.getByText('Created 1 python task', { exact: true })).toBeVisible()
  } finally {
    await page.request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    await rm(root, { recursive: true, force: true })
  }
})
